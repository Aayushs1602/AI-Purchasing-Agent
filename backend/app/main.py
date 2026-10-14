"""FastAPI application factory."""

import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin_ui import mount_admin
from app.agent.llm.base import LLMClient
from app.agent.llm.factory import LLMConfigError, build_llm_client
from app.agent.orchestrator import AgentConfig, AgentRunner
from app.api import (
    admin,
    approvals,
    custom_scenarios,
    escalations,
    purchase_orders,
    recommendations,
    runs,
)
from app.background import Executor, make_executor
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.seed import ensure_database

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMClient | None = None,
    executor: Executor | None = None,
) -> FastAPI:
    """Build the app. Tests inject `llm` (e.g. a scripted client) and an inline `executor`."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = make_engine(settings.database_url)
    executor = executor or make_executor(settings.agent_workers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # First start seeds the demo scenarios; later starts keep whatever is in the database.
        ensure_database(engine, settings.scenarios_dir, settings.sim_today)
        yield
        if isinstance(executor, ThreadPoolExecutor):
            executor.shutdown(wait=False, cancel_futures=True)
        engine.dispose()

    app = FastAPI(title="AI Purchasing Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.executor = executor

    # Without an LLM the data endpoints still work; starting a run explains what is missing.
    app.state.llm_error = None
    if llm is None:
        try:
            llm = build_llm_client(settings)
        except LLMConfigError as exc:
            app.state.llm_error = str(exc)
            log.warning("Agent runs disabled: %s", exc)
    app.state.llm = llm
    app.state.runner = (
        AgentRunner(
            app.state.session_factory,
            llm,
            today=settings.sim_today,
            config=AgentConfig(max_llm_calls=settings.agent_max_llm_calls),
        )
        if llm is not None
        else None
    )

    # The demo API has no auth, so only the local UI origin is allowed to call it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "env": settings.app_env,
            "agent": "ready" if app.state.runner else "disabled",
            "agent_error": app.state.llm_error,
            # "replay" can only play back the recorded demo scenarios, not custom ones.
            "llm_provider": app.state.llm.provider if app.state.llm else None,
            "custom_scenarios": settings.allow_custom_scenarios,
        }

    app.include_router(recommendations.router)
    app.include_router(runs.router)
    app.include_router(approvals.router)
    app.include_router(escalations.router)
    app.include_router(purchase_orders.router)
    app.include_router(custom_scenarios.router)
    app.include_router(admin.router)

    if settings.admin_ui_enabled:
        mount_admin(app, engine)
    return app


app = create_app()
