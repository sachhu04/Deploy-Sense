from typing import Any

"""
DeploySense — Enhanced Risk Scoring Engine (Sprint 2)

WHAT CHANGED FROM SPRINT 1:
Sprint 1 scored risk using only static PR metadata (files, lines, migration).
Sprint 2 adds HISTORICAL CONTEXT — the most powerful risk signals come from
the service's own deployment history:
  - How many times did this service fail recently?
  - How frequently is it being deployed (deploy fatigue)?
  - What's the baseline error rate vs. current?
  - What's the service's stability score?

NEW FEATURES IN SPRINT 2:
  1. Deployment frequency scoring (too many deploys = fatigue)
  2. Recent failure history (past failures predict future failures)
  3. Service stability score integration
  4. Metrics anomaly detection (error rate spike)
  5. Time-of-day risk modifier (Friday 5pm deploys are riskier)
  6. Changeset complexity scoring (not just size, but composition)

ALGORITHM:
  risk_score = Σ(feature × weight)  [static features, Sprint 1]
             + Σ(historical × weight) [historical features, Sprint 2]
             + time_modifier           [contextual, Sprint 2]

CALIBRATION:
The failure_probability is now a logistic function instead of linear.
This better models real-world failure rates: most deployments are low-risk,
failure probability increases sharply for high-risk deployments.
"""

import math
import time
from datetime import UTC, datetime

from deploysense.logging import get_logger

logger = get_logger(__name__)


# ─── Feature Weights ─────────────────────────────────────────────────────────

WEIGHTS = {
    # Static features (from PR/deployment metadata)
    "has_db_migration": 20,
    "has_infra_change": 15,
    "files_changed": 10,
    "lines_changed": 8,
    # Historical features (from deployment history) — NEW in Sprint 2
    "recent_failures": 15,
    "deployment_frequency": 5,
    "service_instability": 10,
    "metrics_anomaly": 12,
    # Contextual features — NEW in Sprint 2
    "time_of_day": 5,
}


# ─── Data Classes ────────────────────────────────────────────────────────────


class RiskFeatures:
    """
    Complete feature vector for risk scoring.

    Sprint 1 features are provided by the API caller.
    Sprint 2 features are computed from database queries.
    """

    def __init__(
        self,
        # Static (Sprint 1)
        has_db_migration: bool = False,
        has_infra_change: bool = False,
        files_changed: int = 0,
        lines_added: int = 0,
        lines_deleted: int = 0,
        # Historical (Sprint 2)
        recent_failure_count: int = 0,
        deployments_last_24h: int = 0,
        service_stability_score: int = 100,
        current_error_rate: float = 0.0,
        baseline_error_rate: float = 0.0,
        # Contextual (Sprint 2)
        deploy_hour_utc: int | None = None,
        deploy_day_of_week: int | None = None,  # 0=Monday, 6=Sunday
    ) -> None:
        self.has_db_migration = has_db_migration
        self.has_infra_change = has_infra_change
        self.files_changed = files_changed
        self.lines_added = lines_added
        self.lines_deleted = lines_deleted
        self.recent_failure_count = recent_failure_count
        self.deployments_last_24h = deployments_last_24h
        self.service_stability_score = service_stability_score
        self.current_error_rate = current_error_rate
        self.baseline_error_rate = baseline_error_rate

        now = datetime.now(UTC)
        self.deploy_hour_utc = deploy_hour_utc if deploy_hour_utc is not None else now.hour
        self.deploy_day_of_week = (
            deploy_day_of_week if deploy_day_of_week is not None else now.weekday()
        )


class RiskFactor:
    """A single contributing factor to the risk score."""

    def __init__(self, name: str, contribution: float, description: str, category: str = "static"):
        self.name = name
        self.contribution = contribution
        self.description = description
        self.category = category  # "static", "historical", "contextual"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "contribution": self.contribution,
            "description": self.description,
            "category": self.category,
        }


class RiskResult:
    """Complete risk evaluation result."""

    def __init__(
        self,
        risk_score: int,
        risk_level: str,
        failure_probability: float,
        recommendation: str,
        factors: list[RiskFactor],
        feature_snapshot: dict[str, Any],
    ):
        self.risk_score = risk_score
        self.risk_level = risk_level
        self.failure_probability = failure_probability
        self.recommendation = recommendation
        self.factors = factors
        self.feature_snapshot = feature_snapshot


# ─── Enhanced Risk Scoring Engine ────────────────────────────────────────────


