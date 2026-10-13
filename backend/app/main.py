"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, purchase_orders, recommendations
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.seed import ensure_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # First start seeds the demo scenarios; later starts keep whatever is in the database.
        ensure_database(engine, settings.scenarios_dir, settings.sim_today)
        yield
        engine.dispose()

    app = FastAPI(title="AI Purchasing Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    # The demo API has no auth, so only the local UI origin is allowed to call it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    app.include_router(recommendations.router)
    app.include_router(purchase_orders.router)
    app.include_router(admin.router)
    return app


app = create_app()
