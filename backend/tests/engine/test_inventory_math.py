import pytest

from app.engine import inventory_math as im
from app.engine.models import Assumptions
from tests.engine.factories import make_context, open_po

DEFAULT = Assumptions()


class TestBaseline:
    def test_need_is_exactly_the_recommended_800(self):
        ctx = make_context()

        assert im.coverage_window_days(ctx) == 18
        assert im.order_up_to_level(ctx, DEFAULT) == 1100
        assert im.inventory_position(ctx, DEFAULT) == 300
        assert im.net_need(ctx, DEFAULT) == 800

    def test_projection_for_800(self):
        p = im.project(make_context(), 800)

        assert p.order_value == 2000.0
        assert p.arrival_day == 4
        assert p.on_hand_at_arrival == 100  # 300 - 4 days * 50
        assert p.on_hand_after_receipt == 900
        assert p.peak_on_hand_after_arrival == 900
        assert p.cover_days_after_receipt == 18
        assert p.stockout_day_without_order == 6  # 300 units / 50 per day
        assert p.units_short_in_window == 0


class TestOpenPurchaseOrders:
    def test_open_po_counts_toward_inventory_position(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=2)])

        assert im.inventory_position(ctx, DEFAULT) == 620
        assert im.net_need(ctx, DEFAULT) == 480

    def test_800_on_top_of_open_po_overflows_storage(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=2)])

        # Day 4 before receipt: 300 - 100 + 320 - 100 = 420; plus 800 = 1,220
        assert im.project(ctx, 800).peak_on_hand_after_arrival == 1220
        assert im.max_storable_quantity(ctx, DEFAULT) == 780

    def test_excluded_po_is_ignored(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=2, po_id="PO-HOLD")])
        assumptions = Assumptions(exclude_po_ids=["PO-HOLD"])

        assert im.net_need(ctx, assumptions) == 800
        assert im.project(ctx, 800, assumptions).peak_on_hand_after_arrival == 900

    def test_overdue_po_is_projected_to_arrive_today(self):
        ctx = make_context(open_purchase_orders=[open_po(320, eta_in_days=-3)])

        p = im.project(ctx, 0)
        assert p.inventory_position == 620
        assert p.stockout_day_without_order == 12  # 620 units / 50 per day


class TestDemandBasis:
    def test_recent_actuals_basis_uses_last_14_days(self):
        ctx = make_context(recent_daily_sales=[50.0] * 14 + [25.0] * 14)
        actuals = Assumptions(demand_basis="recent_actuals")

        assert im.recent_sales_rate(ctx) == 25.0
        assert im.order_up_to_level(ctx, actuals) == 650  # 25 * 18 + 200
        assert im.net_need(ctx, actuals) == 350

    def test_forecast_shorter_than_horizon_extends_with_its_average(self):
        ctx = make_context(daily_forecast=[50.0] * 10)

        assert im.demand_series(ctx, DEFAULT, 20)[15] == 50.0

    def test_missing_sales_history_is_an_error_for_actuals_basis(self):
        ctx = make_context(recent_daily_sales=[])

        with pytest.raises(ValueError):
            im.demand_series(ctx, Assumptions(demand_basis="recent_actuals"))


class TestRounding:
    @pytest.mark.parametrize(
        ("qty", "moq", "case", "expected"),
        [
            (800, 400, 20, 800),
            (801, 400, 20, 820),
            (350, 400, 20, 400),  # raised to MOQ
            (800, 1000, 20, 1000),
            (10, 50, 24, 72),  # MOQ not a case multiple -> next case multiple above it
            (0, 400, 20, 0),
        ],
    )
    def test_round_up_to_valid(self, qty, moq, case, expected):
        assert im.round_up_to_valid(qty, moq, case) == expected

    @pytest.mark.parametrize(
        ("qty", "moq", "case", "expected"),
        [
            (758, 400, 20, 740),
            (600, 400, 20, 600),
            (399, 400, 20, 0),  # no valid order below the MOQ
            (800, 1000, 20, 0),
        ],
    )
    def test_round_down_to_valid(self, qty, moq, case, expected):
        assert im.round_down_to_valid(qty, moq, case) == expected


class TestLimits:
    def test_max_affordable_quantity(self):
        assert im.max_affordable_quantity(make_context(budget_available=1500)) == 600
        ctx = make_context(budget_available=2200, supplier={"unit_cost": 2.90})
        assert im.max_affordable_quantity(ctx) == 740  # 758 units -> 37 cases

    def test_max_storable_quantity(self):
        assert im.max_storable_quantity(make_context(storage_capacity=800), DEFAULT) == 700

    def test_nothing_storable_when_already_over_capacity_at_arrival(self):
        ctx = make_context(on_hand=1500, storage_capacity=1200)

        assert im.max_storable_quantity(ctx, DEFAULT) == 0

    def test_reserved_units_are_not_available(self):
        ctx = make_context(reserved=100)

        assert im.available_on_hand(ctx) == 200
        assert im.net_need(ctx, DEFAULT) == 900
