from typing import Any

"""
DeploySense — Historical Feature Collector (Sprint 2)

WHY THIS EXISTS:
The enhanced risk engine needs historical context that isn't available
in the deployment request payload. This module queries the database
to gather deployment history, metrics baselines, and service stability.

CALLED BY:
  Risk Engine routes — before calling compute_enhanced_risk(),
  the route handler calls collect_historical_features() to gather
  database-backed features.

PERFORMANCE:
  All queries use indexed columns (service_id, created_at, status).
  Total collection time target: <50ms for all features.
  Features are cached in Redis for 5 minutes to avoid repeated queries.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.logging import get_logger
from deploysense.models import Deployment, MetricsSnapshot, Service

logger = get_logger(__name__)


async def collect_historical_features(
    db: AsyncSession,
    service_id: str | None,
    environment: str,
) -> dict[str, Any]:
    """
    Gather historical features for risk scoring.

    Returns a dict that can be unpacked into RiskFeatures(**historical).

    FEATURES COLLECTED:
      - recent_failure_count: Failures in the last 7 days
      - deployments_last_24h: Deploy frequency check
      - service_stability_score: From the service record
      - current_error_rate: Latest metrics snapshot
      - baseline_error_rate: Average error rate over last 7 days
    """
    now = datetime.now(UTC)
    features: dict[str, Any] = {
        "recent_failure_count": 0,
        "deployments_last_24h": 0,
        "service_stability_score": 100,
        "current_error_rate": 0.0,
        "baseline_error_rate": 0.0,
    }

    if not service_id:
        return features

    try:
        # ── Recent Failures (last 7 days) ────────────────────────────────
        failure_result = await db.execute(
            select(func.count(Deployment.id)).where(
                Deployment.service_id == service_id,
                Deployment.status.in_(["FAILED", "ROLLED_BACK"]),
                Deployment.created_at >= now - timedelta(days=7),
            )
        )
        features["recent_failure_count"] = failure_result.scalar() or 0

        # ── Deployment Frequency (last 24h) ──────────────────────────────
        freq_result = await db.execute(
            select(func.count(Deployment.id)).where(
                Deployment.service_id == service_id,
                Deployment.created_at >= now - timedelta(hours=24),
            )
        )
        features["deployments_last_24h"] = freq_result.scalar() or 0

        # ── Service Stability Score ──────────────────────────────────────
        svc_result = await db.execute(select(Service).where(Service.id == service_id))
        service = svc_result.scalar_one_or_none()
        if service:
            features["service_stability_score"] = service.stability_score or 100

        # ── Current Error Rate (latest snapshot) ─────────────────────────
        current_metrics = await db.execute(
            select(MetricsSnapshot)
            .where(MetricsSnapshot.service_id == service_id)
            .order_by(MetricsSnapshot.collected_at.desc())
            .limit(1)
        )
        latest = current_metrics.scalar_one_or_none()
        if latest and latest.error_rate is not None:
            features["current_error_rate"] = float(latest.error_rate)

        # ── Baseline Error Rate (7-day average) ──────────────────────────
        baseline_result = await db.execute(
            select(func.avg(MetricsSnapshot.error_rate)).where(
                MetricsSnapshot.service_id == service_id,
                MetricsSnapshot.collected_at >= now - timedelta(days=7),
            )
        )
        baseline = baseline_result.scalar()
        if baseline is not None:
            features["baseline_error_rate"] = float(baseline)

        logger.debug(
            "historical_features_collected",
            service_id=service_id,
            features=features,
        )

    except Exception as e:
        logger.error(
            "historical_features_collection_failed",
            service_id=service_id,
            error=str(e),
        )

    return features


async def update_service_stability(db: AsyncSession, service_id: str) -> int:
    """
    Recalculate and update a service's stability score.

    ALGORITHM:
      Start at 100.
      -5 for each failure in the last 30 days
      -10 for each rollback in the last 30 days
      +2 for each stable deployment in the last 30 days (max +20)
      Clamp to [0, 100]

    WHY:
    Stability score is a rolling measure of "how reliable is this service?"
    It decays with failures and recovers with successful deployments.
    Used by the Risk Engine and the dashboard health view.
    """
    now = datetime.now(UTC)
    thirty_days_ago = now - timedelta(days=30)

    score = 100

    # Failures
    fail_result = await db.execute(
        select(func.count(Deployment.id)).where(
            Deployment.service_id == service_id,
            Deployment.status == "FAILED",
            Deployment.created_at >= thirty_days_ago,
        )
    )
    failures = fail_result.scalar() or 0
    score -= failures * 5

    # Rollbacks
    rollback_result = await db.execute(
        select(func.count(Deployment.id)).where(
            Deployment.service_id == service_id,
            Deployment.status == "ROLLED_BACK",
            Deployment.created_at >= thirty_days_ago,
        )
    )
    rollbacks = rollback_result.scalar() or 0
    score -= rollbacks * 10

    # Successful deployments (bonus, capped)
    stable_result = await db.execute(
        select(func.count(Deployment.id)).where(
            Deployment.service_id == service_id,
            Deployment.status == "STABLE",
            Deployment.created_at >= thirty_days_ago,
        )
    )
    stable = stable_result.scalar() or 0
    score += min(stable * 2, 20)

    score = max(0, min(100, score))

    # Update service record
    svc_result = await db.execute(select(Service).where(Service.id == service_id))
    service = svc_result.scalar_one_or_none()
    if service:
        service.stability_score = score

    return score
