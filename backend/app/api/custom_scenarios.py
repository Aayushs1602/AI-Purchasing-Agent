"""Build your own scenario: presets, a live engine preview, then create and run it."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_session
from app.config import Settings
from app.custom_scenarios import Preset, presets
from app.models import Supplier
from app.scenarios import NodeSpec, load_baseline_world
from app.services.custom_scenarios import InputProblems, Problem, ScenarioPreview, parse_input
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
