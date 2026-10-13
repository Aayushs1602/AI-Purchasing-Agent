"""Demo administration: restore every scenario to its starting state."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_app_settings
from app.config import Settings
from app.seed import reset_database

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/reset")
def reset_demo_data(request: Request, settings: Annotated[Settings, Depends(get_app_settings)]):
    # Destructive: wipes all data, including POs the agent created. Demo environments only.
    if not settings.allow_demo_reset:
        raise HTTPException(403, "Demo reset is disabled in this environment")
    count = reset_database(request.app.state.engine, settings.scenarios_dir, settings.sim_today)
    return {"scenarios": count}
