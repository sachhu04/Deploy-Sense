from typing import Any

"""
DeploySense — Alerts API Routes

WHY THIS EXISTS:
Alerts are the bridge between observability and deployment intelligence.
When Prometheus fires an alert (error rate spike, latency increase),
DeploySense correlates it with the active deployment.

This correlation answers: "Did the deployment cause this alert?"

ENDPOINTS (maps to architecture/03-api-definitions.md section 3.1.5):
  GET    /alerts                   — List alerts (paginated, filtered)
  GET    /alerts/{id}              — Get alert details
  POST   /alerts/{id}/acknowledge  — Acknowledge an alert
  POST   /alerts/{id}/resolve      — Resolve an alert
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.api.auth import get_current_user
from deploysense.api.schemas import AlertResponse, PaginationMeta
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import Alert, User

logger = get_logger(__name__)

router = APIRouter()


# ─── Response Schema ─────────────────────────────────────────────────────────


class AlertListResponse:
    """Not using Pydantic here — returning dict for flexibility."""

    pass


# ─── GET /alerts ─────────────────────────────────────────────────────────────


@router.get("/alerts")
async def list_alerts(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="Filter: OPEN, ACKNOWLEDGED, RESOLVED"),
    severity: str | None = Query(default=None, description="Filter: INFO, WARNING, HIGH, CRITICAL"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """
    List alerts with pagination and optional filters.

    PURPOSE: Alert feed in the dashboard. Shows all active alerts
    with ability to filter by severity and status.

    INDEXING: alerts(status) and alerts(service_id) ensure fast queries.
    """
    query = select(Alert)

    if status:
        query = query.where(Alert.status == status)
    if severity:
        query = query.where(Alert.severity == severity)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Fetch page
    query = query.order_by(Alert.triggered_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    alerts = result.scalars().all()

    return {
        "data": [AlertResponse.model_validate(a).model_dump() for a in alerts],
        "pagination": PaginationMeta(page=page, per_page=per_page, total=total).model_dump(),
    }


# ─── GET /alerts/{id} ───────────────────────────────────────────────────────


@router.get("/alerts/{alert_id}", response_model=AlertResponse)
async def get_alert(
    alert_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AlertResponse:
    """Get alert details by ID."""
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return AlertResponse.model_validate(alert)


# ─── POST /alerts/{id}/acknowledge ───────────────────────────────────────────


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertResponse)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AlertResponse:
    """
    Acknowledge an alert.

    PURPOSE: When an engineer sees an alert, they acknowledge it to signal
    "I'm looking at this." This prevents multiple people from investigating
    the same alert simultaneously.

    STATE MACHINE:
      OPEN → ACKNOWLEDGED → RESOLVED
      Cannot acknowledge if already RESOLVED.
    """
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    if alert.status == "RESOLVED":
        raise HTTPException(status_code=409, detail="Alert is already resolved")

    alert.status = "ACKNOWLEDGED"

    logger.info(
        "alert_acknowledged",
        alert_id=str(alert_id),
        acknowledged_by=user.github_username,
    )

    return AlertResponse.model_validate(alert)


# ─── POST /alerts/{id}/resolve ───────────────────────────────────────────────


@router.post("/alerts/{alert_id}/resolve", response_model=AlertResponse)
async def resolve_alert(
    alert_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AlertResponse:
    """
    Resolve an alert.

    PURPOSE: Mark an alert as resolved after the issue is fixed.
    Sets resolved_at timestamp for MTTR calculation.

    FUTURE: Auto-resolve alerts when metrics return to normal
    after a rollback or hotfix.
    """
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    if alert.status == "RESOLVED":
        raise HTTPException(status_code=409, detail="Alert is already resolved")

    alert.status = "RESOLVED"
    alert.resolved_at = datetime.utcnow()

    logger.info(
        "alert_resolved",
        alert_id=str(alert_id),
        resolved_by=user.github_username,
    )

    return AlertResponse.model_validate(alert)
