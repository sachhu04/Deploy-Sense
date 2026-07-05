from typing import Any

"""
DeploySense — Webhook Routes

WHY THIS EXISTS:
DeploySense receives events from external systems via webhooks:
  1. GitHub: Push, PR, deployment, deployment_status events
  2. ArgoCD: Sync status notifications
  3. Prometheus Alertmanager: Alert notifications

These webhooks are the primary data ingestion mechanism.
DeploySense doesn't poll — it reacts to events.

SECURITY:
  - GitHub webhooks are validated using HMAC-SHA256 signature
  - ArgoCD webhooks are validated using shared secret
  - Alertmanager webhooks are validated by source IP (future)

ENDPOINTS (maps to architecture/03-api-definitions.md section 3.1.7):
  POST   /webhooks/github       — GitHub webhook receiver
  POST   /webhooks/argocd       — ArgoCD notification receiver
  POST   /webhooks/prometheus   — Prometheus Alertmanager receiver
"""

import hashlib
import hmac
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.api.schemas import WebhookResponse
from deploysense.core import get_settings
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import (
    Alert,
    Deployment,
    DeploymentEvent,
    PullRequest,
    Repository,
    Service,
)

logger = get_logger(__name__)

router = APIRouter()


# ─── Metrics ─────────────────────────────────────────────────────────────────
# Simple in-memory counter for webhook events. In production this would be
# a Prometheus Counter, but for MVP we use a lightweight stub to avoid
# a circular import with main.py.


class _WebhookCounter:
    """Lightweight webhook event counter (stub for Prometheus Counter)."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def labels(self, **kwargs: str) -> "_WebhookCounter":
        self._label_key = "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return self

    def inc(self) -> None:
        key = getattr(self, "_label_key", "default")
        self._counts[key] = self._counts.get(key, 0) + 1


try:
    # Use real Prometheus counter if available
    from prometheus_client import Counter

    WEBHOOK_COUNT = Counter(
        "deploysense_webhooks_total",
        "Total webhook events received",
        ["event_type"],
    )
except ImportError:
    # Fallback stub — no prometheus_client installed
    WEBHOOK_COUNT = _WebhookCounter()  # type: ignore[assignment]


def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify GitHub webhook HMAC-SHA256 signature.

    WHY: Without this, anyone can send fake webhook events to our endpoint.
    GitHub signs every webhook payload with the shared secret.
    We recompute the signature and compare.

    SECURITY: Uses hmac.compare_digest for constant-time comparison
    to prevent timing attacks.
    """
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(f"sha256={expected}", signature)


# ─── POST /webhooks/github ───────────────────────────────────────────────────


