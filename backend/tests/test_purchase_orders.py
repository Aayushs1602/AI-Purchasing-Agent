import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.engine.models import Assumptions
from app.models import Budget, Product, PurchaseOrder, PurchaseOrderEvent, Recommendation
from app.scenarios import load_scenarios
from app.seed import seed_scenarios
from app.services.context import build_context
from app.services.purchase_orders import (
    HardConstraintError,
    InvalidTransitionError,
    LivePOExistsError,
    PurchaseOrderService,
    SupplierUnavailableError,
)
from app.services.supplier_gateway import SupplierGatewayError

TODAY = get_settings().sim_today


@pytest.fixture
def seeded(session):
    seed_scenarios(session, load_scenarios(get_settings().scenarios_dir), TODAY)
    return session


@pytest.fixture
def service(seeded):
    return PurchaseOrderService(seeded, today=TODAY)


def draft(service, rec_id, quantity=None, **kwargs):
    rec = service.session.get(Recommendation, rec_id)
    return service.create_draft(
        sku=rec.sku,
        node_id=rec.node_id,
        supplier_id=rec.supplier_id,
        quantity=quantity or rec.quantity,
        created_by="agent",
        recommendation_id=rec.id,
        **kwargs,
    )


def available(session, rec_id):
    rec = session.get(Recommendation, rec_id)
    category = session.get(Product, rec.sku).category
    return session.scalar(select(Budget).where(Budget.category == category)).available


def events(session, po_id):
    return session.scalars(
        select(PurchaseOrderEvent.event)
        .where(PurchaseOrderEvent.po_id == po_id)
        .order_by(PurchaseOrderEvent.id)
    ).all()


def agent_po_count(session):
    return session.scalar(
        select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.created_by == "agent")
    )


class TestCreate:
    def test_valid_draft(self, service):
        result = draft(service, "REC-1001")

        assert (result.po.id, result.po.status, result.po.quantity) == ("PO-5001", "draft", 800)
        assert result.evaluation.feasible
        assert available(service.session, "REC-1001") == 5000  # drafts hold no budget
        assert events(service.session, "PO-5001") == ["created"]

    def test_hard_violation_creates_nothing(self, service):
        with pytest.raises(HardConstraintError) as exc:
            draft(service, "REC-1002")  # 800 on top of the open PO overflows storage

        assert [f.code for f in exc.value.evaluation.hard_violations] == ["OVER_STORAGE_CAPACITY"]
        assert agent_po_count(service.session) == 0

    def test_create_is_idempotent_by_key(self, service):
        first = draft(service, "REC-1001", idempotency_key="run-1:create")
        second = draft(service, "REC-1001", idempotency_key="run-1:create")

        assert second.replayed
        assert second.po.id == first.po.id
        assert agent_po_count(service.session) == 1

    def test_one_live_po_per_recommendation(self, service):
        draft(service, "REC-1001")

        with pytest.raises(LivePOExistsError):
            draft(service, "REC-1001")

    def test_assumption_override_is_validated_and_stored(self, service):
        with pytest.raises(HardConstraintError):
            draft(service, "REC-1003")  # counting the held PO, 800 overflows storage

        result = draft(service, "REC-1003", assumptions=Assumptions(exclude_po_ids=["PO-2042"]))

        assert result.po.assumptions == {"exclude_po_ids": ["PO-2042"], "demand_basis": "forecast"}
        assert result.evaluation.requires_approval


