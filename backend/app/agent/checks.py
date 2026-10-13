"""Checks that close the feedback loop.

Expectation vs outcome: every PO action carries what the agent expects (quantity, cost,
arrival, cover). After the action, the system reads the PO back, as stored or as the supplier
confirmed it, and reports each difference. The agent must react to UNEXPECTED_OUTCOME; it
cannot simply carry on with a plan the world has invalidated.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.tools.base import ToolArgs
from app.engine.policy import QuantityEvaluation
from app.models import PurchaseOrder


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
    unexpected = bool(diffs or violations)
    return OutcomeCheck(
        status="UNEXPECTED_OUTCOME" if unexpected else "AS_EXPECTED",
        differences=diffs,
        constraint_violations=violations,
        next_actions=_next_actions(sent=sent, violations=violations) if unexpected else [],
    )


def _next_actions(*, sent: bool, violations: list[str]) -> list[str]:
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
    if not violations:
        actions.append("accept the deviation and explain it in finalize_decision")
    return actions