@router.post("/webhooks/github", response_model=WebhookResponse)
async def github_webhook(
    request: Request,
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_hub_signature_256: str = Header(default="", alias="X-Hub-Signature-256"),
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """
    Receive and process GitHub webhook events.

    SUPPORTED EVENTS:
      - push: Code pushed to a branch
      - pull_request: PR opened, updated, merged, closed
      - deployment: Deployment created in GitHub
      - deployment_status: Deployment status updated

    FLOW:
      1. Validate HMAC signature
      2. Parse event payload
      3. Store relevant data (PRs, commits)
      4. Trigger risk evaluation if deployment-related

    FAILURE SCENARIOS:
      - 401: Invalid signature (reject immediately)
      - 422: Unsupported event type (acknowledge but don't process)
      - 500: Database error (webhook will be retried by GitHub)
    """
    body = await request.body()
    settings = get_settings()

    # Validate signature (skip in development if no secret configured)
    if (
        settings.github_webhook_secret
        and x_hub_signature_256
        and not _verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret)
    ):
        logger.warning("github_webhook_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse payload
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    # Record metric
    WEBHOOK_COUNT.labels(event_type=x_github_event).inc()

    logger.info(
        "github_webhook_received",
        event_type=x_github_event,
        repository=payload.get("repository", {}).get("full_name", "unknown"),
    )

    # Process based on event type
    if x_github_event == "pull_request":
        await _handle_pull_request(payload, db)
    elif x_github_event == "deployment":
        await _handle_deployment_event(payload, db)
    elif x_github_event == "push":
        logger.info("github_push_event", ref=payload.get("ref", ""))
    else:
        logger.info("github_webhook_unhandled", event_type=x_github_event)

    return WebhookResponse(status="received", event_id=payload.get("delivery"))


async def _handle_pull_request(payload: dict[str, Any], db: AsyncSession) -> None:
    """
    Process pull_request webhook event.

    FLOW:
      1. Resolve the repository from the webhook payload
      2. Extract PR metadata (files, lines, author)
      3. Detect migration and infrastructure changes from file paths
      4. Upsert PR record in pull_requests table

    WHY upsert: GitHub sends multiple events for the same PR
    (opened, synchronize, closed, merged). We update the existing
    record rather than creating duplicates.
    """
    from deploysense.integrations.github_client import detect_infra_change, detect_migration

    pr_data = payload.get("pull_request", {})
    action = payload.get("action", "")
    repo_data = payload.get("repository", {})

    pr_number = pr_data.get("number")
    repo_full_name = repo_data.get("full_name", "")

    logger.info(
        "github_pr_processing",
        action=action,
        pr_number=pr_number,
        repository=repo_full_name,
    )

    # Only process meaningful actions
    if action not in ("opened", "synchronize", "closed", "reopened"):
        logger.info("github_pr_action_skipped", action=action)
        return

    # Resolve repository in our database
    owner, repo_name = repo_full_name.split("/", 1) if "/" in repo_full_name else ("", "")
    repo_result = await db.execute(
        select(Repository).where(
            Repository.owner == owner,
            Repository.repository_name == repo_name,
        )
    )
    repository = repo_result.scalar_one_or_none()

    # If we don't track this repository, log and skip
    if not repository:
        logger.info("github_pr_repo_not_tracked", repository=repo_full_name)
        return

    # Detect migration and infra changes from file list embedded in PR data
    # GitHub webhook PR payload doesn't include full file list, but we can
    # detect from the PR's changed_files count and title heuristics.
    # For full file-level detection, the worker sync job handles it.
    pr_files = pr_data.get("files", [])
    has_migration = detect_migration(pr_files) if pr_files else False
    has_infra = detect_infra_change(pr_files) if pr_files else False

    # Parse timestamps - strip timezone to store as naive UTC (DB uses TIMESTAMP WITHOUT TIME ZONE)
    created_at_str = pr_data.get("created_at", "")
    if created_at_str:
        created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    else:
        created_at = datetime.utcnow()

    merged_at = None
    if pr_data.get("merged_at"):
        merged_at = datetime.fromisoformat(pr_data["merged_at"].replace("Z", "+00:00")).replace(
            tzinfo=None
        )

    # Check if PR already exists (upsert pattern)
    existing_result = await db.execute(
        select(PullRequest).where(
            PullRequest.repository_id == repository.id,
            PullRequest.pr_number == pr_number,
        )
    )
    existing_pr = existing_result.scalar_one_or_none()

    if existing_pr:
        # Update existing PR
        existing_pr.state = pr_data.get("state")
        existing_pr.title = pr_data.get("title")
        existing_pr.files_changed = pr_data.get("changed_files", existing_pr.files_changed)
        existing_pr.lines_added = pr_data.get("additions", existing_pr.lines_added)
        existing_pr.lines_deleted = pr_data.get("deletions", existing_pr.lines_deleted)
        if has_migration:
            existing_pr.has_db_migration = True
        if has_infra:
            existing_pr.has_infra_change = True
        existing_pr.merged_at = merged_at

        logger.info("github_pr_updated", pr_number=pr_number, repository=repo_full_name)
    else:
        # Create new PR record
        new_pr = PullRequest(
            repository_id=repository.id,
            pr_number=pr_number,
            title=pr_data.get("title"),
            author=pr_data.get("user", {}).get("login"),
            state=pr_data.get("state"),
            files_changed=pr_data.get("changed_files", 0),
            lines_added=pr_data.get("additions", 0),
            lines_deleted=pr_data.get("deletions", 0),
            has_db_migration=has_migration,
            has_infra_change=has_infra,
            created_at=created_at,
            merged_at=merged_at,
        )
        db.add(new_pr)

        logger.info("github_pr_created", pr_number=pr_number, repository=repo_full_name)


async def _handle_deployment_event(payload: dict[str, Any], db: AsyncSession) -> None:
    """
    Process deployment webhook event.

    FLOW:
      1. Extract deployment metadata from GitHub payload
      2. Resolve the service from repository mapping
      3. Create a Deployment record with status=PENDING
      4. Create an audit trail event
      5. Trigger risk evaluation asynchronously via the Risk Engine

    WHY we create the deployment here:
    This is the primary integration point with CI/CD. When GitHub Actions
    or another tool creates a deployment, this webhook fires and DeploySense
    starts tracking it.
    """
    deployment_data = payload.get("deployment", {})
    repo_data = payload.get("repository", {})
    sender = payload.get("sender", {})

    environment = deployment_data.get("environment", "production")
    git_sha = deployment_data.get("sha", "")
    repo_full_name = repo_data.get("full_name", "")
    deployed_by = sender.get("login", "github")

    logger.info(
        "github_deployment_processing",
        environment=environment,
        sha=git_sha,
        repository=repo_full_name,
    )

    # Resolve service from repository
    owner, repo_name = repo_full_name.split("/", 1) if "/" in repo_full_name else ("", "")
    service_id = None
    service_name = repo_name or "unknown"

    repo_result = await db.execute(
        select(Repository).where(
            Repository.owner == owner,
            Repository.repository_name == repo_name,
        )
    )
    repository = repo_result.scalar_one_or_none()

    if repository:
        # Find the first service linked to this repository
        svc_result = await db.execute(
            select(Service).where(Service.repository_id == repository.id).limit(1)
        )
        service = svc_result.scalar_one_or_none()
        if service:
            service_id = service.id
            service_name = service.name

    # Create deployment record
    deployment = Deployment(
        service_id=service_id,
        environment=environment,
        version=deployment_data.get("ref"),
        git_sha=git_sha,
        status="PENDING",
        deployed_by=deployed_by,
        initiated_at=datetime.utcnow(),
    )
    db.add(deployment)
    await db.flush()  # Get generated ID

    # Create audit trail event
    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="deployment.created",
        current_state="PENDING",
        message=f"Deployment initiated via GitHub webhook for {service_name} to {environment}",
    )
    db.add(event)

    logger.info(
        "github_deployment_created",
        deployment_id=str(deployment.id),
        service=service_name,
        environment=environment,
        git_sha=git_sha,
    )

    # Trigger risk evaluation asynchronously
    # Fire-and-forget: we don't block the webhook response on risk evaluation.
    # If the Risk Engine is down, the deployment is still recorded.
    settings = get_settings()
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.risk_engine_url}/internal/risk/evaluate",
                json={
                    "deployment_id": str(deployment.id),
                    "service_name": service_name,
                    "environment": environment,
                    "git_sha": git_sha,
                    "files_changed": None,
                    "lines_added": None,
                    "lines_deleted": None,
                    "has_db_migration": False,
                    "has_infra_change": False,
                },
                timeout=5.0,
            )
            logger.info("risk_evaluation_triggered", deployment_id=str(deployment.id))
    except Exception as e:
        # Risk Engine being unavailable should NOT fail the webhook
        logger.warning(
            "risk_evaluation_trigger_failed",
            deployment_id=str(deployment.id),
            error=str(e),
        )


