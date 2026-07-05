from typing import Any

"""
DeploySense — Database Models

WHY ALL MODELS IN ONE FILE (for now):
At MVP with 11 tables, a single models file is easier to navigate than 11 files.
When the model count exceeds ~15 or when services split, we'll extract into
per-domain files (e.g., models/deployments.py, models/risk.py).

Every table maps directly to the architecture doc (05-database-schemas.md).
If you add a table here, add it there too.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from deploysense.database.base import Base, BaseModel

# ─── Enums ───────────────────────────────────────────────────────────────────
# Python enums for type safety. Stored as VARCHAR in PostgreSQL.
# WHY NOT PostgreSQL enums: Schema migrations to add values require ALTER TYPE
# which is painful. VARCHAR with Python enum validation is simpler.


class DeploymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    RISK_ASSESSED = "RISK_ASSESSED"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    MONITORING = "MONITORING"
    STABLE = "STABLE"
    DEGRADED = "DEGRADED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, enum.Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AnalysisStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ─── Organizations ───────────────────────────────────────────────────────────
# WHY: Multi-tenancy boundary. All data is scoped to an organization.
# Even if we start with a single org, the data model supports multiple.


class Organization(BaseModel):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Relationships
    users: Mapped[list["User"]] = relationship(back_populates="organization")
    repositories: Mapped[list["Repository"]] = relationship(back_populates="organization")


# ─── Users ───────────────────────────────────────────────────────────────────
# WHY: Track who deploys what. GitHub username is the identity anchor.
# Role field enables future RBAC without schema migration.


class User(BaseModel):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(50), default="engineer", nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    organization: Mapped[Organization | None] = relationship(back_populates="users")


# ─── Repositories ────────────────────────────────────────────────────────────
# WHY: DeploySense connects to GitHub repos. This tracks which repos
# are monitored and their sync status.


class Repository(BaseModel):
    __tablename__ = "repositories"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")

    # Relationships
    organization: Mapped[Organization | None] = relationship(back_populates="repositories")
    services: Mapped[list["Service"]] = relationship(back_populates="repository")
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="repository")


# ─── Services ────────────────────────────────────────────────────────────────
# WHY: A repository can contain multiple deployable services.
# E.g., a monorepo with "payments-api" and "payments-worker".
# Risk scores and deployment tracking are per-service, not per-repo.


class Service(BaseModel):
    __tablename__ = "services"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")
    stability_score: Mapped[int] = mapped_column(Integer, default=100)

    # Relationships
    repository: Mapped[Repository | None] = relationship(back_populates="services")
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="service")
    metrics_snapshots: Mapped[list["MetricsSnapshot"]] = relationship(back_populates="service")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="service")


# ─── Pull Requests ───────────────────────────────────────────────────────────
# WHY: PRs are a primary risk signal. Files changed, lines changed,
# migration presence, and review coverage all feed into risk scoring.


class PullRequest(BaseModel):
    __tablename__ = "pull_requests"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id"), nullable=True
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    files_changed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_added: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_deleted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_db_migration: Mapped[bool] = mapped_column(Boolean, default=False)
    has_infra_change: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Override created_at to not use server default (comes from GitHub)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Relationships
    repository: Mapped[Repository | None] = relationship(back_populates="pull_requests")


# ─── Deployments ─────────────────────────────────────────────────────────────
# WHY: The central entity. Every deployment flows through:
# PENDING → RISK_ASSESSED → APPROVED/BLOCKED → DEPLOYING → DEPLOYED →
# MONITORING → STABLE/DEGRADED → ROLLED_BACK
#
# TRADEOFF: Status is a VARCHAR, not a PostgreSQL enum. This avoids
# ALTER TYPE migrations when we add new states (which we will).


class Deployment(BaseModel):
    __tablename__ = "deployments"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    environment: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_sha: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING")

    # Risk data (denormalized from risk_assessments for fast queries)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_probability: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    deployed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Lifecycle timestamps
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    service: Mapped[Service | None] = relationship(back_populates="deployments")
    events: Mapped[list["DeploymentEvent"]] = relationship(back_populates="deployment")
    risk_assessments: Mapped[list["RiskAssessment"]] = relationship(back_populates="deployment")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="deployment")
    ai_analyses: Mapped[list["AIAnalysis"]] = relationship(back_populates="deployment")

    @property
    def service_name(self) -> str | None:
        """Expose the related service name through the deployment API schema."""
        return self.service.name if self.service else None


# ─── Deployment Events ───────────────────────────────────────────────────────
# WHY: Immutable audit trail. Every state transition is recorded.
# This is event-sourcing-lite — we can reconstruct the full deployment
# timeline from these events. Critical for post-incident analysis.


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    current_state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Relationships
    deployment: Mapped[Deployment | None] = relationship(back_populates="events")


# ─── Risk Assessments ────────────────────────────────────────────────────────
# WHY: Historical record of every risk evaluation. The deployment table
# stores the latest score (denormalized), but this table stores ALL
# assessments including the full feature vector and contributing factors.
# Essential for model training and auditing risk decisions.


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True
    )
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(50), nullable=False)
    failure_probability: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    feature_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    factors: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Relationships
    deployment: Mapped[Deployment | None] = relationship(back_populates="risk_assessments")


# ─── Metrics Snapshots ───────────────────────────────────────────────────────
# WHY: Point-in-time capture of service metrics (error rate, latency, etc.)
# collected from Prometheus. Used by the Risk Engine to compare current
# metrics against baselines. Stored in regular PostgreSQL for now —
# TimescaleDB is a future optimization when volume justifies it.


class MetricsSnapshot(Base):
    __tablename__ = "metrics_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    error_rate: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_p50_ms: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    latency_p95_ms: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    latency_p99_ms: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    request_rate_rps: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    cpu_usage: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    memory_usage: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Relationships
    service: Mapped[Service | None] = relationship(back_populates="metrics_snapshots")


# ─── Alerts ──────────────────────────────────────────────────────────────────
# WHY: Track alerts correlated with deployments. When error rate spikes
# after a deploy, the alert links to both the service AND the deployment.
# This correlation is what makes DeploySense an intelligence platform
# rather than just another dashboard.


class Alert(BaseModel):
    __tablename__ = "alerts"

    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=True
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True
    )
    severity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="OPEN")
    triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    service: Mapped[Service | None] = relationship(back_populates="alerts")
    deployment: Mapped[Deployment | None] = relationship(back_populates="alerts")


# ─── AI Analyses ─────────────────────────────────────────────────────────────
# WHY: Record of every AI-powered analysis (root cause, PR risk, etc.)
# Stored separately from risk assessments because AI analysis is:
#   1. Async (takes seconds, not milliseconds)
#   2. Optional (not every deployment gets AI analysis)
#   3. Uses a different data model (free-text summary + structured JSON)


class AIAnalysis(BaseModel):
    __tablename__ = "ai_analyses"

    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployments.id"), nullable=True
    )
    analysis_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_causes: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    recommendations: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    failure_patterns: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    deployment: Mapped[Deployment | None] = relationship(back_populates="ai_analyses")


# ─── Audit Log (Phase 3) ────────────────────────────────────────────────────
# WHY: Immutable audit trail for compliance, incident response, and debugging.
# Append-only — no UPDATE or DELETE operations allowed.


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    actor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), nullable=False, index=True
    )


# ─── Service Level Objectives (Phase 3) ─────────────────────────────────────
# WHY: Track SLOs per service. Maps to architecture/07-observability.md 7.10.
# Used by the SLO monitoring dashboard to show burn rate and error budget.


class ServiceSLO(BaseModel):
    __tablename__ = "service_slos"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)  # e.g., 99.90
    metric_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    current_value: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), default="MEETING"
    )  # MEETING / BREACHING / AT_RISK

    # Relationships
    service: Mapped[Service] = relationship()
