"""
DeploySense — Authentication Routes

ENDPOINTS (maps to architecture/03-api-definitions.md):
  GET    /auth/github/login     — Redirect to GitHub OAuth authorization
  GET    /auth/github/callback  — Handle GitHub OAuth callback, issue JWT
  POST   /auth/github           — Exchange GitHub OAuth code for JWT
  GET    /auth/me               — Get current authenticated user

FLOW:
  1. Frontend redirects user to GET /auth/github/login
  2. Backend redirects to: github.com/login/oauth/authorize?client_id=xxx&scope=repo,user:email
  3. User authorizes, GitHub redirects to GET /auth/github/callback?code=xxx
  4. Backend exchanges code → GitHub token → GitHub profile → JWT
  5. Backend redirects to frontend with JWT in URL fragment
  6. Frontend stores JWT, sends it as Bearer token on all requests

NOTE: Currently uses in-memory user storage (no PostgreSQL required).
      Replace with real DB calls once PostgreSQL is available.
"""

import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from deploysense.api.auth import (
    create_access_token,
    exchange_github_code,
    fetch_github_user,
    get_current_user,
)
from deploysense.core import get_settings
from deploysense.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# ─── In-Memory User Store (placeholder until DB is available) ────────────────
# Key: github_username, Value: user dict
_users: dict[str, dict] = {}


# ─── Request/Response Schemas ────────────────────────────────────────────────

class GitHubAuthRequest(BaseModel):
    """OAuth authorization code from GitHub."""
    code: str


class AuthResponse(BaseModel):
    """JWT token response after successful authentication."""
    access_token: str
    token_type: str = "bearer"
    user: "UserProfile"


class UserProfile(BaseModel):
    """Authenticated user profile."""
    id: uuid.UUID
    github_username: str
    email: str | None
    avatar_url: str | None
    role: str

    model_config = {"from_attributes": True}


# ─── Helper: upsert user in memory store ────────────────────────────────────

def _upsert_user(github_user: dict) -> dict:
    """Create or update user in the in-memory store. Returns user dict."""
    username = github_user["login"]
    if username in _users:
        _users[username]["email"] = github_user.get("email")
        _users[username]["avatar_url"] = github_user.get("avatar_url")
        logger.info("user_login", github_username=username)
    else:
        _users[username] = {
            "id": str(uuid.uuid4()),
            "github_username": username,
            "email": github_user.get("email"),
            "avatar_url": github_user.get("avatar_url"),
            "role": "engineer",
        }
        logger.info("user_created", github_username=username)
    return _users[username]


# ─── POST /auth/github ──────────────────────────────────────────────────────

@router.post("/auth/github", response_model=AuthResponse)
async def github_auth(
    body: GitHubAuthRequest,
) -> AuthResponse:
    """
    Authenticate via GitHub OAuth.

    FLOW:
      1. Exchange authorization code for GitHub access token
      2. Fetch user profile from GitHub API
      3. Create or update user in memory
      4. Issue a JWT for subsequent API calls
    """
    # Step 1: Exchange code for token
    token_data = await exchange_github_code(body.code)
    github_token = token_data["access_token"]

    # Step 2: Fetch GitHub profile
    github_user = await fetch_github_user(github_token)

    # Step 3: Create or update user in memory
    user = _upsert_user(github_user)

    # Step 4: Issue JWT
    access_token = create_access_token(
        user_id=user["id"],
        github_username=user["github_username"],
    )

    return AuthResponse(
        access_token=access_token,
        user=UserProfile(**user),
    )


# ─── GET /auth/me ────────────────────────────────────────────────────────────

@router.get("/auth/me", response_model=UserProfile)
async def get_me(user: dict = Depends(get_current_user)) -> UserProfile:
    """
    Get the currently authenticated user's profile.

    PURPOSE: Frontend calls this on page load to verify the JWT
    is still valid and display user info in the navigation bar.
    """
    return UserProfile(**user)


# ─── GET /auth/github/login ─────────────────────────────────────────────────

@router.get("/auth/github/login")
async def github_login() -> RedirectResponse:
    """
    Redirect user to GitHub's OAuth authorization page.

    PURPOSE: This is the entry point for the OAuth flow. The frontend
    links to this endpoint, which builds the GitHub authorization URL
    with the correct client_id, redirect_uri, and scopes.

    SCOPES:
      - user:email — access the user's email addresses
      - read:user  — access public profile information

    NOTE: redirect_uri must point to the BACKEND callback endpoint
    (port 8000), matching what's registered in the GitHub OAuth app.
    """
    settings = get_settings()
    # Build the backend callback URL (must match GitHub OAuth app settings)
    backend_base = f"http://localhost:{settings.backend_port}"
    params = urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": f"{backend_base}/api/v1/auth/github/callback",
        "scope": "user:email read:user",
    })
    return RedirectResponse(
        url=f"https://github.com/login/oauth/authorize?{params}",
        status_code=302,
    )


# ─── GET /auth/github/callback ──────────────────────────────────────────────

@router.get("/auth/github/callback")
async def github_callback(
    code: str = Query(..., description="GitHub OAuth authorization code"),
) -> RedirectResponse:
    """
    Handle the GitHub OAuth callback.

    FLOW:
      1. GitHub redirects here with ?code=xxx after user authorizes
      2. Exchange the code for a GitHub access token
      3. Fetch the user's GitHub profile
      4. Create or update the user in memory
      5. Issue a DeploySense JWT
      6. Redirect to the frontend with the JWT as a query parameter

    WHY redirect with query param (not fragment):
      Next.js App Router runs server-side first, and URL fragments (#)
      are not sent to the server. Query params work with both SSR and CSR.
      The frontend callback page reads the token, stores it, and clears the URL.

    SECURITY NOTE:
      In production, add a `state` parameter to prevent CSRF attacks.
      The state should be a random value stored in a cookie/session before
      redirecting to GitHub, and validated here on return.
    """
    settings = get_settings()

    try:
        # Step 1: Exchange code for GitHub token
        token_data = await exchange_github_code(code)
        github_token = token_data["access_token"]

        # Step 2: Fetch GitHub profile
        github_user = await fetch_github_user(github_token)

        # Step 3: Create or update user in memory
        user = _upsert_user(github_user)
        logger.info("user_oauth_callback", github_username=user["github_username"])

        # Step 4: Issue JWT
        access_token = create_access_token(
            user_id=user["id"],
            github_username=user["github_username"],
        )

        # Step 5: Redirect to frontend callback page with token
        frontend_callback = f"{settings.frontend_url.rstrip('/')}/auth/callback?token={access_token}"
        return RedirectResponse(url=frontend_callback, status_code=302)

    except Exception as e:
        logger.error("oauth_callback_failed", error=str(e))
        error_url = f"{settings.frontend_url.rstrip('/')}/auth/callback?error=auth_failed"
        return RedirectResponse(url=error_url, status_code=302)