class TestSubmit:
    def test_auto_submit_confirms_and_commits_budget(self, service):
        po_id = draft(service, "REC-1001").po.id

        result = service.submit(po_id, actor="agent", confidence=0.9)

        assert result.po.status == "confirmed"
        assert (result.ack.confirmed_quantity, result.po.eta) == (800, TODAY.replace(day=5))
        assert available(service.session, "REC-1001") == 3000
        assert result.evaluation.feasible
        assert events(service.session, po_id) == ["created", "submitted", "acknowledged"]

    def test_high_value_waits_for_approval_then_goes_out(self, service):
        po_id = draft(service, "REC-1012").po.id

        pending = service.submit(po_id, actor="agent", confidence=0.9)

        assert pending.po.status == "pending_approval"
        assert [r.code for r in pending.approval_reasons] == ["VALUE_ABOVE_AUTO_APPROVE_LIMIT"]
        assert available(service.session, "REC-1012") == 5000  # nothing committed yet

        approved = service.approve(po_id, approver="buyer:ana", comment="ok, promo week")

        assert approved.po.status == "confirmed"
        assert approved.po.approval_status == "approved"
        assert available(service.session, "REC-1012") == 1400  # 5,000 - 800 * 4.50

    def test_decline_frees_the_recommendation_for_a_new_po(self, service):
        po_id = draft(service, "REC-1012").po.id
        service.submit(po_id, actor="agent")

        result = service.decline(po_id, approver="buyer:ana", comment="wait for promo price")

        assert result.po.status == "declined"
        assert available(service.session, "REC-1012") == 5000
        assert draft(service, "REC-1012", quantity=600).po.status == "draft"

    def test_low_confidence_needs_approval(self, service):
        po_id = draft(service, "REC-1001").po.id

        result = service.submit(po_id, actor="agent", confidence=0.5)

        assert result.po.status == "pending_approval"
        assert [r.code for r in result.approval_reasons] == ["LOW_CONFIDENCE"]

    def test_invalid_transitions_are_refused(self, service):
        po_id = draft(service, "REC-1001").po.id
        with pytest.raises(InvalidTransitionError):
            service.approve(po_id, approver="buyer:ana")

        service.submit(po_id, actor="agent")
        with pytest.raises(InvalidTransitionError):
            service.submit(po_id, actor="agent")

    def test_supplier_outage_leaves_the_draft_untouched(self, seeded):
        class DownGateway:
            def send_order(self, order):
                raise SupplierGatewayError("timeout")

        service = PurchaseOrderService(seeded, today=TODAY, gateway=DownGateway())
        po_id = draft(service, "REC-1001").po.id

        with pytest.raises(SupplierUnavailableError):
            service.submit(po_id, actor="agent")

        assert service.get(po_id).status == "draft"
        assert available(seeded, "REC-1001") == 5000


class TestSupplierDeviations:
    def test_price_increase_breaks_budget_and_amend_fixes_it(self, service):
        po_id = draft(service, "REC-1010").po.id  # $2,000 against $2,200 available

        confirmed = service.submit(po_id, actor="agent")

        assert confirmed.po.status == "confirmed"
        assert confirmed.po.effective_unit_cost == 2.90
        assert available(service.session, "REC-1010") == -120  # 2,320 held against 2,200
        assert [f.code for f in confirmed.evaluation.hard_violations] == ["OVER_BUDGET"]

        with pytest.raises(HardConstraintError):
            service.amend(po_id, quantity=760, actor="agent")  # 760 * 2.90 = 2,204
        assert service.get(po_id).status == "confirmed"  # refused amend changes nothing

        amended = service.amend(po_id, quantity=740, actor="agent")

        assert (amended.po.status, amended.po.revision, amended.po.unit_cost) == ("draft", 2, 2.90)
        assert available(service.session, "REC-1010") == 2200

        final = service.submit(po_id, actor="agent")

        assert final.po.status == "confirmed"
        assert final.po.value == 2146.0
        assert final.evaluation.feasible
        assert events(service.session, po_id) == [
            "created",
            "submitted",
            "acknowledged",
            "amended",
            "submitted",
            "acknowledged",
        ]

    def test_partial_fill_is_recorded_and_counted(self, service):
        po_id = draft(service, "REC-1011").po.id

        result = service.submit(po_id, actor="agent")

        assert result.po.status == "partially_confirmed"
        assert result.po.outstanding_quantity == 500
        assert available(service.session, "REC-1011") == 3750  # only 500 * 2.50 held
        assert [f.code for f in result.evaluation.soft_warnings] == ["UNDER_COVERAGE"]

        rec = service.session.get(Recommendation, "REC-1011")
        ctx = build_context(
            service.session,
            sku=rec.sku,
            node_id=rec.node_id,
            supplier_id=rec.supplier_id,
            today=TODAY,
        )
        assert [po.quantity for po in ctx.open_purchase_orders] == [500]


class TestCancel:
    def test_cancel_releases_the_budget(self, service):
        po_id = draft(service, "REC-1001").po.id
        service.submit(po_id, actor="agent")

        result = service.cancel(po_id, actor="agent", reason="found cheaper supplier")

        assert (result.po.status, result.po.committed_value) == ("cancelled", 0)
        assert available(service.session, "REC-1001") == 5000

    def test_finished_pos_cannot_change(self, service):
        po_id = draft(service, "REC-1001").po.id
        service.cancel(po_id, actor="agent", reason="test")

        with pytest.raises(InvalidTransitionError):
            service.cancel(po_id, actor="agent", reason="again")
        with pytest.raises(InvalidTransitionError):
            service.amend(po_id, quantity=600, actor="agent")
