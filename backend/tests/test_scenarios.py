import shutil
from statistics import fmean

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import create_schema, make_engine, make_memory_engine, make_session_factory
from app.models import (
    SCHEMA_VERSION,
    Budget,
    DemandForecast,
    PurchaseOrder,
    Recommendation,
    SalesRecord,
    SchemaInfo,
    Supplier,
    SupplierTerm,
)
from app.scenarios import deep_merge, load_baseline_world, load_scenarios
from app.seed import add_world, ensure_database, seed_scenarios, stored_schema_version

SCENARIOS_DIR = get_settings().scenarios_dir
TODAY = get_settings().sim_today


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIOS_DIR)


def test_all_scenarios_load(scenarios):
    assert len(scenarios) == 14
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
        assert self.count(seeded, Recommendation) == 14
        assert self.count(seeded, SupplierTerm) == 14
        assert self.count(seeded, Budget) == 14
        assert self.count(seeded, Supplier) == 7
        assert self.count(seeded, PurchaseOrder) == 3
        assert self.count(seeded, DemandForecast) == 14 * 60
        assert self.count(seeded, SalesRecord) == 14 * 28

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


class TestAddWorld:
    """Adding one world to a database that already holds the scenarios (custom scenarios)."""

    @pytest.fixture
    def seeded(self, session, scenarios):
        seed_scenarios(session, scenarios, TODAY)
        return session

    def world(self, **supplier):
        world = load_baseline_world(SCENARIOS_DIR)
        updates = {
            "product": world.product.model_copy(
                update={"sku": "SKU-X1", "name": "Test Tea", "category": "Tea"}
            ),
            "recommendation": world.recommendation.model_copy(update={"id": "REC-X1"}),
        }
        if supplier:
            updates["supplier"] = world.supplier.model_copy(update=supplier)
        return world.model_copy(update=updates)

    def test_reuses_the_node_and_an_existing_supplier(self, seeded):
        add_world(seeded, self.world(), scenario_id="custom-x", title="Tea", today=TODAY)
        seeded.commit()

        rec = seeded.get(Recommendation, "REC-X1")
        assert (rec.scenario, rec.supplier_id) == ("custom-x", "SUP-ANDES")
        assert seeded.scalar(select(func.count()).select_from(Supplier)) == 7

    def test_adds_a_new_supplier(self, seeded):
        world = self.world(id="SUP-NEW", name="New Supplier")
        add_world(seeded, world, scenario_id="custom-x", title="Tea", today=TODAY)

        assert seeded.get(Supplier, "SUP-NEW").name == "New Supplier"

    def test_refuses_a_supplier_that_conflicts_with_the_stored_one(self, seeded):
        world = self.world(fill_rate=0.5)  # SUP-ANDES exists with 0.98

        with pytest.raises(ValueError, match="SUP-ANDES is defined differently"):
            add_world(seeded, world, scenario_id="custom-x", title="Tea", today=TODAY)


def test_memory_engine_is_one_database_across_sessions(scenarios):
    engine = make_memory_engine()
    create_schema(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        seed_scenarios(session, scenarios, TODAY)

    with factory() as other:
        assert other.scalar(select(func.count()).select_from(Recommendation)) == 14
    engine.dispose()


class TestSchemaVersion:
    @pytest.fixture
    def fresh_engine(self, tmp_path):
        engine = make_engine(f"sqlite:///{(tmp_path / 'versioned.db').as_posix()}")
        yield engine
        engine.dispose()

    def test_first_start_seeds_and_stamps_the_version(self, fresh_engine):
        ensure_database(fresh_engine, SCENARIOS_DIR, TODAY)

        assert stored_schema_version(fresh_engine) == SCHEMA_VERSION
        with make_session_factory(fresh_engine)() as session:
            assert session.scalar(select(func.count()).select_from(Recommendation)) == 14

    def test_matching_version_keeps_existing_data(self, fresh_engine):
        ensure_database(fresh_engine, SCENARIOS_DIR, TODAY)
        with make_session_factory(fresh_engine)() as session:
            session.get(Recommendation, "REC-1001").status = "decided"
            session.commit()

        ensure_database(fresh_engine, SCENARIOS_DIR, TODAY)

        with make_session_factory(fresh_engine)() as session:
            assert session.get(Recommendation, "REC-1001").status == "decided"

    def test_outdated_or_unstamped_database_is_rebuilt(self, fresh_engine):
        ensure_database(fresh_engine, SCENARIOS_DIR, TODAY)
        with make_session_factory(fresh_engine)() as session:
            session.get(Recommendation, "REC-1001").status = "decided"
            session.get(SchemaInfo, SCHEMA_VERSION).version = SCHEMA_VERSION - 1
            session.commit()

        ensure_database(fresh_engine, SCENARIOS_DIR, TODAY)

        assert stored_schema_version(fresh_engine) == SCHEMA_VERSION
        with make_session_factory(fresh_engine)() as session:
            assert session.get(Recommendation, "REC-1001").status == "open"
