"""
DeploySense — API Service Entry Point

The main FastAPI application. Mounts all routers under /api/v1
and applies middleware (CORS, security headers, rate limiting).

Start with: uvicorn deploysense.api.main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deploysense.api.routes import auth, deployments, services, alerts, webhooks, admin, ai
from deploysense.api.security import apply_security_middleware
from deploysense.core import get_settings

settings = get_settings()

app = FastAPI(
    title="DeploySense API",
    description="Deployment Intelligence Platform",
    version="0.1.0",
)

# ─── CORS ────────────────────────────────────────────────────────────────────
# Required for the Next.js frontend to communicate with the API,
# especially during the OAuth callback redirect flow.

_frontend_url = settings.frontend_url.rstrip("/") if settings.frontend_url else "http://localhost:3001"

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

app.include_router(auth.router,        prefix="/api/v1", tags=["auth"])
app.include_router(deployments.router, prefix="/api/v1", tags=["deployments"])
app.include_router(services.router,    prefix="/api/v1", tags=["services"])
app.include_router(alerts.router,      prefix="/api/v1", tags=["alerts"])
app.include_router(webhooks.router,    prefix="/api/v1", tags=["webhooks"])
app.include_router(admin.router,       prefix="/api/v1", tags=["admin"])
app.include_router(ai.router,          prefix="/api/v1", tags=["ai"])


# ─── Health Check ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}