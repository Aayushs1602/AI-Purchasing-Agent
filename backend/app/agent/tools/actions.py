"""Action tools: what the agent may change, and the fences around each change.

Fences, all enforced in code (the prompt only explains them):
- Evidence gate: no action until every required evidence category has been fetched.
- Ownership: the agent can only touch purchase orders created by its own run.
- Idempotency: a PO create is keyed by run and tool-call id, so a replayed call is a no-op.
- The PO service refuses hard violations and applies the approval policy; the agent cannot
  approve its own POs, and there is no tool to change budgets, terms or other SKUs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from sqlalchemy import func, select

from app.agent.checks import ActionExpectation, check_outcome
from app.agent.tools.base import RunContext, Tool, ToolArgs, ToolError, untrusted
from app.agent.tools.investigate import assumptions_for
from app.engine.models import Assumptions, Decision, Factor
from app.engine.policy import QuantityEvaluation
from app.models import Escalation, PurchaseOrder
from app.services.purchase_orders import POResult


def require_evidence(ctx: RunContext) -> None:
    missing = ctx.missing_evidence()
    if missing:
        raise ToolError(
            "EVIDENCE_MISSING",
            "Gather this evidence before acting: " + ", ".join(missing),
            {"missing": missing},
        )


def own_po(ctx: RunContext, po_id: str) -> PurchaseOrder:
    po = ctx.session.get(PurchaseOrder, po_id)
    if po is None:
        raise ToolError("PO_NOT_FOUND", f"Purchase order {po_id} does not exist")
    if po.run_id != ctx.run.id:
        raise ToolError(
            "PERMISSION_DENIED",
            f"{po_id} was not created by this run. The agent may only change its own "
            "purchase orders; existing POs are information, not something to edit.",
        )
    return po


def validation_summary(evaluation: QuantityEvaluation | None) -> dict[str, Any] | None:
    if evaluation is None:
        return None
    p = evaluation.projection

    def brief(findings):
        return [{"code": f.code, "message": f.message} for f in findings]

    return {
        "feasible": evaluation.feasible,
        "requires_approval": evaluation.requires_approval,
        "hard_violations": brief(evaluation.hard_violations),
        "soft_warnings": brief(evaluation.soft_warnings),
        "data_quality_flags": brief(evaluation.data_quality_flags),
        "projection": {
            "net_need": p.net_need,
            "order_value": p.order_value,
            "on_hand_at_arrival": p.on_hand_at_arrival,
            "peak_on_hand_after_arrival": p.peak_on_hand_after_arrival,
            "cover_days_after_receipt": p.cover_days_after_receipt,
            "units_short_in_window": p.units_short_in_window,
        },
        "limits": evaluation.limits.model_dump(),
    }


def po_view(po: PurchaseOrder) -> dict[str, Any]:
    return {
        "po_id": po.id,
        "status": po.status,
        "revision": po.revision,
        "quantity": po.quantity,
        "unit_cost": po.unit_cost,
        "confirmed_quantity": po.confirmed_quantity,
        "confirmed_unit_cost": po.confirmed_unit_cost,
        "outstanding_quantity": po.outstanding_quantity,
        "value": po.value,
        "eta": po.eta.isoformat() if po.eta else None,
        "assumptions": po.assumptions,
        "approval_status": po.approval_status,
        "approval_reasons": [
            {"code": r["code"], "message": r["message"]} for r in po.approval_reasons or []
        ],
    }


def po_result(result: POResult, **extra: Any) -> dict[str, Any]:
    return {
        "purchase_order": po_view(result.po),
        "validation": validation_summary(result.evaluation),
        **extra,
    }


# --- Purchase orders ---------------------------------------------------------------------


class CreatePOArgs(ToolArgs):
    quantity: int = Field(ge=1, description="Units to order")
    exclude_po_ids: list[str] = Field(
        default_factory=list, description="Open POs you judged unreliable (needs approval)"
    )
    demand_basis: Literal["forecast", "recent_actuals"] = "forecast"
    note: str | None = Field(None, max_length=300, description="Short note for the buyer")
    expectation: ActionExpectation = Field(description="What you expect the draft to look like")


def create_purchase_order(ctx: RunContext, args: CreatePOArgs) -> dict:
    require_evidence(ctx)
    rec = ctx.recommendation
    result = ctx.po_service.create_draft(
        sku=rec.sku,
        node_id=rec.node_id,
        supplier_id=rec.supplier_id,
        quantity=args.quantity,
        created_by=ctx.actor,
        recommendation_id=rec.id,
        run_id=ctx.run.id,
        assumptions=assumptions_for(ctx, args.exclude_po_ids, args.demand_basis),
        idempotency_key=f"{ctx.run.id}:{ctx.call_id}",
        note=args.note,
    )
    return po_result(
        result,
        outcome_check=check_outcome(
            args.expectation, result.po, result.evaluation, sent=False
        ).model_dump(),
        next_step="Check the validation, then submit_purchase_order, or amend/cancel it.",
    )


class AmendPOArgs(ToolArgs):
    po_id: str
    quantity: int = Field(ge=1)
    exclude_po_ids: list[str] | None = Field(
        None, description="Replace the excluded POs; omit to keep the current assumptions"
    )
    demand_basis: Literal["forecast", "recent_actuals"] | None = None
    expectation: ActionExpectation = Field(description="What you expect the new revision to be")


def amend_purchase_order(ctx: RunContext, args: AmendPOArgs) -> dict:
    require_evidence(ctx)
    po = own_po(ctx, args.po_id)
    assumptions: Assumptions | None = None
    if args.exclude_po_ids is not None or args.demand_basis is not None:
        current = Assumptions(**po.assumptions) if po.assumptions else Assumptions()
        assumptions = assumptions_for(
            ctx,
            args.exclude_po_ids if args.exclude_po_ids is not None else current.exclude_po_ids,
            args.demand_basis or current.demand_basis,
        )
    result = ctx.po_service.amend(
        po.id, quantity=args.quantity, actor=ctx.actor, assumptions=assumptions
    )
    return po_result(
        result,
        outcome_check=check_outcome(
            args.expectation, result.po, result.evaluation, sent=False
        ).model_dump(),
        next_step="The amended PO is a draft again: submit it to send it.",
    )


class CancelPOArgs(ToolArgs):
    po_id: str
    reason: str = Field(min_length=5, max_length=300)


def cancel_purchase_order(ctx: RunContext, args: CancelPOArgs) -> dict:
    po = own_po(ctx, args.po_id)
    return po_result(ctx.po_service.cancel(po.id, actor=ctx.actor, reason=args.reason))


class SubmitPOArgs(ToolArgs):
    po_id: str
    confidence: float = Field(ge=0, le=1, description="Your confidence that this PO is right (0-1)")
    expectation: ActionExpectation = Field(description="What you expect the supplier to confirm")


def submit_purchase_order(ctx: RunContext, args: SubmitPOArgs) -> dict:
    require_evidence(ctx)
    po = own_po(ctx, args.po_id)
    result = ctx.po_service.submit(po.id, actor=ctx.actor, confidence=args.confidence)

    if result.po.status == "pending_approval":
        return po_result(
            result,
            outcome="PENDING_APPROVAL",
            outcome_check=check_outcome(
                args.expectation, result.po, result.evaluation, sent=False
            ).model_dump(),
            explanation="Policy requires a human to approve this PO before it is sent.",
        )

    ack = result.ack
    return po_result(
        result,
        outcome="SENT_TO_SUPPLIER",
        outcome_check=check_outcome(
            args.expectation, result.po, result.evaluation, sent=True
        ).model_dump(),
        supplier_ack={
            "status": ack.status,
            "confirmed_quantity": ack.confirmed_quantity,
            "confirmed_unit_cost": ack.confirmed_unit_cost,
            "eta": ack.eta.isoformat(),
            "message": untrusted(ack.message, "supplier order system"),
        },
        explanation=(
            "'validation' re-checks the PO as the supplier confirmed it; 'outcome_check' compares "
            "it with your expectation."
        ),
    )


# --- Escalation and decision -------------------------------------------------------------


class EscalateArgs(ToolArgs):
    reason: str = Field(min_length=10, max_length=1000)
    questions: list[str] = Field(min_length=1, max_length=5, description="What a human must answer")
    options: list[str] = Field(default_factory=list, max_length=5, description="Options you see")
    severity: Literal["low", "medium", "high"] = "medium"


def escalate_to_human(ctx: RunContext, args: EscalateArgs) -> dict:
    require_evidence(ctx)
    count = ctx.session.scalar(select(func.count()).select_from(Escalation)) or 0
    escalation = Escalation(
        id=f"ESC-{count + 1:04d}",
        run_id=ctx.run.id,
        recommendation_id=ctx.recommendation.id,
        severity=args.severity,
        reason=args.reason,
        questions=args.questions,
        options=args.options,
        created_on=ctx.today,
    )
    ctx.session.add(escalation)
    ctx.session.commit()
    return {"escalation_id": escalation.id, "status": escalation.status}


class KeyFactor(ToolArgs):
    factor: Factor
    value: float | str = Field(description="The number (from the tools) you relied on")
    impact: str = Field(max_length=200, description="How it shaped the decision")


class FinalizeArgs(ToolArgs):
    decision: Decision
    final_quantity: int = Field(
        ge=0, description="Quantity of the PO this run leaves in place (0 if none)"
    )
    rationale: str = Field(min_length=20, max_length=1500)
    key_factors: list[KeyFactor] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)
    risks: list[str] = Field(default_factory=list, max_length=5)


def finalize_decision(ctx: RunContext, args: FinalizeArgs) -> dict:
    require_evidence(ctx)
    if ctx.finalized:
        raise ToolError("ALREADY_FINALIZED", "This run already has a final decision.")
    run = ctx.run
    run.decision = args.decision
    run.final_quantity = args.final_quantity
    run.rationale = args.rationale
    run.key_factors = [factor.model_dump() for factor in args.key_factors]
    run.confidence = args.confidence
    run.risks = args.risks
    ctx.session.commit()
    ctx.finalized = True
    return {"recorded": True, "decision": args.decision}


ACTION_TOOLS = [
    Tool(
        "create_purchase_order",
        "Create a draft PO for the product under review. It is validated immediately and "
        "refused if it breaks a hard constraint. Nothing is sent until you submit it.",
        CreatePOArgs,
        create_purchase_order,
        "action",
    ),
    Tool(
        "amend_purchase_order",
        "Change the quantity (or assumptions) of a PO this run created. A PO the supplier "
        "already confirmed becomes a new draft revision that must be submitted again.",
        AmendPOArgs,
        amend_purchase_order,
        "action",
    ),
    Tool(
        "cancel_purchase_order",
        "Cancel a PO this run created, releasing any budget it holds.",
        CancelPOArgs,
        cancel_purchase_order,
        "action",
    ),
    Tool(
        "submit_purchase_order",
        "Submit a draft PO. Policy decides whether it goes to the supplier now or waits for "
        "human approval. If sent, the result includes the supplier's confirmation and a "
        "re-validation of the PO as confirmed.",
        SubmitPOArgs,
        submit_purchase_order,
        "action",
    ),
    Tool(
        "escalate_to_human",
        "Hand the case to a human buyer with specific questions and the options you see. "
        "Use it when data conflicts, no feasible order exists, or an outcome needs a "
        "decision you are not allowed to make.",
        EscalateArgs,
        escalate_to_human,
        "action",
    ),
    Tool(
        "finalize_decision",
        "Record your final decision on the recommendation. Call exactly once, at the end.",
        FinalizeArgs,
        finalize_decision,
        "terminal",
    ),
]
