from typing import Any

"""
DeploySense — Admin API Routes (Phase 3)

ENDPOINTS:
  GET  /admin/users               — List all users (admin only)
  PATCH /admin/users/{id}/role    — Change user role (admin only)
  GET  /admin/audit               — Query audit logs (admin only)
  GET  /admin/slos                — List SLOs (admin/engineer)
  POST /admin/slos                — Create SLO (admin only)

SECURITY:
  All admin routes require admin role via RBAC.
  Audit log queries require audit:read permission.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deploysense.api.audit import AuditAction, query_audit_logs, record_audit
from deploysense.api.rbac import Permission, require_permission, require_role
from deploysense.database import get_db_session
from deploysense.logging import get_logger
from deploysense.models import Service, ServiceSLO, User

logger = get_logger(__name__)

router = APIRouter()


# ─── Schemas ─────────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    id: uuid.UUID
    github_username: str
    email: str | None = None
    role: str
    avatar_url: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleUpdateRequest(BaseModel):
    role: str


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    action: str
    actor_name: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    details: dict[str, Any] | None = None
    ip_address: str | None = None
    timestamp: datetime

    model_config = {"from_attributes": True}


class SLOCreateRequest(BaseModel):
    service_name: str
    name: str
    target: float
    metric_query: str | None = None
    window_days: int = 30


class SLOResponse(BaseModel):
    id: uuid.UUID
    name: str
    target: float
    current_value: float | None = None
    status: str
    window_days: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── GET /admin/users ────────────────────────────────────────────────────────


@router.get("/admin/users", response_model=list[UserResponse])
async def list_users(
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db_session),
) -> list[UserResponse]:
    """List all users. Admin only."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [UserResponse.model_validate(u) for u in users]


# ─── PATCH /admin/users/{id}/role ────────────────────────────────────────────


@router.patch("/admin/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: uuid.UUID,
    body: RoleUpdateRequest,
    current_user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    """
    Change a user's role. Admin only.

    ALLOWED ROLES: viewer, engineer, admin, service

    AUDIT: This action is always audit-logged because role changes
    affect what a user can do across the entire platform.
    """
    valid_roles = {"viewer", "engineer", "admin", "service"}
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(sorted(valid_roles))}",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = target_user.role
    target_user.role = body.role

    # Audit log
    await record_audit(
        db=db,
        action=AuditAction.USER_ROLE_CHANGED,
        actor_id=str(current_user.id),
        actor_name=current_user.github_username,
        resource_type="user",
        resource_id=str(user_id),
        details={"old_role": old_role, "new_role": body.role},
    )

    logger.info(
        "user_role_changed",
        target_user=target_user.github_username,
        old_role=old_role,
        new_role=body.role,
        changed_by=current_user.github_username,
    )

    return UserResponse.model_validate(target_user)


# ─── GET /admin/audit ────────────────────────────────────────────────────────


@router.get("/admin/audit", response_model=list[AuditLogResponse])
async def get_audit_logs(
    user: User = Depends(require_permission(Permission.AUDIT_READ)),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db_session),
) -> list[AuditLogResponse]:
    """
    Query audit logs. Admin only.

    FILTERS:
      - action: Filter by action type (e.g., "deployment.created")
      - resource_type: Filter by resource (e.g., "deployment", "user")
      - limit: Max results (default 50)
    """
    logs = await query_audit_logs(
        db=db,
        action=action,
        resource_type=resource_type,
        limit=limit,
    )
    return [AuditLogResponse.model_validate(log) for log in logs]


# ─── GET /admin/slos ─────────────────────────────────────────────────────────


@router.get("/admin/slos", response_model=list[SLOResponse])
async def list_slos(
    user: User = Depends(require_permission(Permission.SERVICES_READ)),
    db: AsyncSession = Depends(get_db_session),
) -> list[SLOResponse]:
    """List all SLOs."""
    result = await db.execute(select(ServiceSLO).order_by(ServiceSLO.created_at.desc()))
    slos = result.scalars().all()
    return [SLOResponse.model_validate(s) for s in slos]


# ─── POST /admin/slos ────────────────────────────────────────────────────────


@router.post("/admin/slos", response_model=SLOResponse, status_code=201)
async def create_slo(
    body: SLOCreateRequest,
    user: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db_session),
) -> SLOResponse:
    """
    Create a Service Level Objective. Admin only.

    EXAMPLE:
      POST /admin/slos
      {"service_name": "payments-api", "name": "Availability", "target": 99.90}
    """
    svc_result = await db.execute(select(Service).where(Service.name == body.service_name))
    service = svc_result.scalar_one_or_none()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{body.service_name}' not found")

    slo = ServiceSLO(
        service_id=service.id,
        name=body.name,
        target=body.target,
        metric_query=body.metric_query,
        window_days=body.window_days,
        status="MEETING",
    )
    db.add(slo)
    await db.flush()

    await record_audit(
        db=db,
        action="slo.created",
        actor_id=str(user.id),
        actor_name=user.github_username,
        resource_type="slo",
        resource_id=str(slo.id),
        details={"service": body.service_name, "target": body.target},
    )

    return SLOResponse.model_validate(slo)
