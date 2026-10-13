"""Read-only views of recommendations and the data behind them."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_session
from app.config import Settings
from app.engine.policy import evaluate_quantity, recommendation_findings
from app.models import AgentRun, Product, Recommendation, Supplier, SupplierTerm
from app.schemas import (
    RecommendationContext,
    RecommendationDetail,
    RecommendationSummary,
    RunSummary,
)
from app.services.context import (
    ContextNotFoundError,
    context_for_recommendation,
    recommendation_inputs,
)

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def _summary_fields(session: Session, rec: Recommendation) -> dict:
    product = session.get(Product, rec.sku)
    supplier = session.get(Supplier, rec.supplier_id)
    latest = session.scalar(
        select(AgentRun)
        .where(AgentRun.recommendation_id == rec.id)
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    return {
        "id": rec.id,
        "scenario": rec.scenario,
        "title": rec.title,
        "sku": rec.sku,
        "product_name": product.name,
        "category": product.category,
        "node_id": rec.node_id,
        "supplier_id": rec.supplier_id,
        "supplier_name": supplier.name,
        "quantity": rec.quantity,
        "created_on": rec.created_on,
        "status": rec.status,
        "latest_run": RunSummary.model_validate(latest) if latest else None,
    }


def _get_or_404(session: Session, recommendation_id: str) -> Recommendation:
    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise HTTPException(404, f"Recommendation {recommendation_id} not found")
    return rec


@router.get("", response_model=list[RecommendationSummary])
def list_recommendations(session: SessionDep):
    recs = session.scalars(select(Recommendation).order_by(Recommendation.id))
    return [RecommendationSummary(**_summary_fields(session, rec)) for rec in recs]


@router.get("/{recommendation_id}", response_model=RecommendationDetail)
def get_recommendation(recommendation_id: str, session: SessionDep):
    rec = _get_or_404(session, recommendation_id)
    return RecommendationDetail(
        **_summary_fields(session, rec), source=rec.source, method=rec.method, inputs=rec.inputs
    )


@router.get("/{recommendation_id}/context", response_model=RecommendationContext)
def get_recommendation_context(recommendation_id: str, session: SessionDep, settings: SettingsDep):
    rec = _get_or_404(session, recommendation_id)
    try:
        ctx = context_for_recommendation(session, rec, settings.sim_today)
    except ContextNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc

    terms = session.scalar(
        select(SupplierTerm).where(
            SupplierTerm.supplier_id == rec.supplier_id, SupplierTerm.sku == rec.sku
        )
    )
    return RecommendationContext(
        recommendation_id=rec.id,
        context=ctx,
        evaluation=evaluate_quantity(ctx, rec.quantity, recommended_qty=rec.quantity),
        recommendation_findings=recommendation_findings(ctx, recommendation_inputs(rec)),
        supplier_note=terms.note if terms else None,
    )
