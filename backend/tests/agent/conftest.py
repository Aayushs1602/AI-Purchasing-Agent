from datetime import datetime, timezone

import pytest

from app.agent.llm.base import ToolCall
from app.agent.tools import execute_tool
from app.agent.tools.base import RunContext
from app.config import get_settings
from app.engine.policy import DEFAULT_POLICY
from app.models import AgentRun, Recommendation
from app.scenarios import load_scenarios
from app.seed import seed_scenarios
from app.services.purchase_orders import PurchaseOrderService

TODAY = get_settings().sim_today


@pytest.fixture
def seeded(session):
    seed_scenarios(session, load_scenarios(get_settings().scenarios_dir), TODAY)
    return session


@pytest.fixture
def make_ctx(seeded):
    """Build a RunContext for a recommendation, as the orchestrator would."""

    def _make(rec_id: str = "REC-1001") -> RunContext:
        run = AgentRun(
            id=f"RUN-{rec_id}",
            recommendation_id=rec_id,
            status="running",
            provider="test",
            model="test",
            evidence=[],
            started_at=datetime.now(timezone.utc),
        )
        seeded.add(run)
        seeded.commit()
        return RunContext(
            session=seeded,
            run=run,
            recommendation=seeded.get(Recommendation, rec_id),
            today=TODAY,
            policy=DEFAULT_POLICY,
            po_service=PurchaseOrderService(seeded, today=TODAY),
        )

    return _make


def call(ctx: RunContext, name: str, **args) -> dict:
    """Execute one tool call the way the orchestrator does, returning its envelope."""
    return execute_tool(ctx, ToolCall(id=f"call-{name}", name=name, args=args))


def gather_evidence(ctx: RunContext) -> None:
    for name in (
        "get_inventory",
        "get_storage_capacity",
        "get_demand_forecast",
        "get_sales_history",
        "get_open_purchase_orders",
        "get_supplier_terms",
        "get_budget",
    ):
        assert call(ctx, name)["ok"], name
