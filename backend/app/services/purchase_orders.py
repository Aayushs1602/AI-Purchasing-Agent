"""Purchase order lifecycle: every way a PO can change, with validation at each step.

    draft ──submit──► submitted ──supplier ack──► confirmed | partially_confirmed
      │        └─(approval needed)─► pending_approval ──approve──► submitted
      │                                    └─decline──► declined
      └── cancel ──► cancelled          (from any live state)
    pending_approval | confirmed | partially_confirmed ──amend──► draft (new revision)

Rules enforced here rather than trusted to the agent:
- Nothing is created, amended, submitted or approved with a hard constraint violation.
- The approval policy decides whether a submission goes to the supplier or waits for a human.
- Budget is committed when a PO is sent, adjusted to what the supplier confirmed, and released
  on amend or cancel.
- At most one live PO per recommendation, and creation is idempotent by key.
- Every change is written to the PO's event log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.models import Assumptions
from app.engine.policy import (
    DEFAULT_POLICY,
    Finding,
    PolicyConfig,
    QuantityEvaluation,
    evaluate_quantity,
    submission_approval_reasons,
)
from app.models import (
    LIVE_PO_STATUSES,
    Budget,
    Product,
    PurchaseOrder,
    PurchaseOrderEvent,
    Recommendation,
    SupplierTerm,
)
from app.services.context import build_context
from app.services.supplier_gateway import (
    MockSupplierGateway,
    OrderRequest,
    SupplierAck,
    SupplierGateway,
    SupplierGatewayError,
)

TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_approval", "submitted", "cancelled"},
    "pending_approval": {"submitted", "declined", "draft", "cancelled"},
    "submitted": {"confirmed", "partially_confirmed", "cancelled"},
    "confirmed": {"draft", "cancelled", "received"},
    "partially_confirmed": {"draft", "cancelled", "received"},
}
FIRST_AGENT_PO_NUMBER = 5000


class PurchaseOrderError(Exception):
    code = "PO_ERROR"

    def __init__(self, message: str, *, evaluation: QuantityEvaluation | None = None):
        super().__init__(message)
        self.message = message
        self.evaluation = evaluation


class PONotFoundError(PurchaseOrderError):
    code = "PO_NOT_FOUND"


class InvalidTransitionError(PurchaseOrderError):
    code = "INVALID_TRANSITION"


class HardConstraintError(PurchaseOrderError):
    code = "HARD_CONSTRAINT_VIOLATED"


class LivePOExistsError(PurchaseOrderError):
    code = "LIVE_PO_EXISTS"


class SupplierUnavailableError(PurchaseOrderError):
    code = "SUPPLIER_UNAVAILABLE"


@dataclass
class POResult:
    po: PurchaseOrder
    # The PO as it now stands, re-checked against current data. None once it is finished.
    evaluation: QuantityEvaluation | None
    ack: SupplierAck | None = None
    approval_reasons: list[Finding] = field(default_factory=list)
    replayed: bool = False  # an idempotent create returned an existing PO


class PurchaseOrderService:
    def __init__(
        self,
        session: Session,
        *,
        today: date,
        gateway: SupplierGateway | None = None,
        policy: PolicyConfig = DEFAULT_POLICY,
    ):
        self.session = session
        self.today = today
        self.gateway = gateway or MockSupplierGateway(session)
        self.policy = policy

    # --- Commands ------------------------------------------------------------------------

    def create_draft(
        self,
        *,
        sku: str,
        node_id: str,
        supplier_id: str,
        quantity: int,
        created_by: str,
        recommendation_id: str | None = None,
        run_id: str | None = None,
        assumptions: Assumptions | None = None,
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> POResult:
        if idempotency_key:
            existing = self.session.scalar(
                select(PurchaseOrder).where(PurchaseOrder.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return POResult(existing, self._evaluation_if_live(existing), replayed=True)

        if recommendation_id:
            live = self.session.scalar(
                select(PurchaseOrder).where(
                    PurchaseOrder.recommendation_id == recommendation_id,
                    PurchaseOrder.status.in_(LIVE_PO_STATUSES),
                )
            )
            if live is not None:
                raise LivePOExistsError(
                    f"{live.id} is already {live.status} for {recommendation_id}; "
                    "amend or cancel it instead of creating another."
                )

        assumptions = assumptions or Assumptions()
        ctx = build_context(
            self.session, sku=sku, node_id=node_id, supplier_id=supplier_id, today=self.today
        )
        evaluation = evaluate_quantity(
            ctx,
            quantity,
            assumptions=assumptions,
            recommended_qty=self._recommended_qty(recommendation_id),
            config=self.policy,
        )
        self._refuse_hard_violations(evaluation, action="create")

        po = PurchaseOrder(
            id=self._next_id(),
            sku=sku,
            node_id=node_id,
            supplier_id=supplier_id,
            quantity=quantity,
            unit_cost=ctx.supplier.unit_cost,
            status="draft",
            note=note,
            created_on=self.today,
            created_by=created_by,
            recommendation_id=recommendation_id,
            run_id=run_id,
            assumptions=None if assumptions.is_default() else assumptions.model_dump(),
            idempotency_key=idempotency_key,
        )
        self.session.add(po)
        self.session.flush()
        self._log(po, "created", None, "draft", created_by, {"quantity": quantity})
        self.session.commit()
        return POResult(po, evaluation)

    def amend(
        self,
        po_id: str,
        *,
        quantity: int,
        actor: str,
        assumptions: Assumptions | None = None,
    ) -> POResult:
        """Change the quantity. A PO the supplier already saw becomes a new draft revision."""
        po = self._get(po_id)
        if po.status != "draft":
            self._check_transition(po, "draft", action="amend")

        assumptions = assumptions if assumptions is not None else self._assumptions(po)
        evaluation = self._evaluate(
            po, quantity, unit_cost=self._list_price(po), assumptions=assumptions
        )
        self._refuse_hard_violations(evaluation, action="amend")

        detail = {
            "from_quantity": po.outstanding_quantity,
            "to_quantity": quantity,
            "revision": po.revision + 1,
        }
        from_status = po.status
        self._set_commitment(po, 0.0)
        po.quantity = quantity
        po.unit_cost = self._list_price(po)
        po.confirmed_quantity = po.confirmed_unit_cost = po.supplier_message = po.eta = None
        po.approval_status = po.approval_reasons = po.approval_by = po.approval_comment = None
        po.assumptions = None if assumptions.is_default() else assumptions.model_dump()
        po.revision += 1
        po.status = "draft"
        self._log(po, "amended", from_status, "draft", actor, detail)
        self.session.commit()
        return POResult(po, evaluation)

    def submit(self, po_id: str, *, actor: str, confidence: float | None = None) -> POResult:
        """Send a draft to the supplier, or park it for human approval if policy requires."""
        po = self._get(po_id)
        if po.status != "draft":
            raise InvalidTransitionError(f"Cannot submit {po.id}: it is {po.status}, not draft.")

        evaluation = self.revalidate(po)
        self._refuse_hard_violations(evaluation, action="submit")

        reasons = submission_approval_reasons(evaluation, confidence, self.policy)
        if reasons:
            po.approval_status = "pending"
            po.approval_reasons = [reason.model_dump() for reason in reasons]
            self._move(
                po,
                "pending_approval",
                "approval_requested",
                actor,
                {"reasons": [r.code for r in reasons], "confidence": confidence},
            )
            self.session.commit()
            return POResult(po, evaluation, approval_reasons=reasons)

        return self._send_to_supplier(po, actor)

    def approve(self, po_id: str, *, approver: str, comment: str | None = None) -> POResult:
        po = self._get(po_id)
        if po.status != "pending_approval":
            raise InvalidTransitionError(f"Cannot approve {po.id}: it is {po.status}.")

        # Data may have moved while the PO waited (e.g. budget spent elsewhere).
        evaluation = self.revalidate(po)
        self._refuse_hard_violations(evaluation, action="approve")

        po.approval_status = "approved"
        po.approval_by = approver
        po.approval_comment = comment
        self._log(po, "approved", po.status, po.status, approver, {"comment": comment})
        return self._send_to_supplier(po, approver)

    def decline(self, po_id: str, *, approver: str, comment: str) -> POResult:
        po = self._get(po_id)
        if po.status != "pending_approval":
            raise InvalidTransitionError(f"Cannot decline {po.id}: it is {po.status}.")

        po.approval_status = "declined"
        po.approval_by = approver
        po.approval_comment = comment
        self._move(po, "declined", "declined", approver, {"comment": comment})
        self.session.commit()
        return POResult(po, None)

    def cancel(self, po_id: str, *, actor: str, reason: str) -> POResult:
        po = self._get(po_id)
        self._check_transition(po, "cancelled", action="cancel")

        released = po.committed_value
        self._set_commitment(po, 0.0)
        self._move(po, "cancelled", "cancelled", actor, {"reason": reason, "released": released})
        self.session.commit()
        return POResult(po, None)

    # --- Queries -------------------------------------------------------------------------

    def get(self, po_id: str) -> PurchaseOrder:
        return self._get(po_id)

    def revalidate(self, po: PurchaseOrder) -> QuantityEvaluation:
        """Check the PO as it stands now: confirmed quantity and price against current data."""
        return self._evaluate(
            po,
            po.outstanding_quantity,
            unit_cost=po.effective_unit_cost,
            assumptions=self._assumptions(po),
        )

    # --- Internals -----------------------------------------------------------------------

    def _send_to_supplier(self, po: PurchaseOrder, actor: str) -> POResult:
        self._move(po, "submitted", "submitted", actor, None)
        self._set_commitment(po, round(po.quantity * po.unit_cost, 2))
        request = OrderRequest(
            po_id=po.id,
            supplier_id=po.supplier_id,
            sku=po.sku,
            quantity=po.quantity,
            unit_cost=po.unit_cost,
            requested_on=self.today,
        )
        try:
            ack = self.gateway.send_order(request)
        except SupplierGatewayError as exc:
            # Nothing reached the supplier: undo the submission and the budget commitment.
            self.session.rollback()
            raise SupplierUnavailableError(f"Supplier did not accept {po.id}: {exc}") from exc

        self._apply_ack(po, ack)
        self.session.commit()
        return POResult(po, self.revalidate(po), ack=ack)

    def _apply_ack(self, po: PurchaseOrder, ack: SupplierAck) -> None:
        po.confirmed_quantity = ack.confirmed_quantity
        po.confirmed_unit_cost = ack.confirmed_unit_cost
        po.supplier_message = ack.message
        po.eta = ack.eta
        # Hold the budget for what was actually confirmed, even if that breaks the budget:
        # re-validation must see the real position, not a capped one.
        self._set_commitment(po, po.value)
        if ack.new_list_price is not None:
            self._terms(po).unit_cost = ack.new_list_price
        self._move(
            po,
            ack.status,
            "acknowledged",
            f"supplier:{po.supplier_id}",
            ack.model_dump(mode="json"),
        )

    def _evaluate(
        self, po: PurchaseOrder, quantity: int, *, unit_cost: float, assumptions: Assumptions
    ) -> QuantityEvaluation:
        """Evaluate `quantity` for this PO's SKU as if this PO were the order being placed."""
        ctx = build_context(
            self.session,
            sku=po.sku,
            node_id=po.node_id,
            supplier_id=po.supplier_id,
            today=self.today,
        )
        ctx = ctx.model_copy(
            update={
                # The PO must not count as incoming stock against itself...
                "open_purchase_orders": [p for p in ctx.open_purchase_orders if p.po_id != po.id],
                # ...nor against the budget it already holds.
                "budget_available": round(ctx.budget_available + po.committed_value, 2),
                "supplier": ctx.supplier.model_copy(update={"unit_cost": unit_cost}),
            }
        )
        return evaluate_quantity(
            ctx,
            quantity,
            assumptions=assumptions,
            recommended_qty=self._recommended_qty(po.recommendation_id),
            config=self.policy,
        )

    def _evaluation_if_live(self, po: PurchaseOrder) -> QuantityEvaluation | None:
        return self.revalidate(po) if po.status in LIVE_PO_STATUSES else None

    def _refuse_hard_violations(self, evaluation: QuantityEvaluation, *, action: str) -> None:
        if evaluation.hard_violations:
            reasons = "; ".join(f.message for f in evaluation.hard_violations)
            raise HardConstraintError(
                f"Cannot {action} {evaluation.quantity} units: {reasons}", evaluation=evaluation
            )

    def _set_commitment(self, po: PurchaseOrder, value: float) -> None:
        delta = round(value - po.committed_value, 2)
        if delta:
            budget = self._budget(po)
            if budget is None:
                raise PurchaseOrderError(f"No budget line for {po.sku} at {po.node_id}")
            budget.committed = round(budget.committed + delta, 2)
        po.committed_value = round(value, 2)

    def _move(
        self,
        po: PurchaseOrder,
        to_status: str,
        event: str,
        actor: str,
        detail: dict[str, Any] | None,
    ) -> None:
        self._check_transition(po, to_status, action=event)
        from_status = po.status
        po.status = to_status
        self._log(po, event, from_status, to_status, actor, detail)

    def _check_transition(self, po: PurchaseOrder, to_status: str, *, action: str) -> None:
        if to_status not in TRANSITIONS.get(po.status, set()):
            raise InvalidTransitionError(f"Cannot {action} {po.id}: it is {po.status}.")

    def _log(
        self,
        po: PurchaseOrder,
        event: str,
        from_status: str | None,
        to_status: str | None,
        actor: str,
        detail: dict[str, Any] | None,
    ) -> None:
        self.session.add(
            PurchaseOrderEvent(
                po_id=po.id,
                on=self.today,
                event=event,
                from_status=from_status,
                to_status=to_status,
                actor=actor,
                detail=detail,
            )
        )

    def _get(self, po_id: str) -> PurchaseOrder:
        po = self.session.get(PurchaseOrder, po_id)
        if po is None:
            raise PONotFoundError(f"Purchase order {po_id} not found")
        return po

    def _next_id(self) -> str:
        ids = self.session.scalars(select(PurchaseOrder.id)).all()
        numbers = [int(i[3:]) for i in ids if i.startswith("PO-") and i[3:].isdigit()]
        return f"PO-{max([FIRST_AGENT_PO_NUMBER, *numbers]) + 1}"

    def _terms(self, po: PurchaseOrder) -> SupplierTerm:
        terms = self.session.scalar(
            select(SupplierTerm).where(
                SupplierTerm.supplier_id == po.supplier_id, SupplierTerm.sku == po.sku
            )
        )
        if terms is None:
            raise PurchaseOrderError(f"No terms from {po.supplier_id} for {po.sku}")
        return terms

    def _list_price(self, po: PurchaseOrder) -> float:
        return self._terms(po).unit_cost

    def _budget(self, po: PurchaseOrder) -> Budget | None:
        product = self.session.get(Product, po.sku)
        return self.session.scalar(
            select(Budget).where(
                Budget.node_id == po.node_id,
                Budget.category == product.category,
                Budget.period == self.today.strftime("%Y-%m"),
            )
        )

    def _recommended_qty(self, recommendation_id: str | None) -> int | None:
        if recommendation_id is None:
            return None
        rec = self.session.get(Recommendation, recommendation_id)
        return rec.quantity if rec else None

    @staticmethod
    def _assumptions(po: PurchaseOrder) -> Assumptions:
        return Assumptions(**po.assumptions) if po.assumptions else Assumptions()
