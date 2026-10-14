"""Build your own scenario: presets, a live engine preview, then create and run it."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.scenario_draft import DraftError, draft_from_description
from app.api.deps import get_app_settings, get_session
from app.api.runs import launch_run
from app.config import Settings
from app.custom_scenarios import CustomScenarioInput, Preset, ReviewerCall, presets
from app.models import CustomScenario, Recommendation, Supplier
from app.scenarios import NodeSpec, load_baseline_world
from app.services.custom_scenarios import (
    CustomScenarioConflict,
    InputProblems,
    Problem,
    ScenarioPreview,
    create,
    parse_input,
)
from app.services.custom_scenarios import preview as preview_scenario

router = APIRouter(prefix="/api/custom-scenarios", tags=["custom scenarios"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


class SupplierOption(BaseModel):
    id: str
    name: str
    on_time_rate: float
    fill_rate: float


class BuilderOptions(BaseModel):
    today: date
    presets: list[Preset]
    suppliers: list[SupplierOption]


class PreviewResponse(BaseModel):
    """Invalid input is a normal state while someone is typing: problems come back with 200."""

    problems: list[Problem]
    preview: ScenarioPreview | None


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=5, max_length=2000)
    base: dict[str, Any]  # the form as it is now; the description changes only what it states


class DraftResponse(BaseModel):
    input: dict[str, Any]  # for review in the form; nothing has been created
    filled: list[str]
    problems: list[Problem]
    model: str


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    # What the person expects; stored apart from the scenario, never shown to the agent.
    reviewer_call: ReviewerCall | None = None
    title: str | None = Field(None, min_length=2, max_length=120)  # inbox label only
    preset: str | None = Field(None, max_length=64)
    run: bool = True  # start the agent straight away


class Created(BaseModel):
    recommendation_id: str
    run_id: str | None


class StoredScenario(BaseModel):
    recommendation_id: str
    number: int
    title: str
    preset: str | None
    input: CustomScenarioInput
    reviewer_call: ReviewerCall | None


def store_node(settings: Settings) -> NodeSpec:
    """Custom scenarios live at the demo's dark store."""
    return load_baseline_world(settings.scenarios_dir).node


@router.get("/presets", response_model=BuilderOptions)
def get_presets(session: SessionDep, settings: SettingsDep):
    suppliers = session.scalars(select(Supplier).order_by(Supplier.name))
    return BuilderOptions(
        today=settings.sim_today,
        presets=presets(settings.scenarios_dir),
        suppliers=[
            SupplierOption(id=s.id, name=s.name, on_time_rate=s.on_time_rate, fill_rate=s.fill_rate)
            for s in suppliers
        ],
    )


@router.post("/preview", response_model=PreviewResponse)
def preview(data: Annotated[dict[str, Any], Body()], session: SessionDep, settings: SettingsDep):
    try:
        inp = parse_input(data)
        result = preview_scenario(session, inp, node=store_node(settings), today=settings.sim_today)
    except InputProblems as exc:
        return PreviewResponse(problems=exc.problems, preview=None)
    return PreviewResponse(problems=[], preview=result)


@router.post("/draft", response_model=DraftResponse)
def draft(body: DraftRequest, request: Request, session: SessionDep, settings: SettingsDep):
    """Fill the form from a description with one model call; the person reviews the result."""
    if not settings.allow_custom_scenarios:
        raise HTTPException(403, "Custom scenarios are disabled in this environment")
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(503, f"No model is configured: {request.app.state.llm_error}")
    if llm.provider == "replay":
        raise HTTPException(409, "Describing a scenario needs a live model, not replay mode.")
    suppliers = [
        {"id": s.id, "name": s.name}
        for s in session.scalars(select(Supplier).order_by(Supplier.id))
    ]
    try:
        result = draft_from_description(llm, body.description, body.base, suppliers)
    except DraftError as exc:
        raise HTTPException(502, str(exc)) from exc
    return DraftResponse(
        input=result.input, filled=result.filled, problems=result.problems, model=result.model
    )


@router.post("", status_code=201, response_model=Created)
def create_scenario(
    body: CreateRequest, request: Request, session: SessionDep, settings: SettingsDep
):
    if not settings.allow_custom_scenarios:
        raise HTTPException(403, "Custom scenarios are disabled in this environment")
    runner = None
    if body.run:
        # Check before creating anything, so a refused run leaves nothing half done.
        runner = request.app.state.runner
        if runner is None:
            raise HTTPException(503, f"Agent runs are disabled: {request.app.state.llm_error}")
        if request.app.state.llm.provider == "replay":
            raise HTTPException(
                409,
                "Custom scenarios need a live model: replay mode can only play back the "
                "recorded demo scenarios. Create it without running, or set LLM_PROVIDER=gemini.",
            )
    try:
        row = create(
            session,
            parse_input(body.input),
            reviewer_call=body.reviewer_call,
            title=body.title,
            preset=body.preset,
            node=store_node(settings),
            today=settings.sim_today,
            max_count=settings.max_custom_scenarios,
        )
    except InputProblems as exc:
        raise HTTPException(422, f"Fix the form first: {exc}") from exc
    except CustomScenarioConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    run_id = launch_run(request, runner, row.recommendation_id) if runner else None
    return Created(recommendation_id=row.recommendation_id, run_id=run_id)


@router.get("/{recommendation_id}", response_model=StoredScenario)
def get_scenario(recommendation_id: str, session: SessionDep):
    """A custom scenario's form, to duplicate it as a new one."""
    row = session.get(CustomScenario, recommendation_id)
    if row is None:
        raise HTTPException(404, f"{recommendation_id} is not a custom scenario")
    return StoredScenario(
        recommendation_id=row.recommendation_id,
        number=row.number,
        title=session.get(Recommendation, recommendation_id).title,
        preset=row.preset,
        input=CustomScenarioInput.model_validate(row.input),
        reviewer_call=row.reviewer_call,
    )
