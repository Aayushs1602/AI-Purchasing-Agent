"""Checks that close the feedback loop.

Expectation vs outcome: every PO action carries what the agent expects (quantity, cost,
arrival, cover). After the action, the system reads the PO back, as stored or as the supplier
confirmed it, and reports each difference. The agent must react to UNEXPECTED_OUTCOME; it
cannot simply carry on with a plan the world has invalidated.

Final decision: before a decision is recorded, its label must match what the run actually left
behind (consistency), and every number it cites must be one a tool returned (grounding).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.tools.base import RunContext, ToolArgs
from app.engine.policy import QuantityEvaluation
from app.models import Escalation, PurchaseOrder


class ActionExpectation(ToolArgs):
    """What the agent expects the PO to look like after the action."""

    quantity: int = Field(ge=0, description="Units you expect on the PO afterwards")
    max_total_cost: float = Field(ge=0, description="The most you expect the PO to cost")
    arrive_by: date | None = Field(None, description="Latest acceptable arrival date")
    min_cover_days: int | None = Field(None, ge=0, description="Least acceptable cover")
    max_cover_days: int | None = Field(None, ge=0, description="Most acceptable cover")


class Difference(BaseModel):
    field: str
    expected: Any
    actual: Any
    message: str


class OutcomeCheck(BaseModel):
    status: Literal["AS_EXPECTED", "UNEXPECTED_OUTCOME"]
    differences: list[Difference]
    constraint_violations: list[str]
    # Approval reasons the PO now carries that no human has signed off.
    needs_human_approval: list[str] = Field(default_factory=list)
    next_actions: list[str]


def check_outcome(
    expected: ActionExpectation,
    po: PurchaseOrder,
    evaluation: QuantityEvaluation | None,
    *,
    sent: bool,
) -> OutcomeCheck:
    """Compare the PO as it now stands with what the agent expected.

    `sent` means the supplier has acknowledged it: compare confirmed values. Otherwise the PO
    is a draft or waiting for approval: compare what is stored.
    """
    diffs: list[Difference] = []

    quantity = po.outstanding_quantity if sent else po.quantity
    if quantity != expected.quantity:
        diffs.append(
            Difference(
                field="quantity",
                expected=expected.quantity,
                actual=quantity,
                message=f"PO has {quantity} units, expected {expected.quantity}.",
            )
        )

    if sent and po.confirmed_unit_cost is not None and po.confirmed_unit_cost != po.unit_cost:
        diffs.append(
            Difference(
                field="unit_cost",
                expected=po.unit_cost,
                actual=po.confirmed_unit_cost,
                message=(
                    f"Supplier confirmed ${po.confirmed_unit_cost:.2f}/unit; the PO was sent at "
                    f"${po.unit_cost:.2f}."
                ),
            )
        )

    cost = po.value if sent else round(po.quantity * po.unit_cost, 2)
    if cost > expected.max_total_cost + 0.01:
        diffs.append(
            Difference(
                field="total_cost",
                expected=expected.max_total_cost,
                actual=cost,
                message=(
                    f"PO costs ${cost:,.2f}, above the expected ${expected.max_total_cost:,.2f}."
                ),
            )
        )

    projection = evaluation.projection if evaluation else None
    arrival = po.eta if sent else (projection.arrival_date if projection else None)
    if expected.arrive_by and arrival and arrival > expected.arrive_by:
        diffs.append(
            Difference(
                field="arrival",
                expected=expected.arrive_by.isoformat(),
                actual=arrival.isoformat(),
                message=f"Arrives {arrival.isoformat()}, after {expected.arrive_by.isoformat()}.",
            )
        )

    cover = projection.cover_days_after_receipt if projection else None
    if cover is not None:
        if expected.min_cover_days is not None and cover < expected.min_cover_days:
            diffs.append(
                Difference(
                    field="cover_days",
                    expected=f">= {expected.min_cover_days}",
                    actual=cover,
                    message=f"Covers {cover} days, less than {expected.min_cover_days}.",
                )
            )
        if expected.max_cover_days is not None and cover > expected.max_cover_days:
            diffs.append(
                Difference(
                    field="cover_days",
                    expected=f"<= {expected.max_cover_days}",
                    actual=cover,
                    message=f"Covers {cover} days, more than {expected.max_cover_days}.",
                )
            )

    violations = [f.code for f in evaluation.hard_violations] if evaluation else []
    unapproved = unapproved_reasons(po, evaluation) if sent else []
    unexpected = bool(diffs or violations)
    return OutcomeCheck(
        status="UNEXPECTED_OUTCOME" if unexpected else "AS_EXPECTED",
        differences=diffs,
        constraint_violations=violations,
        needs_human_approval=unapproved,
        next_actions=(
            _next_actions(sent=sent, violations=violations, unapproved=unapproved)
            if unexpected
            else []
        ),
    )


def unapproved_reasons(po: PurchaseOrder, evaluation: QuantityEvaluation | None) -> list[str]:
    """Approval reasons a sent PO carries that no human approved (e.g. after a partial fill)."""
    if evaluation is None or po.approval_status == "approved":
        return []
    return [f.code for f in evaluation.approval_reasons] if evaluation.requires_approval else []


def _next_actions(*, sent: bool, violations: list[str], unapproved: list[str]) -> list[str]:
    if not sent:
        return [
            "amend_purchase_order to the quantity you intended",
            "cancel_purchase_order and reconsider",
        ]
    actions = [
        "amend_purchase_order, then submit_purchase_order again",
        "cancel_purchase_order",
        "escalate_to_human",
    ]
    if unapproved:
        # The approval policy would have stopped this PO; the agent cannot wave it through.
        actions.append(
            "accepting this deviation needs a human (" + ", ".join(unapproved) + "): escalate it"
        )
    elif not violations:
        actions.append("accept the deviation and explain it in finalize_decision")
    return actions


# --- Final decision ----------------------------------------------------------------------

PLACED_STATUSES = ("pending_approval", "submitted", "confirmed", "partially_confirmed")


def decision_problems(ctx: RunContext, decision: str, final_quantity: int) -> list[str]:
    """Does the decision label match what the run actually left in the database?"""
    run_pos = ctx.session.scalars(
        select(PurchaseOrder).where(PurchaseOrder.run_id == ctx.run.id)
    ).all()
    escalated = ctx.session.scalar(select(Escalation).where(Escalation.run_id == ctx.run.id))
    placed = next((po for po in run_pos if po.status in PLACED_STATUSES), None)
    left = placed.outstanding_quantity if placed else 0
    recommended = ctx.recommendation.quantity
    problems = [
        f"{po.id} is still a draft: submit it or cancel it before finalizing."
        for po in run_pos
        if po.status == "draft"
    ]

    if final_quantity != left:
        where = f"{placed.id} has {left} units" if placed else "no PO is left in place"
        problems.append(f"final_quantity is {final_quantity} but {where}.")

    if decision == "accept":
        if placed is None:
            problems.append(f"accept needs a placed PO for the recommended {recommended} units.")
        elif left != recommended:
            problems.append(
                f"accept means the recommended {recommended} units, but {placed.id} has {left}; "
                "use modify or investigate."
            )
    elif decision == "modify":
        if placed is None:
            problems.append("modify needs a placed PO; none was placed. Use reject or investigate.")
        elif left == recommended:
            problems.append(
                f"{placed.id} has exactly the recommended {recommended} units: that is accept."
            )
    elif decision == "reject":
        if placed is not None:
            problems.append(
                f"reject means no order, but {placed.id} ({placed.status}) is still in place: "
                "cancel it or choose another decision."
            )
    elif decision == "investigate" and escalated is None:
        problems.append("investigate requires escalate_to_human with your questions first.")

    if ctx.circuit_open and decision != "investigate":
        problems.append(
            "The circuit breaker escalated this case: the decision must be investigate."
        )

    if placed is not None and escalated is None and placed.status != "pending_approval":
        evaluation = ctx.po_service.revalidate(placed)
        violations = [f.code for f in evaluation.hard_violations]
        unapproved = unapproved_reasons(placed, evaluation)
        if violations:
            problems.append(
                f"{placed.id} breaks hard constraints ({', '.join(violations)}): amend, cancel or "
                "escalate it before finalizing."
            )
        elif unapproved:
            problems.append(
                f"{placed.id}, as the supplier confirmed it, needs a human's approval "
                f"({', '.join(unapproved)}) that it never got: escalate it before finalizing."
            )
    return problems


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    cleaned = str(value)
    for token in ("$", ",", "%", "units", "days", " "):
        cleaned = cleaned.replace(token, "")
    try:
        return float(cleaned)
    except ValueError:
        return None  # qualitative value, e.g. "ignored"


def grounding_problems(ctx: RunContext, key_factors: list[Any]) -> list[str]:
    """Every number the agent cites must be one some tool returned during this run."""
    problems = []
    for factor in key_factors:
        value = _as_number(factor.value)
        if value is None:
            continue
        if not any(
            abs(value - seen) <= max(0.01, 0.005 * abs(seen)) for seen in ctx.observed_numbers
        ):
            problems.append(
                f"key factor {factor.factor}={factor.value} does not match any number the tools "
                "returned in this run."
            )
    return problems
