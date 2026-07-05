from typing import Any

"""
DeploySense — Dashboard Routes (Sprint 3)

WHY SERVER-RENDERED (not React/Next.js):
  1. No build step, no npm, no node_modules — simpler stack
  2. FastAPI + Jinja2 is production-ready for dashboard UIs
  3. Data is already in Python — no API-to-frontend serialization overhead
  4. SEO-irrelevant (internal tool) — no SSR/CSR tradeoff to make
  5. HTMX for interactivity — server returns HTML fragments, not JSON

FUTURE: If the dashboard needs complex client-side interactions (drag-and-drop,
real-time charts with zooming), we'll add a React frontend in Phase 3.

PAGES:
  /dashboard                    — Platform overview (deployments, risk, alerts)
  /dashboard/deployments        — Deployment list with filters
  /dashboard/deployments/{id}   — Deployment detail + timeline + risk
  /dashboard/services           — Service list with health status
  /dashboard/services/{name}    — Service detail + deployment history
  /dashboard/risk               — Risk analysis (trends, top factors)
  /dashboard/alerts             — Alert feed
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import Alert, Deployment, DeploymentEvent, RiskAssessment, Service

logger = get_logger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="deploysense/dashboard/templates")


def _database_unavailable_context(page: str, exc: Exception) -> dict[str, Any]:
    """Return shared template state when Postgres is not reachable locally."""
    logger.warning(
        "dashboard_database_unavailable",
        page=page,
        error=str(exc),
    )
    return {"database_unavailable": True}


# ─── Dashboard: Overview ─────────────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_overview(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """
    Platform overview dashboard.

    PANELS:
      - Total deployments / success rate
      - Active deployments
      - Recent risk scores
      - Open alerts
      - Deployment timeline (last 7 days)
    """
    fallback = {}
    try:
        # Stats
        total_result = await db.execute(select(func.count(Deployment.id)))
        total_deploys = total_result.scalar() or 0

        stable_result = await db.execute(
            select(func.count(Deployment.id)).where(Deployment.status == "STABLE")
        )
        stable = stable_result.scalar() or 0

        failed_result = await db.execute(
            select(func.count(Deployment.id)).where(
                Deployment.status.in_(["FAILED", "ROLLED_BACK"])
            )
        )
        failed = failed_result.scalar() or 0

        active_result = await db.execute(
            select(func.count(Deployment.id)).where(
                Deployment.status.in_(["DEPLOYING", "DEPLOYED", "MONITORING"])
            )
        )
        active = active_result.scalar() or 0

        open_alerts_result = await db.execute(
            select(func.count(Alert.id)).where(Alert.status == "OPEN")
        )
        open_alerts = open_alerts_result.scalar() or 0

        # Recent deployments
        recent_result = await db.execute(
            select(Deployment).order_by(Deployment.created_at.desc()).limit(10)
        )
        recent_deployments = recent_result.scalars().all()
    except Exception as exc:
        fallback = _database_unavailable_context("overview", exc)
        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "request": request,
                "total_deployments": 0,
                "stable_deployments": 0,
                "failed_deployments": 0,
                "active_deployments": 0,
                "open_alerts": 0,
                "success_rate": 0,
                "recent_deployments": [],
                **fallback,
            },
        )

    success_rate = round((stable / total_deploys * 100), 1) if total_deploys > 0 else 0

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "request": request,
            "total_deployments": total_deploys,
            "stable_deployments": stable,
            "failed_deployments": failed,
            "active_deployments": active,
            "open_alerts": open_alerts,
            "success_rate": success_rate,
            "recent_deployments": recent_deployments,
        },
    )


# ─── Dashboard: Deployments ──────────────────────────────────────────────────


@router.get("/dashboard/deployments", response_class=HTMLResponse)
async def dashboard_deployments(
    request: Request,
    status: str | None = Query(default=None),
    environment: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Deployment list with filters."""
    per_page = 20
    query = select(Deployment)
    fallback = {}

    if status:
        query = query.where(Deployment.status == status)
    if environment:
        query = query.where(Deployment.environment == environment)

    try:
        count_result = await db.execute(select(func.count()).select_from(query.subquery()))
        total = count_result.scalar() or 0

        query = query.order_by(Deployment.created_at.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        deployments = result.scalars().all()
    except Exception as exc:
        fallback = _database_unavailable_context("deployments", exc)
        total = 0
        deployments = []

    return templates.TemplateResponse(
        request,
        "deployments.html",
        {
            "request": request,
            "deployments": deployments,
            "total": total,
            "page": page,
            "per_page": per_page,
            "status_filter": status,
            "environment_filter": environment,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            **fallback,
        },
    )


# ─── Dashboard: Deployment Detail ────────────────────────────────────────────


@router.get("/dashboard/deployments/{deployment_id}", response_class=HTMLResponse)
async def dashboard_deployment_detail(
    request: Request,
    deployment_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Deployment detail page with timeline and risk assessment."""
    fallback = {}
    try:
        result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
        deployment = result.scalar_one_or_none()

        # Timeline events
        events_result = await db.execute(
            select(DeploymentEvent)
            .where(DeploymentEvent.deployment_id == deployment_id)
            .order_by(DeploymentEvent.created_at.asc())
        )
        events = events_result.scalars().all()

        # Risk assessments
        risk_result = await db.execute(
            select(RiskAssessment)
            .where(RiskAssessment.deployment_id == deployment_id)
            .order_by(RiskAssessment.created_at.desc())
        )
        risks = risk_result.scalars().all()
    except Exception as exc:
        fallback = _database_unavailable_context("deployment_detail", exc)
        deployment = None
        events = []
        risks = []

    return templates.TemplateResponse(
        request,
        "deployment_detail.html",
        {
            "request": request,
            "deployment": deployment,
            "events": events,
            "risks": risks,
            **fallback,
        },
    )


# ─── Dashboard: Services ─────────────────────────────────────────────────────


@router.get("/dashboard/services", response_class=HTMLResponse)
async def dashboard_services(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Service list with health status."""
    fallback = {}
    try:
        result = await db.execute(select(Service).order_by(Service.name))
        services = result.scalars().all()
    except Exception as exc:
        fallback = _database_unavailable_context("services", exc)
        services = []

    return templates.TemplateResponse(
        request,
        "services.html",
        {
            "request": request,
            "services": services,
            **fallback,
        },
    )


# ─── Dashboard: Risk Analysis ────────────────────────────────────────────────


@router.get("/dashboard/risk", response_class=HTMLResponse)
async def dashboard_risk(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Risk analysis dashboard — trends, top factors, score distribution."""
    fallback = {}
    try:
        # Recent risk assessments
        recent_result = await db.execute(
            select(RiskAssessment).order_by(RiskAssessment.created_at.desc()).limit(50)
        )
        recent_risks = recent_result.scalars().all()
    except Exception as exc:
        fallback = _database_unavailable_context("risk", exc)
        recent_risks = []

    # Score distribution
    distribution = {"LOW": 0, "MODERATE": 0, "HIGH": 0, "CRITICAL": 0}
    for r in recent_risks:
        distribution[r.risk_level] = distribution.get(r.risk_level, 0) + 1

    # Average score
    avg_score = (
        round(sum(r.risk_score for r in recent_risks) / len(recent_risks), 1) if recent_risks else 0
    )

    return templates.TemplateResponse(
        request,
        "risk.html",
        {
            "request": request,
            "recent_risks": recent_risks,
            "distribution": distribution,
            "avg_score": avg_score,
            **fallback,
        },
    )


# ─── Dashboard: Alerts ──────────────────────────────────────────────────────


@router.get("/dashboard/alerts", response_class=HTMLResponse)
async def dashboard_alerts(
    request: Request,
    status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Alert feed with status filter."""
    query = select(Alert)
    fallback = {}
    if status:
        query = query.where(Alert.status == status)
    query = query.order_by(Alert.triggered_at.desc()).limit(50)

    try:
        result = await db.execute(query)
        alerts = result.scalars().all()

        # Counts by status
        open_result = await db.execute(select(func.count(Alert.id)).where(Alert.status == "OPEN"))
        ack_result = await db.execute(
            select(func.count(Alert.id)).where(Alert.status == "ACKNOWLEDGED")
        )
        open_count = open_result.scalar() or 0
        acknowledged_count = ack_result.scalar() or 0
    except Exception as exc:
        fallback = _database_unavailable_context("alerts", exc)
        alerts = []
        open_count = 0
        acknowledged_count = 0

    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "request": request,
            "alerts": alerts,
            "status_filter": status,
            "open_count": open_count,
            "acknowledged_count": acknowledged_count,
            **fallback,
        },
    )
