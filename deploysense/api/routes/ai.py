from typing import Any

"""
DeploySense — AI Analysis API Routes (Phase 2)

ENDPOINTS (maps to architecture/03-api-definitions.md section 3.1.6):
  POST   /ai/analyze/deployment      — Trigger AI analysis for a deployment
  POST   /ai/analyze/pull-request    — Trigger AI analysis for a PR
  GET    /ai/analyses/{id}           — Get analysis result

FLOW:
  1. Client sends POST /ai/analyze/deployment with deployment_id
  2. We gather all context (risk, PR, service history, metrics)
  3. AI engine produces structured analysis
  4. Analysis is stored in ai_analyses table
  5. Client polls GET /ai/analyses/{id} or receives WebSocket notification
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.ai.engine import get_ai_engine
from deploysense.api.auth import get_current_user
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import AIAnalysis, Deployment, RiskAssessment, Service, User
from deploysense.risk_engine.historical import collect_historical_features

logger = get_logger(__name__)

router = APIRouter()


# ─── Schemas ─────────────────────────────────────────────────────────────────


class AnalyzeDeploymentRequest(BaseModel):
    deployment_id: uuid.UUID


class AnalyzePRRequest(BaseModel):
    repository_owner: str
    repository_name: str
    pr_number: int


class AnalysisResponse(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID | None
    status: str
    analysis_type: str
    summary: str | None = None
    risk_explanation: str | None = None
    root_causes: list[dict[str, Any]] | None = None
    recommendations: list[dict[str, Any]] | None = None
    failure_patterns: list[dict[str, Any]] | None = None
    confidence: float | None = None
    model_used: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── POST /ai/analyze/deployment ─────────────────────────────────────────────


@router.post("/ai/analyze/deployment", response_model=AnalysisResponse, status_code=201)
async def analyze_deployment(
    body: AnalyzeDeploymentRequest,
    db: AsyncSession = Depends(get_db_session),
) -> AnalysisResponse:
    """
    Trigger AI-powered analysis of a deployment.

    FLOW:
      1. Fetch deployment + latest risk assessment
      2. Gather historical features for the service
      3. Build analysis context
      4. Call AI engine (LLM or rule-based fallback)
      5. Store and return analysis

    WHY SYNCHRONOUS (not background job):
    Analysis takes 2-5 seconds. For MVP, the user can wait.
    In production, this becomes a background job with polling.
    """
    # Fetch deployment
    deploy_result = await db.execute(select(Deployment).where(Deployment.id == body.deployment_id))
    deployment = deploy_result.scalar_one_or_none()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Fetch latest risk assessment
    risk_result = await db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.deployment_id == body.deployment_id)
        .order_by(RiskAssessment.created_at.desc())
        .limit(1)
    )
    risk_assessment = risk_result.scalar_one_or_none()

    # Gather historical features
    service_id = str(deployment.service_id) if deployment.service_id else None
    historical = await collect_historical_features(db, service_id, deployment.environment)

    # Build context for AI engine
    context = {
        "service_name": "unknown",
        "environment": deployment.environment,
        "git_sha": deployment.git_sha,
        "risk_score": risk_assessment.risk_score if risk_assessment else 0,
        "risk_level": risk_assessment.risk_level if risk_assessment else "LOW",
        "failure_probability": risk_assessment.failure_probability if risk_assessment else 0,
        "factors": (risk_assessment.factors or {}).get("factors", []) if risk_assessment else [],
        "files_changed": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "has_db_migration": False,
        "has_infra_change": False,
        **historical,
    }

    # Resolve service name
    if service_id:
        svc_result = await db.execute(select(Service).where(Service.id == service_id))
        service = svc_result.scalar_one_or_none()
        if service:
            context["service_name"] = service.name

    # Pull feature snapshot from risk assessment
    if risk_assessment and risk_assessment.feature_snapshot:
        snap = risk_assessment.feature_snapshot
        context["files_changed"] = snap.get("files_changed", 0)
        context["lines_added"] = snap.get("lines_added", 0)
        context["lines_deleted"] = snap.get("lines_deleted", 0)
        context["has_db_migration"] = snap.get("has_db_migration", False)
        context["has_infra_change"] = snap.get("has_infra_change", False)

    # Run AI analysis
    engine = get_ai_engine()
    result = await engine.analyze_deployment(context)

    # Store analysis
    analysis = AIAnalysis(
        deployment_id=body.deployment_id,
        analysis_type="deployment",
        status="COMPLETED",
        summary=result.summary,
        risk_explanation=result.risk_explanation,
        root_causes=result.root_causes,
        recommendations=result.recommendations,
        failure_patterns=result.failure_patterns,
        confidence=result.confidence,
        model_used=result.model_used,
    )
    db.add(analysis)
    await db.flush()

    logger.info(
        "ai_analysis_created",
        analysis_id=str(analysis.id),
        deployment_id=str(body.deployment_id),
        model=result.model_used,
        triggered_by="dashboard_user",
    )

    return AnalysisResponse(
        id=analysis.id,
        deployment_id=analysis.deployment_id,
        status=analysis.status,
        analysis_type=analysis.analysis_type or "unknown",
        summary=result.summary,
        risk_explanation=result.risk_explanation,
        root_causes=result.root_causes,
        recommendations=result.recommendations,
        failure_patterns=result.failure_patterns,
        confidence=result.confidence,
        model_used=result.model_used,
        created_at=analysis.created_at,
    )


# ─── POST /ai/analyze/pull-request ──────────────────────────────────────────


@router.post("/ai/analyze/pull-request", response_model=AnalysisResponse, status_code=201)
async def analyze_pull_request(
    body: AnalyzePRRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AnalysisResponse:
    """
    Trigger AI analysis for a pull request (before deployment).

    PURPOSE: Pre-deployment risk analysis. Review PRs for risk signals
    before they're merged and deployed.

    FLOW:
      1. Fetch PR from database using repository owner/name + PR number
      2. Build analysis context from PR metadata
      3. Call AI engine with PR-specific prompt
      4. Store and return analysis
    """
    from deploysense.models import PullRequest, Repository

    # Find the repository
    repo_result = await db.execute(
        select(Repository).where(
            Repository.owner == body.repository_owner,
            Repository.repository_name == body.repository_name,
        )
    )
    repository = repo_result.scalar_one_or_none()
    if not repository:
        raise HTTPException(
            status_code=404,
            detail=f"Repository {body.repository_owner}/{body.repository_name} not found",
        )

    # Find the PR
    pr_result = await db.execute(
        select(PullRequest).where(
            PullRequest.repository_id == repository.id,
            PullRequest.pr_number == body.pr_number,
        )
    )
    pr = pr_result.scalar_one_or_none()
    if not pr:
        raise HTTPException(
            status_code=404,
            detail=f"PR #{body.pr_number} not found in {body.repository_owner}/{body.repository_name}",
        )

    # Build context for AI analysis
    context = {
        "analysis_type": "pull_request",
        "repository": f"{body.repository_owner}/{body.repository_name}",
        "pr_number": pr.pr_number,
        "pr_title": pr.title or "Untitled",
        "pr_author": pr.author or "unknown",
        "pr_state": pr.state or "unknown",
        "files_changed": pr.files_changed or 0,
        "lines_added": pr.lines_added or 0,
        "lines_deleted": pr.lines_deleted or 0,
        "has_db_migration": pr.has_db_migration,
        "has_infra_change": pr.has_infra_change,
        "service_name": repository.repository_name or body.repository_name,
    }

    # Get historical context for this service if available
    service_id = None
    if repository.id:
        svc_result = await db.execute(
            select(Service).where(Service.repository_id == repository.id).limit(1)
        )
        service = svc_result.scalar_one_or_none()
        if service:
            service_id = str(service.id)
            context["service_name"] = service.name

            # Get historical features for risk context
            historical = await collect_historical_features(db, service_id, "production")
            context.update(historical)

    # Run AI analysis using the deployment analysis with PR-specific context
    engine = get_ai_engine()
    result = await engine.analyze_pull_request(context)

    # Store analysis (not linked to a deployment since this is pre-merge)
    analysis = AIAnalysis(
        deployment_id=None,
        analysis_type="pull_request",
        status="COMPLETED",
        summary=result.summary,
        risk_explanation=result.risk_explanation,
        root_causes=result.root_causes,
        recommendations=result.recommendations,
        failure_patterns=result.failure_patterns,
        confidence=result.confidence,
        model_used=result.model_used,
    )
    db.add(analysis)
    await db.flush()

    logger.info(
        "ai_pr_analysis_created",
        analysis_id=str(analysis.id),
        repository=f"{body.repository_owner}/{body.repository_name}",
        pr_number=body.pr_number,
        model=result.model_used,
        triggered_by=user.github_username,
    )

    return AnalysisResponse(
        id=analysis.id,
        deployment_id=analysis.deployment_id,
        status=analysis.status,
        analysis_type=analysis.analysis_type or "unknown",
        summary=result.summary,
        risk_explanation=result.risk_explanation,
        root_causes=result.root_causes,
        recommendations=result.recommendations,
        failure_patterns=result.failure_patterns,
        confidence=result.confidence,
        model_used=result.model_used,
        created_at=analysis.created_at,
    )


# ─── GET /ai/analyses/{id} ──────────────────────────────────────────────────


@router.get("/ai/analyses/{analysis_id}", response_model=AnalysisResponse)
async def get_analysis(
    analysis_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AnalysisResponse:
    """Get an AI analysis result by ID."""
    result = await db.execute(select(AIAnalysis).where(AIAnalysis.id == analysis_id))
    analysis = result.scalar_one_or_none()
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    return AnalysisResponse(
        id=analysis.id,
        deployment_id=analysis.deployment_id,
        status=analysis.status,
        analysis_type=analysis.analysis_type or "unknown",
        summary=analysis.summary,
        risk_explanation=analysis.risk_explanation,
        root_causes=analysis.root_causes,
        recommendations=analysis.recommendations,
        failure_patterns=analysis.failure_patterns,
        confidence=analysis.confidence,
        model_used=analysis.model_used,
        created_at=analysis.created_at,
    )
