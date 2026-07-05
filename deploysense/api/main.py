"""
DeploySense — API Service Entry Point

The main FastAPI application. Mounts all routers under /api/v1
and applies middleware (CORS, security headers, rate limiting).

Start with: uvicorn deploysense.api.main:app --reload --port 8000
"""

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from deploysense.api.routes import (
    admin,
    ai,
    alerts,
    auth,
    deployments,
    repositories,
    services,
    webhooks,
)
from deploysense.api.security import apply_security_middleware
from deploysense.core import get_settings
from deploysense.dashboard.routes import router as dashboard_router

settings = get_settings()

app = FastAPI(
    title="DeploySense",
    description="Deployment Intelligence Platform",
    version="0.1.0",
)

# ─── CORS ────────────────────────────────────────────────────────────────────
# Required for the Next.js frontend to communicate with the API,
# especially during the OAuth callback redirect flow.

_frontend_url = (
    settings.frontend_url.rstrip("/") if settings.frontend_url else "http://localhost:3001"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[_frontend_url, "http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Security Middleware ─────────────────────────────────────────────────────
apply_security_middleware(app)

# ─── Routers ─────────────────────────────────────────────────────────────────
# All API routes are prefixed with /api/v1 for versioning.

app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
app.include_router(repositories.router, prefix="/api/v1", tags=["repositories"])
app.include_router(deployments.router, prefix="/api/v1", tags=["deployments"])
app.include_router(services.router, prefix="/api/v1", tags=["services"])
app.include_router(alerts.router, prefix="/api/v1", tags=["alerts"])
app.include_router(webhooks.router, prefix="/api/v1", tags=["webhooks"])
app.include_router(admin.router, prefix="/api/v1", tags=["admin"])
app.include_router(ai.router, prefix="/api/v1", tags=["ai"])

# Dashboard routes (server-rendered HTML pages)
app.include_router(dashboard_router, tags=["dashboard"])


# ─── Health Check ────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "api-service"}


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "deploysense-api",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
