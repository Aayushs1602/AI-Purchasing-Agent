"""Load scenario fixtures into the database.

python -m app.seed      # drop everything and reseed from backend/scenarios/
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import create_schema, drop_schema, make_engine, make_session_factory
from app.models import (
    SCHEMA_VERSION,
    Budget,
    DemandForecast,
    InventoryRecord,
    Node,
    Product,
    PurchaseOrder,
    Recommendation,
    SalesRecord,
    SchemaInfo,
    Supplier,
    SupplierTerm,
)
from app.scenarios import Scenario, load_scenarios

log = logging.getLogger(__name__)


def seed_scenarios(session: Session, scenarios: list[Scenario], today: date) -> None:
    """Insert every scenario's world. Parents are flushed before children (no ORM relationships)."""
    nodes, suppliers = {}, {}
    for s in scenarios:
        w = s.world
        nodes[w.node.id] = Node(**w.node.model_dump())
        suppliers[w.supplier.id] = Supplier(**w.supplier.model_dump())
        session.add(Product(**w.product.model_dump()))
    session.add_all([*nodes.values(), *suppliers.values()])
    session.flush()

    for s in scenarios:
        _seed_world(session, s, today)
    session.flush()

    for s in scenarios:
        _seed_recommendation_and_pos(session, s, today)
    session.commit()


def _seed_world(session: Session, scenario: Scenario, today: date) -> None:
    w = scenario.world
    sku, node_id = w.product.sku, w.node.id

    session.add(
        SupplierTerm(
            supplier_id=w.supplier.id,
            sku=sku,
            lead_time_days=w.terms.lead_time_days,
            moq=w.terms.moq,
            case_pack=w.terms.case_pack,
            unit_cost=w.terms.unit_cost,
            note=w.terms.note,
            mock_ack=w.terms.mock_ack.model_dump(exclude_none=True),
        )
    )

    inv = w.inventory
    session.add(
        InventoryRecord(
            sku=sku,
            node_id=node_id,
            on_hand=inv.on_hand,
            reserved=inv.reserved,
            safety_stock=inv.safety_stock,
            storage_capacity=inv.storage_capacity,
            review_period_days=inv.review_period_days,
            counted_on=None
            if inv.counted_days_ago is None
            else today - timedelta(days=inv.counted_days_ago),
        )
    )

    demand = w.demand
    session.add_all(
        DemandForecast(
            sku=sku, node_id=node_id, day=today + timedelta(days=d), units=demand.forecast_per_day
        )
        for d in range(demand.forecast_days)
    )
    # Sales run from `sales_days` ago up to yesterday, following the weekly pattern.
    recent_start = demand.sales_days - 14
    for i in range(demand.sales_days):
        pattern = demand.sales_weekly_pattern
        if demand.recent_14d_weekly_pattern and i >= recent_start:
            pattern = demand.recent_14d_weekly_pattern
        session.add(
            SalesRecord(
                sku=sku,
                node_id=node_id,
                day=today - timedelta(days=demand.sales_days - i),
                units=pattern[i % 7],
            )
        )

    session.add(
        Budget(
            node_id=node_id,
            category=w.product.category,
            period=today.strftime("%Y-%m"),
            total=w.budget.total,
            committed=w.budget.committed,
        )
    )


def _seed_recommendation_and_pos(session: Session, scenario: Scenario, today: date) -> None:
    w = scenario.world
    rec = w.recommendation
    session.add(
        Recommendation(
            id=rec.id,
            scenario=scenario.id,
            title=scenario.title,
            sku=w.product.sku,
            node_id=w.node.id,
            supplier_id=w.supplier.id,
            quantity=rec.quantity,
            created_on=today - timedelta(days=rec.created_days_ago),
            source=rec.source,
            method=rec.method,
            inputs=rec.inputs,
        )
    )
    for po in w.open_purchase_orders:
        session.add(
            PurchaseOrder(
                id=po.id,
                sku=w.product.sku,
                node_id=w.node.id,
                supplier_id=w.supplier.id,
                quantity=po.quantity,
                unit_cost=w.terms.unit_cost,
                status=po.status,
                eta=today + timedelta(days=po.eta_in_days),
                note=po.note,
                created_on=today - timedelta(days=po.created_days_ago),
                created_by=po.created_by,
            )
        )


def reset_database(engine: Engine, scenarios_dir: Path, today: date) -> int:
    """Drop all tables, recreate them and seed every scenario. Returns the scenario count."""
    scenarios = load_scenarios(scenarios_dir)
    drop_schema(engine)
    create_schema(engine)
    with make_session_factory(engine)() as session:
        seed_scenarios(session, scenarios, today)
        session.add(SchemaInfo(version=SCHEMA_VERSION))
        session.commit()
    log.info("Seeded %d scenarios", len(scenarios))
    return len(scenarios)


def stored_schema_version(engine: Engine) -> int | None:
    if not inspect(engine).has_table(SchemaInfo.__tablename__):
        return None
    with make_session_factory(engine)() as session:
        return session.scalar(select(SchemaInfo.version))


def ensure_database(engine: Engine, scenarios_dir: Path, today: date) -> None:
    """Seed on first start, rebuild if the schema changed, otherwise keep existing data."""
    stored = stored_schema_version(engine)
    if stored == SCHEMA_VERSION:
        return
    if stored is not None or inspect(engine).get_table_names():
        log.warning(
            "Database schema version %s does not match %s; rebuilding demo data",
            stored,
            SCHEMA_VERSION,
        )
    reset_database(engine, scenarios_dir, today)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    count = reset_database(
        make_engine(settings.database_url), settings.scenarios_dir, settings.sim_today
    )
    print(f"Reset {settings.database_url} with {count} scenarios.")