# ─── POST /webhooks/argocd ───────────────────────────────────────────────────


@router.post("/webhooks/argocd", response_model=WebhookResponse)
async def argocd_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """
    Receive ArgoCD sync notifications.

    PURPOSE: When ArgoCD deploys a new version, DeploySense updates
    the deployment status and starts the post-deployment monitoring window.

    ARGOCD SYNC STATUSES → DEPLOYSENSE STATES:
      Syncing    → DEPLOYING  (deployment is being rolled out)
      Synced     → DEPLOYED   (deployment is live, start monitoring)
      Degraded   → DEGRADED   (deployment has issues)
      Unknown    → logged but no state change

    HOW WE FIND THE DEPLOYMENT:
    ArgoCD payloads include the app name and the revision (git SHA).
    We match on git_sha + active status to find the correct deployment.
    """
    payload = await request.json()

    app_data = payload.get("app", {})
    app_name = app_data.get("metadata", {}).get("name", "unknown")
    sync_status = app_data.get("status", {}).get("sync", {}).get("status", "Unknown")
    health_status = app_data.get("status", {}).get("health", {}).get("status", "Unknown")
    revision = app_data.get("status", {}).get("sync", {}).get("revision", "")

    logger.info(
        "argocd_webhook_processing",
        app=app_name,
        sync_status=sync_status,
        health_status=health_status,
        revision=revision,
    )

    # Map ArgoCD status to DeploySense deployment state
    status_map = {
        "Syncing": "DEPLOYING",
        "Synced": "DEPLOYED",
        "Degraded": "DEGRADED",
        "OutOfSync": None,  # Don't update — this is normal pre-sync state
        "Unknown": None,
    }

    new_status = status_map.get(sync_status)

    # Also check health status for degradation
    if health_status == "Degraded":
        new_status = "DEGRADED"

    if new_status and revision:
        # Find the most recent deployment matching this git SHA
        deploy_result = await db.execute(
            select(Deployment)
            .where(
                Deployment.git_sha.ilike(f"{revision[:12]}%"),
                Deployment.status.in_(
                    ["PENDING", "RISK_ASSESSED", "APPROVED", "DEPLOYING", "DEPLOYED", "MONITORING"]
                ),
            )
            .order_by(Deployment.created_at.desc())
            .limit(1)
        )
        deployment = deploy_result.scalar_one_or_none()

        if deployment:
            previous_status = deployment.status
            deployment.status = new_status

            # Set deployed_at timestamp when transitioning to DEPLOYED
            if new_status == "DEPLOYED" and not deployment.deployed_at:
                deployment.deployed_at = datetime.utcnow()

            # Record state transition event
            event = DeploymentEvent(
                deployment_id=deployment.id,
                event_type=f"argocd.{sync_status.lower()}",
                previous_state=previous_status,
                current_state=new_status,
                message=f"ArgoCD sync status: {sync_status}, health: {health_status}",
            )
            db.add(event)

            logger.info(
                "argocd_deployment_updated",
                deployment_id=str(deployment.id),
                previous_status=previous_status,
                new_status=new_status,
                app=app_name,
            )
        else:
            logger.info(
                "argocd_no_matching_deployment",
                revision=revision,
                app=app_name,
            )

    return WebhookResponse(status="received")


