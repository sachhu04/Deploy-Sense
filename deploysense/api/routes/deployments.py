"""
DeploySense — Deployment API Routes

WHY THIS EXISTS:
Deployments are the central entity in DeploySense. These endpoints let
clients (dashboard, CI/CD hooks, CLI) create, query, and manage deployments.

ENDPOINTS (maps to architecture/03-api-definitions.md section 3.1.1):
  GET    /deployments           — List deployments (paginated, filtered)
  POST   /deployments           — Register a new deployment
  GET    /deployments/{id}      — Get deployment detail
  GET    /deployments/{id}/risk — Get risk assessment for a deployment
  POST   /deployments/{id}/approve — Approve a blocked deployment
  GET    /deployments/stats     — Aggregate deployment statistics

FAILURE SCENARIOS:
  - 404: Deployment not found
  - 422: Invalid deployment data (Pydantic catches this automatically)
  - 500: Database connection failure (caught by global exception handler)
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from deploysense.api.schemas import (
    DeploymentCreate,
    DeploymentListResponse,
    DeploymentResponse,
    PaginationMeta,
    RiskHistoryResponse,
)
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import Deployment, DeploymentEvent, RiskAssessment, Service

logger = get_logger(__name__)

router = APIRouter()


# ─── GET /deployments ────────────────────────────────────────────────────────


@router.get("/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="Filter by status"),
    environment: str | None = Query(default=None, description="Filter by environment"),
    db: AsyncSession = Depends(get_db_session),
) -> DeploymentListResponse:
    """
    List deployments with pagination and optional filters.

    PURPOSE: Dashboard landing page shows a table of recent deployments.
    Supports filtering by status and environment for quick triage.

    INDEXING: deployments(status) and deployments(created_at DESC) ensure
    these queries are fast even with millions of rows.
    """
    query = select(Deployment)

    if status:
        query = query.where(Deployment.status == status)
    if environment:
        query = query.where(Deployment.environment == environment)

    # Count total for pagination
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Fetch page
    query = query.options(selectinload(Deployment.service)).order_by(Deployment.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    deployments = result.scalars().all()

    return DeploymentListResponse(
        data=[DeploymentResponse.model_validate(d) for d in deployments],
        pagination=PaginationMeta(page=page, per_page=per_page, total=total),
    )


# ─── POST /deployments ──────────────────────────────────────────────────────


@router.post("/deployments", response_model=DeploymentResponse, status_code=201)
async def create_deployment(
    body: DeploymentCreate,
    db: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    """
    Register a new deployment.

    PURPOSE: CI/CD pipeline calls this when a deployment is initiated.
    DeploySense doesn't execute the deploy — it records and analyzes it.

    FLOW:
      1. Create deployment record with status=PENDING
      2. Create deployment_event for audit trail
      3. Return deployment (risk evaluation happens asynchronously)

    WHY ASYNC RISK: Risk evaluation may take 100ms-2s depending on
    data availability. We don't want to block the CI/CD pipeline.
    The dashboard will show the risk score once computed.
    """
    # Look up or create the service
    service_query = select(Service).where(Service.name == body.service_name)
    service_result = await db.execute(service_query)
    service = service_result.scalar_one_or_none()

    deployment = Deployment(
        service=service,
        environment=body.environment,
        version=body.version,
        git_sha=body.git_sha,
        status="PENDING",
        deployed_by=body.deployed_by,
        initiated_at=datetime.utcnow(),
    )
    db.add(deployment)
    await db.flush()  # Get the generated ID

    # Audit trail: record the creation event
    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="deployment.created",
        current_state="PENDING",
        message=f"Deployment initiated for {body.service_name} to {body.environment}",
    )
    db.add(event)

    logger.info(
        "deployment_created",
        deployment_id=str(deployment.id),
        service=body.service_name,
        environment=body.environment,
        git_sha=body.git_sha,
    )

    return DeploymentResponse.model_validate(deployment)


# ─── GET /deployments/{id} ───────────────────────────────────────────────────


@router.get("/deployments/{deployment_id:uuid}", response_model=DeploymentResponse)
async def get_deployment(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    """
    Get deployment detail by ID.

    FAILURE: Returns 404 if deployment not found.
    """
    result = await db.execute(
        select(Deployment)
        .options(selectinload(Deployment.service))
        .where(Deployment.id == deployment_id)
    )
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    return DeploymentResponse.model_validate(deployment)


# ─── GET /deployments/{id}/risk ──────────────────────────────────────────────


@router.get("/deployments/{deployment_id:uuid}/risk", response_model=list[RiskHistoryResponse])
async def get_deployment_risk(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> list[RiskHistoryResponse]:
    """
    Get risk assessment history for a deployment.

    PURPOSE: Show all risk evaluations for a deployment, including
    how the score changed over time (e.g., after code review was completed,
    the risk score might decrease).

    WHY A LIST: A deployment can be risk-assessed multiple times.
    Initial assessment, re-assessment after PR update, manual override.
    """
    # Verify deployment exists
    deploy_result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
    if not deploy_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Deployment not found")

    result = await db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.deployment_id == deployment_id)
        .order_by(RiskAssessment.created_at.desc())
    )
    assessments = result.scalars().all()

    return [RiskHistoryResponse.model_validate(a) for a in assessments]


# ─── POST /deployments/{id}/approve ──────────────────────────────────────────


@router.post("/deployments/{deployment_id:uuid}/approve", response_model=DeploymentResponse)
async def approve_deployment(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    """
    Manually approve a blocked deployment.

    PURPOSE: When risk score is HIGH (51-75), the deployment requires
    manual approval. A team lead reviews the risk factors and approves.

    TRADEOFF: No auth check yet — in production this requires RBAC
    (only leads/admins can approve). For MVP, any authenticated user can.

    FAILURE:
      - 404: Deployment not found
      - 409: Deployment is not in a state that can be approved
    """
    result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status not in ("BLOCKED", "RISK_ASSESSED", "PENDING"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve deployment in {deployment.status} state",
        )

    previous_status = deployment.status
    deployment.status = "APPROVED"

    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="deployment.approved",
        previous_state=previous_status,
        current_state="APPROVED",
        message="Deployment manually approved",
    )
    db.add(event)

    logger.info("deployment_approved", deployment_id=str(deployment_id))

    return DeploymentResponse.model_validate(deployment)


# ─── GET /deployments/stats ──────────────────────────────────────────────────


@router.get("/deployments/stats")
async def deployment_stats(
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Aggregate deployment statistics.

    PURPOSE: Dashboard overview panel showing total deployments,
    success rate, rollback count, etc.

    FUTURE: This will return DORA metrics (deployment frequency,
    lead time, MTTR, change failure rate) once we have enough data.
    """
    total_result = await db.execute(select(func.count(Deployment.id)))
    total = total_result.scalar() or 0

    stable_result = await db.execute(
        select(func.count(Deployment.id)).where(Deployment.status == "STABLE")
    )
    stable = stable_result.scalar() or 0

    failed_result = await db.execute(
        select(func.count(Deployment.id)).where(Deployment.status.in_(["FAILED", "ROLLED_BACK"]))
    )
    failed = failed_result.scalar() or 0

    return {
        "total_deployments": total,
        "stable": stable,
        "failed": failed,
        "success_rate": round(stable / total, 4) if total > 0 else 0.0,
    }


