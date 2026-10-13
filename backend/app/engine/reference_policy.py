"""Rules-only reference policy: what a conventional replenishment system would decide.

Used in two places:
- evals: the baseline column, showing where the LLM agent beats or loses to plain rules;
- degraded mode: the fallback when the LLM is unavailable (the result always goes to a human).

It trusts the forecast and counts every open PO. It reports data-quality flags but cannot
interpret them (e.g. it cannot read that a PO is "on hold"), so it only routes them to approval.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.engine import inventory_math as im
from app.engine.models import Assumptions, Decision, PurchasingContext
from app.engine.policy import DEFAULT_POLICY, PolicyConfig, QuantityEvaluation, evaluate_quantity


class ReferenceDecision(BaseModel):
    decision: Decision
    quantity: int
    reasons: list[str]
    evaluation: QuantityEvaluation | None = None
    requires_approval: bool = False


def decide(
    ctx: PurchasingContext, recommended_qty: int, config: PolicyConfig = DEFAULT_POLICY
) -> ReferenceDecision:
    assumptions = Assumptions()
    supplier = ctx.supplier
    need = im.net_need(ctx, assumptions)

    if need == 0:
        return ReferenceDecision(
            decision="reject",
            quantity=0,
            reasons=[
                f"Inventory position {im.inventory_position(ctx, assumptions)} already meets "
                f"the order-up-to level {im.order_up_to_level(ctx, assumptions)}."
            ],
        )

    desired = im.round_up_to_valid(need, supplier.moq, supplier.case_pack)
    reasons = [f"Net need is {need} units; rounded to supplier terms that is {desired}."]
    evaluation = evaluate_quantity(ctx, desired, recommended_qty=recommended_qty, config=config)
    limits = evaluation.limits

    if evaluation.feasible:
        quantity = desired
    elif limits.max_feasible_quantity > 0:
        quantity = limits.max_feasible_quantity
        binding = [
            name
            for name, cap in (
                ("budget", limits.max_affordable_quantity),
                ("storage", limits.max_storable_quantity),
            )
            if cap < desired
        ]
        reasons.append(f"Capped at {quantity} units by {' and '.join(binding)}.")
        evaluation = evaluate_quantity(
            ctx, quantity, recommended_qty=recommended_qty, config=config
        )
    else:
        reasons.append(
            f"No order meets the minimum of {limits.min_valid_quantity} units within budget "
            f"({limits.max_affordable_quantity}) and storage ({limits.max_storable_quantity})."
        )
        return ReferenceDecision(
            decision="investigate", quantity=0, reasons=reasons, evaluation=evaluation
        )

    return ReferenceDecision(
        decision="accept" if quantity == recommended_qty else "modify",
        quantity=quantity,
        reasons=reasons,
        evaluation=evaluation,
        requires_approval=evaluation.requires_approval,
    )
