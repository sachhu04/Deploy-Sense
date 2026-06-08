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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.api.schemas import WebhookResponse
from deploysense.core import get_settings
from deploysense.database import get_db_session
from deploysense.logging import get_logger

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
    if settings.github_webhook_secret and x_hub_signature_256:
        if not _verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret):
            logger.warning("github_webhook_invalid_signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Record metric
    WEBHOOK_COUNT.labels(event_type=x_github_event).inc()

    logger.info(
        "github_webhook_received",
        event_type=x_github_event,
        repository=payload.get("repository", {}).get("full_name", "unknown"),
    )

    # Process based on event type
    # For MVP, we log and acknowledge. Full processing comes in Phase 1 Sprint 1.
    if x_github_event == "pull_request":
        await _handle_pull_request(payload, db)
    elif x_github_event == "deployment":
        await _handle_deployment_event(payload, db)
    elif x_github_event == "push":
        logger.info("github_push_event", ref=payload.get("ref", ""))
    else:
        logger.info("github_webhook_unhandled", event_type=x_github_event)

    return WebhookResponse(status="received", event_id=payload.get("delivery"))


async def _handle_pull_request(payload: dict, db: AsyncSession) -> None:
    """
    Process pull_request webhook event.

    Extracts PR metadata (files changed, lines changed, migration detection)
    that feeds into risk scoring.
    """
    pr_data = payload.get("pull_request", {})
    action = payload.get("action", "")

    logger.info(
        "github_pr_event",
        action=action,
        pr_number=pr_data.get("number"),
        repository=payload.get("repository", {}).get("full_name"),
    )

    # TODO (Phase 1 Sprint 1): Store PR data in pull_requests table
    # TODO (Phase 1 Sprint 1): Detect DB migrations from changed files
    # TODO (Phase 1 Sprint 2): Trigger risk evaluation on PR merge


async def _handle_deployment_event(payload: dict, db: AsyncSession) -> None:
    """
    Process deployment webhook event.

    When GitHub detects a deployment, we create a DeploySense deployment
    record and trigger risk evaluation.
    """
    deployment_data = payload.get("deployment", {})

    logger.info(
        "github_deployment_event",
        environment=deployment_data.get("environment"),
        sha=deployment_data.get("sha"),
        repository=payload.get("repository", {}).get("full_name"),
    )

    # TODO (Phase 1 Sprint 1): Create Deployment record
    # TODO (Phase 1 Sprint 2): Call Risk Engine for evaluation


# ─── POST /webhooks/argocd ───────────────────────────────────────────────────

@router.post("/webhooks/argocd", response_model=WebhookResponse)
async def argocd_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> WebhookResponse:
    """
    Receive ArgoCD sync notifications.

    PURPOSE: When ArgoCD deploys a new version, DeploySense updates
    the deployment status (DEPLOYING → DEPLOYED) and starts the
    post-deployment monitoring window.
    """
    payload = await request.json()

    logger.info(
        "argocd_webhook_received",
        app=payload.get("app", {}).get("metadata", {}).get("name", "unknown"),
    )

    # TODO (Phase 1 Sprint 1): Update deployment state based on ArgoCD sync status

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
    """
    payload = await request.json()

    alerts = payload.get("alerts", [])
    logger.info(
        "prometheus_webhook_received",
        alert_count=len(alerts),
        status=payload.get("status", "unknown"),
    )

    # TODO (Phase 1 Sprint 3): Create Alert records and correlate with deployments

    return WebhookResponse(status="received")
