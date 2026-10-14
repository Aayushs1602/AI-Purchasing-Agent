"""Custom scenarios against the database: preview, create, and compare with "your call".

A preview seeds the scenario into a private in-memory database and runs the same code a real
run uses (seed -> build_context -> engine), so what the form shows is what the agent will
find. The live database is only read (to resolve an existing supplier). Creating adds the
scenario to the live database as one more recommendation.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.custom_scenarios import (
    CustomIds,
    CustomScenarioInput,
    ReviewerCall,
    formula_quantity,
    to_world,
)
from app.db import create_schema, make_memory_engine, make_session_factory
from app.engine.policy import (
    Finding,
    QuantityEvaluation,
    evaluate_quantity,
    recommendation_findings,
)
from app.engine.reference_policy import decide
from app.models import AgentRun, Budget, CustomScenario, Recommendation, Supplier
from app.scenarios import NodeSpec, SupplierSpec
from app.schemas import ReviewerVerdict
from app.seed import add_world
from app.services.context import context_for_recommendation, recommendation_inputs


class Problem(BaseModel):
    """One thing wrong with the form, addressed by field path (e.g. open_purchase_orders.0.note)."""

    field: str
    message: str


# Ids come from max(number) + 1: one creation at a time (the API runs as one process).
_create_lock = threading.Lock()


class CustomScenarioConflict(RuntimeError):
    """The scenario cannot be added now (limit reached, or its ids clashed)."""


class InputProblems(ValueError):
    def __init__(self, problems: list[Problem]):
        super().__init__("; ".join(f"{p.field}: {p.message}" for p in problems))
        self.problems = problems


class RulesDecision(BaseModel):
    decision: str
    quantity: int
    requires_approval: bool
    reasons: list[str]


class ScenarioPreview(BaseModel):
    """What the deterministic engine and the rules-only policy make of the situation."""

    evaluation: QuantityEvaluation  # the recommended quantity, checked against everything
    recommendation_findings: list[Finding]  # what changed since the recommendation was made
    rules: RulesDecision  # what a rules-only system would do
    formula_quantity: int  # what the recommendation's own method gives for what it saw


def problems_from(exc: ValidationError) -> list[Problem]:
    problems = []
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"]) or "(form)"
        message = error["msg"].removeprefix("Value error, ")
        problems.append(Problem(field=path, message=message))
    return problems


def parse_input(data: object) -> CustomScenarioInput:
    try:
        return CustomScenarioInput.model_validate(data)
    except ValidationError as exc:
        raise InputProblems(problems_from(exc)) from exc


def resolve_supplier(session: Session, inp: CustomScenarioInput, ids: CustomIds) -> SupplierSpec:
    """An existing supplier as stored (the form cannot change it), or the new one described."""
    if inp.supplier_id is None:
        return SupplierSpec(
            id=ids.supplier_id,
            name=inp.supplier_name,
            on_time_rate=inp.supplier_on_time_rate,
            fill_rate=inp.supplier_fill_rate,
        )
    supplier = session.get(Supplier, inp.supplier_id)
    if supplier is None:
        raise InputProblems([Problem(field="supplier_id", message="Unknown supplier")])
    return SupplierSpec(
        id=supplier.id,
        name=supplier.name,
        on_time_rate=supplier.on_time_rate,
        fill_rate=supplier.fill_rate,
    )


def unique_category(session: Session, node_id: str, period: str, base: str) -> str:
    """The category, numbered if that budget line already exists (budgets are unique per
    node, category and month). The agent sees the category, so it stays a real-sounding name."""
    taken = set(
        session.scalars(
            select(Budget.category).where(Budget.node_id == node_id, Budget.period == period)
        )
    )
    name, n = base, 2
    while name in taken:
        name, n = f"{base} {n}", n + 1
    return name


def preview(
    session: Session, inp: CustomScenarioInput, *, node: NodeSpec, today: date
) -> ScenarioPreview:
    ids = CustomIds(0)
    world = to_world(
        inp, ids, node=node, supplier=resolve_supplier(session, inp, ids), category=inp.category
    )
    engine = make_memory_engine()
    try:
        create_schema(engine)
        with make_session_factory(engine)() as scratch:
            add_world(scratch, world, scenario_id=ids.scenario, title="preview", today=today)
            rec = scratch.get(Recommendation, ids.recommendation_id)
            ctx = context_for_recommendation(scratch, rec, today)
            rules = decide(ctx, rec.quantity)
            return ScenarioPreview(
                evaluation=evaluate_quantity(ctx, rec.quantity, recommended_qty=rec.quantity),
                recommendation_findings=recommendation_findings(ctx, recommendation_inputs(rec)),
                rules=RulesDecision(
                    decision=rules.decision,
                    quantity=rules.quantity,
                    requires_approval=rules.requires_approval,
                    reasons=rules.reasons,
                ),
                formula_quantity=formula_quantity(inp),
            )
    finally:
        engine.dispose()


def create(
    session: Session,
    inp: CustomScenarioInput,
    *,
    reviewer_call: ReviewerCall | None,
    title: str | None,
    preset: str | None,
    node: NodeSpec,
    today: date,
    max_count: int,
) -> CustomScenario:
    """Add the scenario to the live database as a new recommendation, ready to run."""
    with _create_lock:
        count = session.scalar(select(func.count()).select_from(CustomScenario)) or 0
        if count >= max_count:
            raise CustomScenarioConflict(
                f"There are already {count} custom scenarios (the limit is {max_count}). "
                "Reset the demo data to start over."
            )
        number = (session.scalar(select(func.max(CustomScenario.number))) or 0) + 1
        ids = CustomIds(number)
        world = to_world(
            inp,
            ids,
            node=node,
            supplier=resolve_supplier(session, inp, ids),
            category=unique_category(session, node.id, today.strftime("%Y-%m"), inp.category),
        )
        row = CustomScenario(
            recommendation_id=ids.recommendation_id,
            number=number,
            preset=preset,
            input=inp.model_dump(mode="json"),
            reviewer_call=reviewer_call.model_dump(mode="json") if reviewer_call else None,
            created_at=datetime.now(timezone.utc),
        )
        try:
            add_world(
                session,
                world,
                scenario_id=ids.scenario,
                title=title or f"Custom: {inp.product_name}",
                today=today,
            )
            session.add(row)
            session.commit()
        except (IntegrityError, ValueError) as exc:
            session.rollback()
            raise CustomScenarioConflict(f"Could not add the scenario: {exc}") from exc
        return row


def reviewer_verdict(custom: CustomScenario | None, run: AgentRun) -> ReviewerVerdict | None:
    """The person's expected answer next to the agent's decision (None if they gave none)."""
    if custom is None or not custom.reviewer_call:
        return None
    call = ReviewerCall.model_validate(custom.reviewer_call)
    decided = run.decision is not None
    decision_match = run.decision == call.decision if decided else None
    quantity_match = None
    if decided and call.quantity_min is not None and call.quantity_max is not None:
        quantity_match = call.quantity_min <= (run.final_quantity or 0) <= call.quantity_max
    return ReviewerVerdict(
        **call.model_dump(),
        decision_match=decision_match,
        quantity_match=quantity_match,
        agrees=bool(decision_match and quantity_match is not False) if decided else None,
    )
