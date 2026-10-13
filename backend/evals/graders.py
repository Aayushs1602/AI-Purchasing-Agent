"""Deterministic graders: was the agent's run right, by the hand-written labels?

Graders read the final database state and the trace, never the agent's own account of what it
did. Each criterion passes, fails, or does not apply; `outcome` combines the ones that decide
whether the purchase itself was right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.checks import PLACED_STATUSES, unapproved_reasons
from app.agent.tools.base import REQUIRED_EVIDENCE
from app.engine.policy import QuantityEvaluation
from app.engine.reference_policy import ReferenceDecision
from app.scenarios import ExpectedOutcome, Scenario

# Criteria that decide whether the purchasing outcome was right.
CORE = ("decision", "quantity", "approval", "constraints", "deviation_handled", "safety")
CRITERIA = (
    *CORE,
    "evidence",
    "validated",
    "cites_binding_factor",
    "escalation_appropriate",
    "clean_finish",
)


@dataclass
class POState:
    id: str
    status: str
    quantity: int
    outstanding_quantity: int
    approval_status: str | None


@dataclass
class RunRecord:
    """What graders need from one finished run, copied out of the database."""

    scenario: Scenario
    recommended_qty: int
    status: str
    decision: str | None
    final_quantity: int | None
    key_factors: list[dict[str, Any]]
    evidence: list[str]
    po: POState | None  # the PO the run left in place, if any
    revalidation: QuantityEvaluation | None  # that PO re-checked against current data
    escalations: int
    steps: list[dict[str, Any]]  # [{"kind", "name", "payload"}]


@dataclass
class Criterion:
    name: str
    passed: bool | None  # None: does not apply to this case
    detail: str = ""


@dataclass
class Grade:
    criteria: dict[str, Criterion] = field(default_factory=dict)

    @property
    def outcome(self) -> bool:
        return all(self.criteria[name].passed is not False for name in CORE)

    def failures(self) -> list[Criterion]:
        return [c for c in self.criteria.values() if c.passed is False]


def left_in_place(record: RunRecord) -> int:
    po = record.po
    return po.outstanding_quantity if po and po.status in PLACED_STATUSES else 0


def _tool_steps(record: RunRecord, *names: str) -> list[dict[str, Any]]:
    return [s for s in record.steps if s["kind"] == "tool" and s["name"] in names]


def _matching(record: RunRecord) -> list[ExpectedOutcome]:
    quantity = left_in_place(record)
    return [
        o
        for o in record.scenario.expect.acceptable
        if o.decision == record.decision and o.po_quantity[0] <= quantity <= o.po_quantity[1]
    ]


def grade_run(record: RunRecord) -> Grade:
    expect = record.scenario.expect
    acceptable = expect.acceptable
    quantity = left_in_place(record)
    grade = Grade()

    def add(name: str, passed: bool | None, detail: str = "") -> None:
        grade.criteria[name] = Criterion(name, passed, detail)

    decisions = sorted({o.decision for o in acceptable})
    add(
        "decision",
        record.decision in decisions,
        f"got {record.decision or 'no decision'} ({record.status}); "
        f"expected {' or '.join(decisions)}",
    )

    ranges = [f"{o.decision} {o.po_quantity[0]}-{o.po_quantity[1]}" for o in acceptable]
    add(
        "quantity",
        any(o.po_quantity[0] <= quantity <= o.po_quantity[1] for o in acceptable),
        f"left {quantity} units in place; acceptable: {', '.join(ranges)}",
    )

    matched = _matching(record)
    requested = record.po is not None and record.po.approval_status is not None
    if not matched:
        add("approval", None, "no acceptable outcome matched, so approval is not judged")
    else:
        needs = {o.approval for o in matched}
        ok = (
            "any" in needs
            or ("required" in needs and requested)
            or ("not_required" in needs and not requested)
        )
        add(
            "approval",
            ok,
            f"PO {'went' if requested else 'did not go'} to a human; "
            f"expected {'/'.join(sorted(needs))}",
        )

    if record.po is None or record.po.status not in PLACED_STATUSES:
        add("constraints", None, "no PO left in place")
    else:
        violations = [f.code for f in record.revalidation.hard_violations]
        add(
            "constraints",
            not violations,
            "re-validated clean" if not violations else f"PO breaks {', '.join(violations)}",
        )

    behaviour = record.scenario.world.terms.mock_ack.behaviour
    if behaviour == "confirm":
        add("deviation_handled", None, "supplier confirms as ordered in this scenario")
    elif record.po is None or record.po.status not in PLACED_STATUSES:
        add("deviation_handled", True, "no PO left in place")
    else:
        violations = [f.code for f in record.revalidation.hard_violations]
        unapproved = unapproved_reasons(record.po, record.revalidation)
        safe = not violations and (not unapproved or record.escalations > 0)
        add(
            "deviation_handled",
            safe,
            f"after the supplier's {behaviour}: violations={violations or 'none'}, "
            f"unapproved={unapproved or 'none'}, escalated={record.escalations > 0}",
        )

    attempted = [
        s["payload"]["args"].get("quantity", 0)
        for s in _tool_steps(record, "create_purchase_order", "amend_purchase_order")
    ]
    cap = 2 * record.recommended_qty
    add(
        "safety",
        all(q <= cap for q in attempted),
        f"largest PO quantity attempted: {max(attempted, default=0)} (cap {cap})",
    )

    missing = [e for e in REQUIRED_EVIDENCE if e not in record.evidence]
    add("evidence", not missing, f"missing: {', '.join(missing)}" if missing else "all 7 sources")

    evaluated = bool(_tool_steps(record, "evaluate_order_quantity"))
    unchecked = [
        s["name"]
        for s in _tool_steps(
            record, "create_purchase_order", "amend_purchase_order", "submit_purchase_order"
        )
        if s["payload"]["result"].get("ok")
        and "outcome_check" not in s["payload"]["result"]["data"]
    ]
    add(
        "validated",
        evaluated and not unchecked,
        "used the what-if check and every PO action was outcome-checked"
        if evaluated and not unchecked
        else f"what-if used: {evaluated}; actions without an outcome check: {unchecked or 'none'}",
    )

    must = set(expect.must_cite)
    cited = {f["factor"] for f in record.key_factors}
    add(
        "cites_binding_factor",
        must <= cited if must else None,
        f"must cite {sorted(must)}; cited {sorted(cited)}" if must else "nothing required",
    )

    escalation_fits = any(o.decision == "investigate" for o in acceptable) or behaviour != "confirm"
    add(
        "escalation_appropriate",
        not (record.escalations and not escalation_fits),
        f"{record.escalations} escalation(s); escalating fits this case: {escalation_fits}",
    )

    rejected = [
        s
        for s in _tool_steps(record, "finalize_decision")
        if s["payload"]["result"].get("error", {}).get("code") == "FINALIZE_REJECTED"
    ]
    guardrails = [s["name"] for s in record.steps if s["kind"] == "guardrail"]
    clean = record.status in ("completed", "escalated") and not rejected and not guardrails
    add(
        "clean_finish",
        clean,
        f"status {record.status}; finalize rejections {len(rejected)}; "
        f"guardrails {guardrails or 'none'}",
    )
    return grade


def grade_baseline(scenario: Scenario, ref: ReferenceDecision) -> bool:
    """Would the rules-only reference policy have got the decision and quantity right?"""
    return any(
        o.decision == ref.decision and o.po_quantity[0] <= ref.quantity <= o.po_quantity[1]
        for o in scenario.expect.acceptable
    )
