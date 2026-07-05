from typing import Any

"""
DeploySense — Audit Logging (Phase 3)

WHY THIS EXISTS:
Production systems need an immutable audit trail of WHO did WHAT and WHEN.
Compliance, incident response, and debugging all depend on audit logs.

WHAT GETS AUDITED:
  - Authentication events (login, logout, token refresh)
  - Deployment lifecycle (create, approve, rollback, block)
  - Risk evaluations (who triggered, what was the result)
  - Configuration changes (user role changes, org settings)
  - Alert acknowledgements and resolutions
  - AI analysis requests

DESIGN DECISIONS:

  1. Separate table (not application logs):
     Audit logs have different retention (years, not days),
     different access patterns (search by user/resource),
     and different compliance requirements.

  2. Immutable records (no UPDATE, no DELETE):
     Audit logs are append-only. This ensures tamper-proof records.

  3. JSONB for metadata:
     Different event types have different metadata shapes.
     JSONB allows flexible storage without schema migrations.

  4. Async write (non-blocking):
     Audit logging should never slow down API responses.
     We fire-and-forget the DB insert using a background task.

FUTURE:
  - Ship audit logs to external storage (S3, CloudWatch Logs)
  - Add digital signatures for tamper detection
  - SIEM integration (Splunk, Datadog)
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.logging import get_logger
from deploysense.models import AuditLog

logger = get_logger(__name__)


# ─── Audit Event Types ──────────────────────────────────────────────────────


class AuditAction:
    """
    Constants for audit event types.

    WHY constants (not enum): These are just strings stored in the DB.
    An enum would require code changes for new event types.
    Constants are flexible while providing IDE autocomplete.
    """

    # Auth
    LOGIN = "auth.login"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token_refresh"

    # Deployments
    DEPLOYMENT_CREATED = "deployment.created"
    DEPLOYMENT_APPROVED = "deployment.approved"
    DEPLOYMENT_BLOCKED = "deployment.blocked"
    DEPLOYMENT_ROLLBACK = "deployment.rollback"
    DEPLOYMENT_STATUS_CHANGED = "deployment.status_changed"

    # Risk
    RISK_EVALUATED = "risk.evaluated"
    RISK_AUTO_BLOCKED = "risk.auto_blocked"

    # AI
    AI_ANALYSIS_REQUESTED = "ai.analysis_requested"
    AI_ANALYSIS_COMPLETED = "ai.analysis_completed"

    # Alerts
    ALERT_ACKNOWLEDGED = "alert.acknowledged"
    ALERT_RESOLVED = "alert.resolved"

    # Configuration
    USER_ROLE_CHANGED = "config.user_role_changed"
    REPO_CONNECTED = "config.repo_connected"
    REPO_DISCONNECTED = "config.repo_disconnected"

    # Service
    SERVICE_CREATED = "service.created"
    SERVICE_STATUS_CHANGED = "service.status_changed"


# ─── Audit Log Writer ───────────────────────────────────────────────────────


async def record_audit(
    db: AsyncSession,
    action: str,
    actor_id: str | None = None,
    actor_name: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """
    Write an audit log entry.

    USAGE:
        await record_audit(
            db=db,
            action=AuditAction.DEPLOYMENT_CREATED,
            actor_id=str(user.id),
            actor_name=user.github_username,
            resource_type="deployment",
            resource_id=str(deployment.id),
            metadata={"environment": "production", "version": "1.2.3"},
        )

    WHY async:
    The audit write is a DB insert. Using async ensures we don't block
    the response while writing the audit log.
    """
    from deploysense.models import AuditLog

    try:
        entry = AuditLog(
            action=action,
            actor_id=uuid.UUID(actor_id) if actor_id else None,
            actor_name=actor_name,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip_address=ip_address,
            timestamp=datetime.now(UTC),
        )
        db.add(entry)

        logger.info(
            "audit_recorded",
            action=action,
            actor=actor_name or actor_id,
            resource=f"{resource_type}/{resource_id}" if resource_type else None,
        )
    except Exception as e:
        # Audit logging failures should NEVER crash the main operation
        logger.error("audit_write_failed", action=action, error=str(e))


# ─── Audit Query ─────────────────────────────────────────────────────────────


async def query_audit_logs(
    db: AsyncSession,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> Sequence[AuditLog]:
    """
    Query audit logs with filters.

    Used by the admin dashboard and compliance exports.
    """
    query = select(AuditLog)

    if actor_id:
        query = query.where(AuditLog.actor_id == actor_id)
    if action:
        query = query.where(AuditLog.action == action)
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    if resource_id:
        query = query.where(AuditLog.resource_id == resource_id)
    if since:
        query = query.where(AuditLog.timestamp >= since)

    query = query.order_by(AuditLog.timestamp.desc()).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()
