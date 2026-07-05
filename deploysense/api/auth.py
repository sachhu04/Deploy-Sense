from typing import Any

"""
DeploySense — Authentication & Authorization

WHY THIS EXISTS:
DeploySense uses GitHub OAuth for authentication. When a user logs in:
  1. Frontend redirects to GitHub OAuth authorization page
  2. GitHub redirects back with an authorization code
  3. Our backend exchanges the code for a GitHub access token
  4. We fetch the user's GitHub profile (username, email, avatar)
  5. We create/update a User record in our database
  6. We issue a JWT token that the frontend uses for subsequent API calls

WHY JWT (not session cookies):
  - Stateless: No server-side session storage needed
  - API-friendly: Works with mobile apps, CLI tools, CI/CD integrations
  - Standard: Libraries exist for every language/framework
  - Scalable: Any API instance can verify without hitting Redis

WHY NOT OAuth tokens directly:
  - GitHub tokens give access to the user's GitHub data
  - We don't want that power on every API call
  - JWTs are scoped to DeploySense permissions only
  - GitHub tokens are stored server-side for background operations

SECURITY:
  - JWT expires in 1 hour (short-lived)
  - Refresh tokens are not implemented yet (Phase 3: Security)
  - Secret key must be rotated in production
  - HMAC-SHA256 signing (not RSA — simpler for single-service auth)
"""

from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from deploysense.core import get_settings
from deploysense.logging import get_logger

logger = get_logger(__name__)

# JWT Configuration
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 1

# FastAPI security scheme — extracts Bearer token from Authorization header
security = HTTPBearer(auto_error=False)


class AuthenticatedUser(BaseModel):
    """Small authenticated identity shared by API and RBAC dependencies."""

    id: str
    github_username: str
    email: str | None = None
    avatar_url: str | None = None
    role: str = "engineer"
    organization_id: str | None = None


# ─── JWT Token Operations ───────────────────────────────────────────────────


def create_access_token(user_id: str, github_username: str) -> str:
    """
    Create a signed JWT access token.

    PAYLOAD:
      - sub: User UUID (the "subject" — who this token represents)
      - github: GitHub username (for logging, not auth)
      - exp: Expiration timestamp
      - iat: Issued-at timestamp

    WHY these claims:
      sub is standard JWT. github is convenience — avoids a DB lookup
      just to log "who did this". exp prevents token reuse after compromise.
    """
    settings = get_settings()
    now = datetime.now(UTC)

    payload = {
        "sub": user_id,
        "github": github_username,
        "exp": now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)  # type: ignore[no-any-return]


def decode_access_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT access token.

    FAILURES:
      - ExpiredSignatureError: Token has expired
      - JWTClaimsError: Invalid claims
      - JWTError: Invalid token structure
    """
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])  # type: ignore[no-any-return]


# ─── GitHub OAuth ────────────────────────────────────────────────────────────


async def exchange_github_code(code: str) -> dict[str, Any]:
    """
    Exchange GitHub OAuth authorization code for an access token.

    FLOW (OAuth 2.0 Authorization Code Grant):
      1. Frontend sends user to: github.com/login/oauth/authorize?client_id=...
      2. User authorizes, GitHub redirects back with ?code=...
      3. This function exchanges the code for a token

    WHY async httpx:
      This makes an HTTP call to GitHub. Using async httpx means we don't
      block the event loop while waiting for GitHub's response.
    """
    settings = get_settings()

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"GitHub OAuth error: {data.get('error_description', data['error'])}",
            )

        return data  # type: ignore[no-any-return]


async def fetch_github_user(access_token: str) -> dict[str, Any]:
    """
    Fetch the authenticated user's GitHub profile.

    RETURNS:
      - login: GitHub username
      - email: Primary email (may be None if private)
      - avatar_url: Profile picture URL
      - id: GitHub user ID
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]


# ─── FastAPI Dependency: Get Current User ────────────────────────────────────


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser:
    """
    FastAPI dependency that extracts and validates the current user.

    Usage in a route:
        @router.get("/me")
        async def get_me(user: dict = Depends(get_current_user)):
            return user

    FLOW:
      1. Extract Bearer token from Authorization header
      2. Decode JWT
      3. Look up User by github_username from the JWT's "github" claim
      4. Return User dict or raise 401

    NOTE: Uses in-memory user store (no DB required).
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = payload.get("sub")
        github_username = payload.get("github")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e

    # Import here to avoid circular imports
    from deploysense.api.routes.auth import _users

    user = _users.get(github_username)  # type: ignore[arg-type]

    if not user:
        # User was in a previous server session (memory cleared on restart).
        # Create a placeholder entry from the JWT claims.
        user = {
            "id": user_id,
            "github_username": github_username or "unknown",
            "email": None,
            "avatar_url": None,
            "role": "engineer",
        }
        _users[github_username or "unknown"] = user

    return AuthenticatedUser.model_validate(user)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser | None:
    """
    Like get_current_user but returns None instead of raising 401.

    Use for endpoints that work with or without auth
    (e.g., public dashboards with enhanced features for logged-in users).
    """
    if not credentials:
        return None

    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