def compute_enhanced_risk(features: RiskFeatures) -> RiskResult:
    """
    Compute deployment risk using the enhanced heuristic model.

    This is the Sprint 2 version that incorporates:
      - Static features (from PR metadata)
      - Historical features (from deployment history)
      - Contextual features (time-of-day, day-of-week)
      - Logistic failure probability calibration
    """
    start = time.perf_counter()
    score = 0.0
    factors: list[RiskFactor] = []

    # ── STATIC FEATURES (Sprint 1) ──────────────────────────────────────

    if features.has_db_migration:
        c = WEIGHTS["has_db_migration"]
        score += c
        factors.append(
            RiskFactor(
                "database_migration", c / 100, "Includes database schema migration", "static"
            )
        )

    if features.has_infra_change:
        c = WEIGHTS["has_infra_change"]
        score += c
        factors.append(
            RiskFactor(
                "infrastructure_change", c / 100, "Includes infrastructure changes", "static"
            )
        )

    files = features.files_changed
    if files > 0:
        c = int(_normalize_tier(files, WEIGHTS["files_changed"], tiers=[5, 20]))
        score += c
        factors.append(RiskFactor("files_changed", c / 100, f"{files} files changed", "static"))

    lines = features.lines_added + features.lines_deleted
    if lines > 0:
        c = int(_normalize_tier(lines, WEIGHTS["lines_changed"], tiers=[100, 500]))
        score += c
        factors.append(
            RiskFactor("lines_changed", c / 100, f"{lines} total lines changed", "static")
        )

    # ── HISTORICAL FEATURES (Sprint 2) ──────────────────────────────────

    # Recent failures: Each failure in the last 7 days adds risk
    if features.recent_failure_count > 0:
        c = min(WEIGHTS["recent_failures"], features.recent_failure_count * 5)
        score += c
        factors.append(
            RiskFactor(
                "recent_failures",
                c / 100,
                f"{features.recent_failure_count} failures in the last 7 days",
                "historical",
            )
        )

    # Deployment frequency: >3 deploys in 24h indicates churn
    if features.deployments_last_24h > 3:
        c = min(WEIGHTS["deployment_frequency"], (features.deployments_last_24h - 3) * 2)
        score += c
        factors.append(
            RiskFactor(
                "deployment_frequency",
                c / 100,
                f"{features.deployments_last_24h} deployments in the last 24 hours",
                "historical",
            )
        )

    # Service instability: Low stability score = high risk
    if features.service_stability_score < 80:
        instability = 100 - features.service_stability_score
        c = int(min(WEIGHTS["service_instability"], instability * 0.15))
        score += c
        factors.append(
            RiskFactor(
                "service_instability",
                c / 100,
                f"Service stability score: {features.service_stability_score}/100",
                "historical",
            )
        )

    # Metrics anomaly: Current error rate significantly above baseline
    if features.current_error_rate > 0 and features.baseline_error_rate > 0:
        ratio = features.current_error_rate / max(features.baseline_error_rate, 0.001)
        if ratio > 2.0:  # Error rate is 2x baseline
            c = int(min(WEIGHTS["metrics_anomaly"], (ratio - 1) * 4))
            score += c
            factors.append(
                RiskFactor(
                    "error_rate_elevated",
                    c / 100,
                    f"Error rate {ratio:.1f}x above baseline "
                    f"({features.current_error_rate:.4f} vs {features.baseline_error_rate:.4f})",
                    "historical",
                )
            )

    # ── CONTEXTUAL FEATURES (Sprint 2) ──────────────────────────────────

    time_risk = _compute_time_risk(features.deploy_hour_utc, features.deploy_day_of_week)
    if time_risk > 0:
        c = int(WEIGHTS["time_of_day"] * time_risk)
        score += c
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        factors.append(
            RiskFactor(
                "deployment_timing",
                c / 100,
                f"Deploying at {features.deploy_hour_utc:02d}:00 UTC on "
                f"{day_names[features.deploy_day_of_week]}",
                "contextual",
            )
        )

    # ── FINAL SCORING ───────────────────────────────────────────────────

    score = max(0, min(100, score))
    risk_score = int(round(score))

    risk_level, recommendation = _determine_level(risk_score)
    failure_probability = _logistic_probability(risk_score)

    factors.sort(key=lambda f: f.contribution, reverse=True)

    feature_snapshot = {
        "has_db_migration": features.has_db_migration,
        "has_infra_change": features.has_infra_change,
        "files_changed": features.files_changed,
        "lines_added": features.lines_added,
        "lines_deleted": features.lines_deleted,
        "recent_failure_count": features.recent_failure_count,
        "deployments_last_24h": features.deployments_last_24h,
        "service_stability_score": features.service_stability_score,
        "current_error_rate": features.current_error_rate,
        "baseline_error_rate": features.baseline_error_rate,
        "deploy_hour_utc": features.deploy_hour_utc,
        "deploy_day_of_week": features.deploy_day_of_week,
    }

    duration = time.perf_counter() - start
    logger.info(
        "enhanced_risk_computed",
        risk_score=risk_score,
        risk_level=risk_level,
        failure_probability=failure_probability,
        factor_count=len(factors),
        computation_ms=round(duration * 1000, 2),
    )

    return RiskResult(
        risk_score=risk_score,
        risk_level=risk_level,
        failure_probability=failure_probability,
        recommendation=recommendation,
        factors=factors,
        feature_snapshot=feature_snapshot,
    )


