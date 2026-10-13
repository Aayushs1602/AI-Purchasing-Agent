import shutil
from statistics import fmean

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    Budget,
    DemandForecast,
    PurchaseOrder,
    Recommendation,
    SalesRecord,
    Supplier,
    SupplierTerm,
)
from app.scenarios import deep_merge, load_scenarios
from app.seed import seed_scenarios

SCENARIOS_DIR = get_settings().scenarios_dir
TODAY = get_settings().sim_today


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIOS_DIR)


def test_all_thirteen_scenarios_load(scenarios):
    assert len(scenarios) == 13
    assert scenarios[0].id == "accept_baseline"


def test_overrides_merge_onto_the_baseline(scenarios):
    moq = next(s for s in scenarios if s.id == "moq_roundup").world

    assert moq.terms.moq == 1000  # overridden
    assert moq.terms.lead_time_days == 4  # inherited from the baseline


def test_deep_merge_replaces_lists_and_keeps_base_untouched():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}

    merged = deep_merge(base, {"a": {"c": [3]}})

    assert merged == {"a": {"b": 1, "c": [3]}, "d": 1}
    assert base["a"]["c"] == [1, 2]


def test_duplicate_skus_are_rejected(tmp_path):
    shutil.copy(SCENARIOS_DIR / "_baseline.yaml", tmp_path)
    shutil.copy(SCENARIOS_DIR / "01_accept_baseline.yaml", tmp_path / "01_a.yaml")
    text = (SCENARIOS_DIR / "01_accept_baseline.yaml").read_text(encoding="utf-8")
    (tmp_path / "02_b.yaml").write_text(
        text.replace("id: accept_baseline", "id: copy").replace("REC-1001", "REC-9999"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate sku"):
        load_scenarios(tmp_path)


def test_unknown_fields_are_rejected(tmp_path):
    shutil.copy(SCENARIOS_DIR / "_baseline.yaml", tmp_path)
    text = (SCENARIOS_DIR / "01_accept_baseline.yaml").read_text(encoding="utf-8")
    (tmp_path / "01_typo.yaml").write_text(text + "unexpected_field: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="01_typo.yaml"):
        load_scenarios(tmp_path)


class TestSeeding:
    @pytest.fixture
    def seeded(self, session, scenarios):
        seed_scenarios(session, scenarios, TODAY)
        return session

    def count(self, session, model):
        return session.scalar(select(func.count()).select_from(model))

    def test_row_counts(self, seeded):
        assert self.count(seeded, Recommendation) == 13
        assert self.count(seeded, SupplierTerm) == 13
        assert self.count(seeded, Budget) == 13
        assert self.count(seeded, Supplier) == 7
        assert self.count(seeded, PurchaseOrder) == 2
        assert self.count(seeded, DemandForecast) == 13 * 60
        assert self.count(seeded, SalesRecord) == 13 * 28

    def test_sales_follow_the_weekly_pattern(self, seeded):
        def last_14_days_avg(sku):
            rows = seeded.scalars(
                select(SalesRecord.units).where(SalesRecord.sku == sku).order_by(SalesRecord.day)
            ).all()
            return fmean(rows[-14:])

        assert last_14_days_avg("SKU-1001") == 50
        assert last_14_days_avg("SKU-1008") == 25  # forecast drift scenario

    def test_dates_are_relative_to_the_simulated_today(self, seeded):
        po = seeded.get(PurchaseOrder, "PO-2042")
        assert (po.eta - TODAY).days == -3

        stale = seeded.scalar(select(Recommendation).where(Recommendation.id == "REC-1007"))
        assert (TODAY - stale.created_on).days == 2

    def test_mock_supplier_behaviour_is_stored(self, seeded):
        terms = seeded.scalar(select(SupplierTerm).where(SupplierTerm.sku == "SKU-1010"))

        assert terms.mock_ack == {"behaviour": "price_increase", "new_unit_cost": 2.9}