# ─── GET /deployments/{id}/timeline ──────────────────────────────────────────


@router.get("/deployments/{deployment_id:uuid}/timeline")
async def get_deployment_timeline(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    """
    Get the full event timeline for a deployment.

    PURPOSE: Shows the complete lifecycle of a deployment:
      PENDING → RISK_ASSESSED → APPROVED → DEPLOYING → DEPLOYED →
      MONITORING → STABLE

    Each event includes the state transition, timestamp, and message.
    This is the "deployment story" — essential for post-incident reviews.

    WHY NOT return DeploymentEvent models directly:
    The timeline should be human-readable. We transform events into
    a flat list with formatted timestamps and clear messages.
    """
    # Verify deployment exists
    deploy_result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
    if not deploy_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Deployment not found")

    result = await db.execute(
        select(DeploymentEvent)
        .where(DeploymentEvent.deployment_id == deployment_id)
        .order_by(DeploymentEvent.created_at.asc())
    )
    events = result.scalars().all()

    return [
        {
            "id": str(e.id),
            "event_type": e.event_type,
            "previous_state": e.previous_state,
            "current_state": e.current_state,
            "message": e.message,
            "timestamp": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


# ─── POST /deployments/{id}/rollback ────────────────────────────────────────


@router.post("/deployments/{deployment_id:uuid}/rollback", response_model=DeploymentResponse)
async def rollback_deployment(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    """
    Initiate a deployment rollback.

    PURPOSE: When a deployment causes issues (error rate spike, latency
    increase), the operator triggers a rollback. DeploySense records the
    decision and updates the deployment state.

    WHAT THIS DOES:
      1. Validates the deployment can be rolled back
      2. Updates status to ROLLED_BACK
      3. Records the rollback event in the audit trail
      4. Sets completed_at timestamp

    WHAT THIS DOES NOT DO:
      DeploySense does NOT execute the actual rollback.
      The CI/CD system (ArgoCD, GitHub Actions) handles that.
      DeploySense records the decision and tracks the outcome.

    VALID STATES for rollback:
      DEPLOYING, DEPLOYED, MONITORING, DEGRADED

    INVALID STATES:
      PENDING (nothing to roll back)
      STABLE (no reason to roll back)
      ROLLED_BACK (already rolled back)
      FAILED (already failed)
    """
    result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    rollback_allowed = {"DEPLOYING", "DEPLOYED", "MONITORING", "DEGRADED"}
    if deployment.status not in rollback_allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot rollback deployment in {deployment.status} state. "
            f"Rollback is allowed from: {', '.join(sorted(rollback_allowed))}",
        )

    previous_status = deployment.status
    deployment.status = "ROLLED_BACK"
    deployment.completed_at = datetime.utcnow()

    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="deployment.rolled_back",
        previous_state=previous_status,
        current_state="ROLLED_BACK",
        message=f"Deployment rolled back from {previous_status} state",
    )
    db.add(event)

    logger.info(
        "deployment_rolled_back",
        deployment_id=str(deployment_id),
        previous_status=previous_status,
    )

    return DeploymentResponse.model_validate(deployment)


# ─── POST /deployments/{id}/evaluate-risk ────────────────────────────────────


@router.post("/deployments/{deployment_id:uuid}/evaluate-risk")
async def evaluate_deployment_risk(
    deployment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Trigger risk evaluation for a deployment.

    PURPOSE: This is the proxy endpoint that calls the Risk Engine service.
    The API Service doesn't compute risk itself — it delegates to the
    Risk Engine via REST (architecture/02-microservices.md section 2.4).

    FLOW:
      1. Look up the deployment and its service
      2. Look up related PR data for risk features (files changed, migrations)
      3. Call POST /internal/risk/evaluate on the Risk Engine
      4. Return the risk evaluation result

    WHY A PROXY:
      - API Service handles auth, Risk Engine doesn't
      - Single external API surface (clients don't need to know about internal services)
      - API Service enriches the request with data from its own DB

    FAILURE:
      - 404: Deployment not found
      - 502: Risk Engine unavailable (circuit breaker in production)
    """
    import httpx

    from deploysense.core import get_settings
    from deploysense.models import PullRequest

    result = await db.execute(select(Deployment).where(Deployment.id == deployment_id))
    deployment = result.scalar_one_or_none()

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # ── Resolve service name from service_id ─────────────────────────────
    service_name = "unknown"
    repository_id = None

    if deployment.service_id:
        svc_result = await db.execute(select(Service).where(Service.id == deployment.service_id))
        service = svc_result.scalar_one_or_none()
        if service:
            service_name = service.name
            repository_id = service.repository_id

    # ── Look up related PR data for risk features ────────────────────────
    # Find the most recent merged PR in this repository to gather
    # files_changed, lines_added/deleted, migration and infra flags.
    # These are the primary static risk signals.
    files_changed = None
    lines_added = None
    lines_deleted = None
    has_db_migration = False
    has_infra_change = False

    if repository_id:
        pr_result = await db.execute(
            select(PullRequest)
            .where(
                PullRequest.repository_id == repository_id,
                PullRequest.state == "closed",
                PullRequest.merged_at.isnot(None),
            )
            .order_by(PullRequest.merged_at.desc())
            .limit(1)
        )
        latest_pr = pr_result.scalar_one_or_none()

        if latest_pr:
            files_changed = latest_pr.files_changed
            lines_added = latest_pr.lines_added
            lines_deleted = latest_pr.lines_deleted
            has_db_migration = latest_pr.has_db_migration or False
            has_infra_change = latest_pr.has_infra_change or False

    # ── Call the Risk Engine ─────────────────────────────────────────────
    settings = get_settings()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.risk_engine_url}/internal/risk/evaluate",
                json={
                    "deployment_id": str(deployment.id),
                    "service_name": service_name,
                    "environment": deployment.environment,
                    "git_sha": deployment.git_sha,
                    "files_changed": files_changed,
                    "lines_added": lines_added,
                    "lines_deleted": lines_deleted,
                    "has_db_migration": has_db_migration,
                    "has_infra_change": has_infra_change,
                },
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json()

    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Risk Engine is unavailable. Ensure it's running on {settings.risk_engine_url}",
        ) from exc
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Risk Engine returned error: {e.response.status_code}",
        ) from e