# ─── POST /webhooks/prometheus ───────────────────────────────────────────────


@router.post("/webhooks/prometheus", response_model=WebhookResponse)
async def prometheus_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """
    Receive Prometheus Alertmanager notifications.

    PURPOSE: When Prometheus fires an alert (e.g., error rate spike),
    DeploySense correlates it with the active deployment to determine
    if the deployment caused the issue.

    This correlation is what separates DeploySense from a regular
    alerting system — it answers "did the deploy cause this?"

    ALERTMANAGER PAYLOAD FORMAT:
      {
        "status": "firing" | "resolved",
        "alerts": [
          {
            "status": "firing",
            "labels": {"alertname": "HighErrorRate", "service": "api", "severity": "critical"},
            "annotations": {"summary": "...", "description": "..."},
            "startsAt": "2024-01-01T00:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z"
          }
        ]
      }

    CORRELATION STRATEGY:
    For each alert, we look for the most recent active deployment
    (status in DEPLOYING, DEPLOYED, MONITORING) for the matching
    service. If found, we link the alert to that deployment.
    """
    payload = await request.json()

    alerts_data = payload.get("alerts", [])
    logger.info(
        "prometheus_webhook_processing",
        alert_count=len(alerts_data),
        status=payload.get("status", "unknown"),
    )

    created_count = 0

    for alert_data in alerts_data:
        alert_status = alert_data.get("status", "firing")
        labels = alert_data.get("labels", {})
        annotations = alert_data.get("annotations", {})

        alert_name = labels.get("alertname", "UnknownAlert")
        service_label = labels.get("service", labels.get("job", ""))
        severity = labels.get("severity", "WARNING").upper()

        # Normalize severity to our enum values
        if severity not in ("INFO", "WARNING", "HIGH", "CRITICAL"):
            severity = "WARNING"

        # Parse trigger timestamp - strip timezone to store as naive UTC
        starts_at_str = alert_data.get("startsAt", "")
        try:
            triggered_at = datetime.fromisoformat(starts_at_str.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except (ValueError, TypeError):
            triggered_at = datetime.utcnow()

        # For "resolved" alerts, update existing alert status
        if alert_status == "resolved":
            existing_alert = await db.execute(
                select(Alert)
                .where(Alert.title == alert_name, Alert.status != "RESOLVED")
                .order_by(Alert.triggered_at.desc())
                .limit(1)
            )
            alert_record = existing_alert.scalar_one_or_none()
            if alert_record:
                alert_record.status = "RESOLVED"
                alert_record.resolved_at = datetime.utcnow()
                logger.info("prometheus_alert_resolved", alert=alert_name)
            continue

        # ── Correlate with active deployment ──────────────────────────────
        # Find the service by name, then find the most recent active deployment
        deployment_id = None
        service_id = None

        if service_label:
            svc_result = await db.execute(
                select(Service).where(Service.name.ilike(f"%{service_label}%")).limit(1)
            )
            service = svc_result.scalar_one_or_none()

            if service:
                service_id = service.id

                # Find the most recent active deployment for this service
                deploy_result = await db.execute(
                    select(Deployment)
                    .where(
                        Deployment.service_id == service.id,
                        Deployment.status.in_(["DEPLOYING", "DEPLOYED", "MONITORING"]),
                    )
                    .order_by(Deployment.created_at.desc())
                    .limit(1)
                )
                deployment = deploy_result.scalar_one_or_none()
                if deployment:
                    deployment_id = deployment.id

        # Create alert record
        alert = Alert(
            service_id=service_id,
            deployment_id=deployment_id,
            severity=severity,
            title=alert_name,
            description=annotations.get("description", annotations.get("summary", "")),
            status="OPEN",
            triggered_at=triggered_at,
        )
        db.add(alert)
        created_count += 1

        logger.info(
            "prometheus_alert_created",
            alert=alert_name,
            severity=severity,
            service=service_label,
            correlated_deployment=str(deployment_id) if deployment_id else None,
        )

    logger.info(
        "prometheus_webhook_completed",
        alerts_processed=len(alerts_data),
        alerts_created=created_count,
    )

    return WebhookResponse(status="received")