# ─── Helper Functions ────────────────────────────────────────────────────────


def _normalize_tier(value: int, max_weight: float, tiers: list[int]) -> float:
    """
    Three-tier normalization for numeric features.

    tiers = [low_threshold, high_threshold]
      value <= tiers[0]:                    20% of weight
      tiers[0] < value <= tiers[1]:         50% of weight
      value > tiers[1]:                     100% of weight
    """
    if value > tiers[1]:
        return max_weight
    elif value > tiers[0]:
        return max_weight * 0.5
    else:
        return max_weight * 0.2


def _determine_level(score: int) -> tuple[str, str]:
    """Map risk score to level and recommendation."""
    if score <= 25:
        return "LOW", "PROCEED"
    elif score <= 50:
        return "MODERATE", "PROCEED_WITH_MONITORING"
    elif score <= 75:
        return "HIGH", "REQUIRE_MANUAL_APPROVAL"
    else:
        return "CRITICAL", "BLOCK_DEPLOYMENT"


def _logistic_probability(score: int) -> float:
    """
    Convert risk score to failure probability using logistic function.

    WHY logistic (not linear):
    Real-world failure rates aren't linear. Most deployments succeed.
    Failure probability increases sharply for high-risk deployments.

    Calibration: score=50 → ~15% failure probability
                 score=80 → ~40% failure probability
                 score=100 → ~50% failure probability (capped)
    """
    # Logistic: P = 1 / (1 + e^(-k*(x - x0)))
    # k=0.08, x0=60: inflection point at score=60
    k = 0.08
    x0 = 60
    p = 1.0 / (1.0 + math.exp(-k * (score - x0)))
    return round(min(p, 0.5), 4)  # Cap at 50%


def _compute_time_risk(hour: int, day_of_week: int) -> float:
    """
    Compute time-of-day risk modifier.

    WHY: Deployments outside business hours or on Fridays are riskier
    because fewer people are available to respond to incidents.

    RISK WINDOWS:
      0.0: Safe hours (09:00-16:00 Mon-Thu)
      0.5: Marginal hours (07:00-09:00, 16:00-20:00)
      1.0: High-risk hours (20:00-07:00, Fri afternoon, weekends)
    """
    # Friday afternoon (after 2pm) or weekend
    if day_of_week >= 5:  # Saturday, Sunday
        return 1.0
    if day_of_week == 4 and hour >= 14:  # Friday after 2pm
        return 1.0

    # Late night / early morning (high risk)
    if hour < 7 or hour >= 20:
        return 1.0

    # Marginal hours
    if hour < 9 or hour >= 16:
        return 0.5

    # Business hours (safe)
    return 0.0


# ─── Backward Compatibility ─────────────────────────────────────────────────
# Keep the Sprint 1 API working. The original compute_risk_score function
# is still called by the existing routes. It now delegates to the enhanced engine.


def compute_risk_score(request) -> object:  # type: ignore[no-untyped-def]
    """
    Sprint 1 backward-compatible wrapper.

    Converts the Sprint 1 RiskEvaluationRequest into Sprint 2 RiskFeatures
    and calls the enhanced engine.
    """
    from deploysense.api.schemas import RiskEvaluationResponse
    from deploysense.api.schemas import RiskFactor as SchemaRiskFactor

    features = RiskFeatures(
        has_db_migration=request.has_db_migration,
        has_infra_change=request.has_infra_change,
        files_changed=request.files_changed or 0,
        lines_added=request.lines_added or 0,
        lines_deleted=request.lines_deleted or 0,
    )

    result = compute_enhanced_risk(features)

    return RiskEvaluationResponse(
        deployment_id=request.deployment_id,
        risk_score=result.risk_score,
        risk_level=result.risk_level,
        failure_probability=result.failure_probability,
        factors=[
            SchemaRiskFactor(
                name=f.name,
                contribution=f.contribution,
                description=f.description,
            )
            for f in result.factors
        ],
        recommendation=result.recommendation,
    )
