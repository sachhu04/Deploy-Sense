from typing import Any

"""
DeploySense — Risk Engine API Routes (Sprint 2 Enhanced)

WHAT'S NEW IN SPRINT 2:
  - POST /internal/risk/evaluate now gathers historical features from DB
  - GET /internal/risk/trends — risk trend analysis for a service
  - GET /internal/risk/comparison — before/after deployment risk comparison
  - POST /internal/risk/check-alerts — auto-create alerts for risk increases

ENDPOINTS:
  POST /internal/risk/evaluate            — Full risk evaluation with history
  GET  /internal/risk/{deployment_id}     — Get latest risk for a deployment
  GET  /internal/risk/history/{service}   — Risk history for a service
  GET  /internal/risk/trends/{service}    — Risk trend analysis (Sprint 2)
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.api.schemas import (
    RiskEvaluationRequest,
    RiskEvaluationResponse,
    RiskHistoryResponse,
)
from deploysense.api.schemas import (
    RiskFactor as SchemaRiskFactor,
)
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import Alert, Deployment, RiskAssessment, Service
from deploysense.risk_engine.historical import (
    collect_historical_features,
    update_service_stability,
)
from deploysense.risk_engine.main import RISK_CALCULATIONS, RISK_DURATION, RISK_ERRORS
from deploysense.risk_engine.scoring import RiskFeatures, compute_enhanced_risk

logger = get_logger(__name__)

router = APIRouter()


# ─── POST /internal/risk/evaluate ────────────────────────────────────────────


@router.post("/internal/risk/evaluate", response_model=RiskEvaluationResponse)
async def evaluate_risk(
    body: RiskEvaluationRequest,
    db: AsyncSession = Depends(get_db_session),
) -> RiskEvaluationResponse:
    """
    Enhanced risk evaluation with historical context.

    SPRINT 2 CHANGES:
      1. Looks up the deployment's service_id
      2. Collects historical features (failures, frequency, stability, metrics)
      3. Combines static + historical features
      4. Runs enhanced scoring engine
      5. Stores assessment with full feature snapshot
      6. Auto-creates alerts if risk is CRITICAL
    """
    with RISK_DURATION.time():
        try:
            # Resolve service
            service_id = None
            deploy_result = await db.execute(
                select(Deployment).where(Deployment.id == body.deployment_id)
            )
            deployment = deploy_result.scalar_one_or_none()
            if deployment:
                service_id = str(deployment.service_id) if deployment.service_id else None

            # Collect historical features
            historical = await collect_historical_features(db, service_id, body.environment)

            # Build complete feature vector
            features = RiskFeatures(
                has_db_migration=body.has_db_migration,
                has_infra_change=body.has_infra_change,
                files_changed=body.files_changed or 0,
                lines_added=body.lines_added or 0,
                lines_deleted=body.lines_deleted or 0,
                **historical,
            )

            # Compute enhanced risk
            result = compute_enhanced_risk(features)

            # Store assessment
            assessment = RiskAssessment(
                deployment_id=body.deployment_id,
                risk_score=result.risk_score,
                risk_level=result.risk_level,
                failure_probability=result.failure_probability,
                recommendation=result.recommendation,
                feature_snapshot=result.feature_snapshot,
                factors={"factors": [f.to_dict() for f in result.factors]},
            )
            db.add(assessment)

            # Update deployment
            if deployment:
                deployment.risk_score = result.risk_score
                deployment.risk_level = result.risk_level
                deployment.failure_probability = result.failure_probability
                if deployment.status == "PENDING":
                    deployment.status = "RISK_ASSESSED"

                # Auto-block CRITICAL deployments
                if result.risk_level == "CRITICAL" and deployment.status in (
                    "PENDING",
                    "RISK_ASSESSED",
                ):
                    deployment.status = "BLOCKED"
                    logger.warning(
                        "deployment_auto_blocked",
                        deployment_id=str(body.deployment_id),
                        risk_score=result.risk_score,
                    )

            # Auto-alert for HIGH/CRITICAL risk
            if result.risk_level in ("HIGH", "CRITICAL"):
                alert = Alert(
                    service_id=uuid.UUID(service_id) if service_id else None,
                    deployment_id=body.deployment_id,
                    severity=result.risk_level,
                    title=f"High-risk deployment detected (score: {result.risk_score})",
                    description=(
                        f"Risk level: {result.risk_level}. "
                        f"Top factor: {result.factors[0].name if result.factors else 'unknown'}. "
                        f"Recommendation: {result.recommendation}"
                    ),
                    status="OPEN",
                    triggered_at=datetime.now(UTC),
                )
                db.add(alert)

            # Update service stability
            if service_id:
                await update_service_stability(db, service_id)

            RISK_CALCULATIONS.labels(risk_level=result.risk_level).inc()

            return RiskEvaluationResponse(
                deployment_id=body.deployment_id,
                risk_score=result.risk_score,
                risk_level=result.risk_level,
                failure_probability=result.failure_probability,
                factors=[
                    SchemaRiskFactor(
                        name=f.name,
                        contribution=f.contribution,
                        description=f.description,
                    )
                    for f in result.factors
                ],
                recommendation=result.recommendation,
            )

        except Exception as e:
            RISK_ERRORS.labels(error_type=type(e).__name__).inc()
            logger.error("risk_evaluation_failed", error=str(e))
            raise


# ─── GET /internal/risk/{deployment_id} ──────────────────────────────────────


@router.get("/internal/risk/{deployment_id}", response_model=RiskHistoryResponse)
async def get_latest_risk(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> RiskHistoryResponse:
    """Get the latest risk assessment for a deployment."""
    result = await db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.deployment_id == deployment_id)
        .order_by(RiskAssessment.created_at.desc())
        .limit(1)
    )
    assessment = result.scalar_one_or_none()

    if not assessment:
        raise HTTPException(status_code=404, detail="No risk assessment found")

    return RiskHistoryResponse.model_validate(assessment)


# ─── GET /internal/risk/history/{service} ────────────────────────────────────


@router.get("/internal/risk/history/{service_name}", response_model=list[RiskHistoryResponse])
async def get_risk_history(
    service_name: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
) -> list[RiskHistoryResponse]:
    """Risk history for a service — used by dashboard trend charts."""
    svc_result = await db.execute(select(Service).where(Service.name == service_name))
    service = svc_result.scalar_one_or_none()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    result = await db.execute(
        select(RiskAssessment)
        .join(Deployment, RiskAssessment.deployment_id == Deployment.id)
        .where(Deployment.service_id == service.id)
        .order_by(RiskAssessment.created_at.desc())
        .limit(limit)
    )
    assessments = result.scalars().all()

    return [RiskHistoryResponse.model_validate(a) for a in assessments]


# ─── GET /internal/risk/trends/{service} (Sprint 2) ─────────────────────────


@router.get("/internal/risk/trends/{service_name}")
async def get_risk_trends(
    service_name: str,
    days: int = Query(default=30, ge=1, le=90),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """
    Risk trend analysis for a service.

    PURPOSE: Dashboard trend chart showing how risk evolves over time.
    Answers: "Is this service getting riskier or safer?"

    RETURNS:
      - daily_scores: Average risk score per day
      - trend_direction: "improving", "stable", or "degrading"
      - average_score: Overall average in the period
      - max_score: Peak risk score
      - total_evaluations: Number of risk assessments
    """
    svc_result = await db.execute(select(Service).where(Service.name == service_name))
    service = svc_result.scalar_one_or_none()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    since = datetime.now(UTC) - timedelta(days=days)

    # Fetch all assessments in the period
    result = await db.execute(
        select(RiskAssessment)
        .join(Deployment, RiskAssessment.deployment_id == Deployment.id)
        .where(
            Deployment.service_id == service.id,
            RiskAssessment.created_at >= since,
        )
        .order_by(RiskAssessment.created_at.asc())
    )
    assessments = result.scalars().all()

    if not assessments:
        return {
            "service": service_name,
            "period_days": days,
            "daily_scores": [],
            "trend_direction": "stable",
            "average_score": 0,
            "max_score": 0,
            "total_evaluations": 0,
        }

    # Group by day
    daily_scores: dict[str, list[int]] = {}
    for a in assessments:
        day_key = a.created_at.strftime("%Y-%m-%d") if a.created_at else "unknown"
        daily_scores.setdefault(day_key, []).append(a.risk_score)

    daily_averages = [
        {"date": day, "average_score": round(sum(scores) / len(scores), 1), "count": len(scores)}
        for day, scores in sorted(daily_scores.items())
    ]

    all_scores = [a.risk_score for a in assessments]
    avg_score = round(sum(all_scores) / len(all_scores), 1)
    max_score = max(all_scores)

    # Determine trend: compare first half average vs second half
    mid = len(all_scores) // 2
    if mid > 0:
        first_half = sum(all_scores[:mid]) / mid
        second_half = sum(all_scores[mid:]) / (len(all_scores) - mid)
        if second_half < first_half - 5:
            trend = "improving"
        elif second_half > first_half + 5:
            trend = "degrading"
        else:
            trend = "stable"
    else:
        trend = "stable"

    return {
        "service": service_name,
        "period_days": days,
        "daily_scores": daily_averages,
        "trend_direction": trend,
        "average_score": avg_score,
        "max_score": max_score,
        "total_evaluations": len(assessments),
    }
