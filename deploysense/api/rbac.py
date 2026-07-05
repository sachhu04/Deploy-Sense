"""
DeploySense — Role-Based Access Control (Phase 3)

WHY THIS EXISTS:
Phase 1-2 had a single user role. Production needs granular permissions:
  - Viewers can see dashboards but can't deploy
  - Engineers can deploy but can't change org settings
  - Admins can manage users and organization settings
  - Service accounts (CI/CD) can only trigger deployments

RBAC MODEL:
  User → has one Role → Role has many Permissions

ROLES:
  viewer     — Read-only access to dashboard, deployments, risk
  engineer   — Everything a viewer can do + create deployments, trigger analysis
  admin      — Everything an engineer can do + manage users, org, settings
  service    — API key auth for CI/CD pipelines (create deployments only)

PERMISSIONS:
  Permissions are strings like "deployments:read", "deployments:create".
  A role maps to a set of permissions.

WHY NOT fine-grained resource-level ACL:
  RBAC is sufficient for MVP. Resource-level ACL (e.g., "user X can only
  deploy service Y in environment Z") adds database joins on every request.
  We'll add it in a future iteration if the user base demands it.

IMPLEMENTATION:
  - Permissions are defined in code (not database) for simplicity
  - Role is stored on the User model (already exists as user.role)
  - FastAPI dependencies enforce permissions at the route level
"""

from enum import Enum
from typing import Any

from fastapi import Depends, HTTPException, status

from deploysense.logging import get_logger
from deploysense.models import User

logger = get_logger(__name__)


# ─── Permissions ─────────────────────────────────────────────────────────────


class Permission(str, Enum):
    """
    All permissions in the system.

    NAMING: resource:action
    This makes it easy to check "can user do X to Y?"
    """

    # Deployments
    DEPLOYMENTS_READ = "deployments:read"
    DEPLOYMENTS_CREATE = "deployments:create"
    DEPLOYMENTS_APPROVE = "deployments:approve"
    DEPLOYMENTS_ROLLBACK = "deployments:rollback"

    # Risk
    RISK_READ = "risk:read"
    RISK_EVALUATE = "risk:evaluate"

    # AI Analysis
    AI_ANALYZE = "ai:analyze"
    AI_READ = "ai:read"

    # Services
    SERVICES_READ = "services:read"
    SERVICES_MANAGE = "services:manage"

    # Repositories
    REPOS_READ = "repos:read"
    REPOS_MANAGE = "repos:manage"

    # Alerts
    ALERTS_READ = "alerts:read"
    ALERTS_MANAGE = "alerts:manage"

    # Users & Org
    USERS_READ = "users:read"
    USERS_MANAGE = "users:manage"
    ORG_MANAGE = "org:manage"

    # Audit
    AUDIT_READ = "audit:read"

    # Dashboard
    DASHBOARD_READ = "dashboard:read"


# ─── Role Definitions ───────────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.DEPLOYMENTS_READ,
        Permission.RISK_READ,
        Permission.AI_READ,
        Permission.SERVICES_READ,
        Permission.REPOS_READ,
        Permission.ALERTS_READ,
        Permission.DASHBOARD_READ,
    },
    "engineer": {
        # All viewer permissions
        Permission.DEPLOYMENTS_READ,
        Permission.RISK_READ,
        Permission.AI_READ,
        Permission.SERVICES_READ,
        Permission.REPOS_READ,
        Permission.ALERTS_READ,
        Permission.DASHBOARD_READ,
        # Engineer-specific
        Permission.DEPLOYMENTS_CREATE,
        Permission.DEPLOYMENTS_APPROVE,
        Permission.DEPLOYMENTS_ROLLBACK,
        Permission.RISK_EVALUATE,
        Permission.AI_ANALYZE,
        Permission.REPOS_MANAGE,
        Permission.ALERTS_MANAGE,
        Permission.USERS_READ,
    },
    "admin": {
        # All permissions
        *Permission,
    },
    "service": {
        # CI/CD service accounts — limited scope
        Permission.DEPLOYMENTS_CREATE,
        Permission.DEPLOYMENTS_READ,
        Permission.RISK_EVALUATE,
        Permission.SERVICES_READ,
    },
}


# ─── Permission Check ───────────────────────────────────────────────────────


def has_permission(user: User, permission: Permission) -> bool:
    """Check if a user has a specific permission based on their role."""
    role = user.role or "viewer"
    role_perms = ROLE_PERMISSIONS.get(role, set())
    return permission in role_perms


def check_permission(user: User, permission: Permission) -> None:
    """
    Check permission and raise 403 if denied.

    Use this in route handlers for imperative permission checks.
    """
    if not has_permission(user, permission):
        logger.warning(
            "permission_denied",
            user_id=str(user.id),
            role=user.role,
            permission=permission.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {permission.value} requires role with higher privileges",
        )


# ─── FastAPI Dependencies ────────────────────────────────────────────────────


def require_permission(permission: Permission) -> Any:
    """
    FastAPI dependency factory for permission-based access control.

    USAGE:
        @router.post("/deployments")
        async def create_deployment(
            user: User = Depends(require_permission(Permission.DEPLOYMENTS_CREATE))
        ):
            ...

    HOW IT WORKS:
        1. get_current_user extracts and validates the JWT
        2. require_permission checks the user's role has the needed permission
        3. If denied, returns 403 Forbidden
        4. If allowed, returns the User object

    WHY a factory (not a direct dependency):
        Each route needs a different permission. The factory creates a
        dependency bound to a specific permission string.
    """
    from deploysense.api.auth import get_current_user

    async def _check(user: User = Depends(get_current_user)) -> User:
        check_permission(user, permission)
        return user

    return _check


def require_role(role: str) -> Any:
    """
    FastAPI dependency: require a minimum role.

    USAGE:
        @router.post("/users")
        async def manage_users(user: User = Depends(require_role("admin"))):
            ...
    """
    from deploysense.api.auth import get_current_user

    role_hierarchy = {"viewer": 0, "service": 1, "engineer": 2, "admin": 3}

    async def _check(user: User = Depends(get_current_user)) -> User:
        user_level = role_hierarchy.get(user.role or "viewer", 0)
        required_level = role_hierarchy.get(role, 0)

        if user_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires '{role}' role or higher",
            )
        return user

    return _check
