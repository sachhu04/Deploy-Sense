from typing import Any

"""
DeploySense — Pydantic Schemas (API Request/Response Models)

WHY THIS EXISTS:
FastAPI uses Pydantic models for:
  1. Request body validation (reject bad input before it hits the DB)
  2. Response serialization (control exactly what the API returns)
  3. Auto-generated OpenAPI documentation

WHY SEPARATE FROM SQLAlchemy MODELS:
  - DB models represent storage. Schemas represent the API contract.
  - A deployment DB row has internal fields (updated_at, FK UUIDs).
    The API response should not expose all of them.
  - Request schemas validate input. DB models don't validate — they store.

NAMING CONVENTION:
  - *Create: Request body for POST
  - *Update: Request body for PATCH
  - *Response: What the API returns
  - *List: Paginated list response
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# ─── Pagination ──────────────────────────────────────────────────────────────


class PaginationParams(BaseModel):
    """Query parameters for paginated endpoints."""

    page: int = Field(default=1, ge=1, description="Page number")
    per_page: int = Field(default=20, ge=1, le=100, description="Items per page")


class PaginationMeta(BaseModel):
    """Pagination metadata in list responses."""

    page: int
    per_page: int
    total: int


# ─── Deployments ─────────────────────────────────────────────────────────────


class DeploymentCreate(BaseModel):
    """
    Request body: Create a new deployment.

    WHY these fields:
      - service_name: Which service is being deployed
      - environment: Where (staging, production)
      - version: Semantic version or tag
      - git_sha: The exact commit being deployed (immutable reference)
      - deployed_by: Who triggered the deployment
    """

    service_name: str = Field(..., min_length=1, max_length=255)
    environment: str = Field(..., min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=255)
    git_sha: str = Field(..., min_length=7, max_length=255)
    deployed_by: str | None = Field(default=None, max_length=255)


class DeploymentResponse(BaseModel):
    """
    Response: Single deployment.

    Includes denormalized risk data so the dashboard doesn't need
    a separate API call to show risk alongside deployment status.
    """

    id: uuid.UUID
    service_name: str | None = None
    environment: str
    version: str | None = None
    git_sha: str
    status: str
    risk_score: int | None = None
    risk_level: str | None = None
    failure_probability: float | None = None
    deployed_by: str | None = None
    initiated_at: datetime | None = None
    deployed_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeploymentListResponse(BaseModel):
    """Paginated list of deployments."""

    data: list[DeploymentResponse]
    pagination: PaginationMeta


class DeploymentStatusUpdate(BaseModel):
    """Request body: Update deployment status."""

    status: str = Field(..., description="New deployment status")
    message: str | None = Field(default=None, description="Reason for status change")


# ─── Risk ────────────────────────────────────────────────────────────────────


class RiskEvaluationRequest(BaseModel):
    """
    Request body: Evaluate deployment risk.

    Sent from API Service to Risk Engine via REST.
    Maps to POST /internal/risk/evaluate in the architecture doc.
    """

    deployment_id: uuid.UUID
    service_name: str
    environment: str
    git_sha: str
    files_changed: int | None = None
    lines_added: int | None = None
    lines_deleted: int | None = None
    has_db_migration: bool = False
    has_infra_change: bool = False


class RiskFactor(BaseModel):
    """A single contributing factor to the risk score."""

    name: str
    contribution: float = Field(..., ge=0.0, le=1.0)
    description: str | None = None


class RiskEvaluationResponse(BaseModel):
    """
    Response: Risk evaluation result.

    WHY failure_probability is separate from risk_score:
      - risk_score (0-100): Human-readable composite score
      - failure_probability (0.0-1.0): Statistical estimate of deployment failure
    They correlate but are computed differently. The score includes
    policy considerations; the probability is purely statistical.
    """

    deployment_id: uuid.UUID
    risk_score: int = Field(..., ge=0, le=100)
    risk_level: str
    failure_probability: float = Field(..., ge=0.0, le=1.0)
    factors: list[RiskFactor] = []
    recommendation: str


class RiskHistoryResponse(BaseModel):
    """Risk assessment history for a deployment."""

    deployment_id: uuid.UUID
    risk_score: int
    risk_level: str
    failure_probability: float | None = None
    recommendation: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Services ────────────────────────────────────────────────────────────────


class ServiceResponse(BaseModel):
    """Response: Service information."""

    id: uuid.UUID
    name: str
    environment: str | None = None
    status: str
    stability_score: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ServiceHealthResponse(BaseModel):
    """
    Response: Service health summary.

    Combines the latest metrics snapshot with deployment context.
    Maps to GET /services/{name}/health in the architecture doc.
    """

    service: str
    status: str
    current_version: str | None = None
    metrics: dict[str, Any] | None = None


# ─── Webhooks ────────────────────────────────────────────────────────────────


class WebhookResponse(BaseModel):
    """Response: Webhook received acknowledgement."""

    status: str = "received"
    event_id: str | None = None


# ─── Alerts ──────────────────────────────────────────────────────────────────


class AlertResponse(BaseModel):
    """Response: Alert information."""

    id: uuid.UUID
    severity: str | None = None
    title: str | None = None
    description: str | None = None
    status: str
    triggered_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}


# ─── Standard Error ──────────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """
    Standard error response format.

    Maps to section 3.4 of the architecture doc.
    Every error response across all endpoints follows this shape.
    """

    code: str
    message: str
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
