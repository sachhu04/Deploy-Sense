"""
DeploySense — Worker Jobs (Phase 1 Implementation)

This replaces the Phase 0 stubs with real implementations for Sprint 1.

Each job follows the same pattern:
  1. Log start
  2. Query database for work items
  3. Process each item (call external APIs, update DB)
  4. Log completion with timing

ERROR HANDLING:
  - Each job catches its own exceptions (fail isolation)
  - Individual item failures don't abort the batch
  - All errors are logged with structured context
"""

import time
from datetime import datetime
from typing import Any

from sqlalchemy import select

from deploysense.database.session import async_session_factory
from deploysense.integrations.github_client import (
    GitHubClient,
    detect_infra_change,
    detect_migration,
)
from deploysense.logging import get_logger
from deploysense.models import PullRequest, Repository

logger = get_logger(__name__)


async def repo_sync_job() -> None:
    """
    Synchronize GitHub repository data.

    IMPLEMENTATION (Phase 1 Sprint 1):
      1. Query all repositories with status=ACTIVE or CONNECTED
      2. For each repository, fetch recent PRs from GitHub
      3. For each PR, fetch file list for migration/infra detection
      4. Upsert PR data into pull_requests table

    WHY async_session_factory directly:
      Worker jobs aren't inside a FastAPI request. We create our own
      session outside the request lifecycle. The session is committed
      per-repository to avoid holding a long transaction.
    """
    start = time.perf_counter()
    logger.info("job_started", job="repo_sync")

    synced = 0
    errors = 0

    async with async_session_factory() as db:
        # Fetch all active repositories
        result = await db.execute(
            select(Repository).where(Repository.status.in_(["ACTIVE", "CONNECTED"]))
        )
        repos = result.scalars().all()

        if not repos:
            logger.info("job_completed", job="repo_sync", message="no repositories to sync")
            return

        for repo in repos:
            try:
                await _sync_repository(db, repo)
                synced += 1
            except Exception as e:
                errors += 1
                logger.error(
                    "repo_sync_failed",
                    owner=repo.owner,
                    repository=repo.repository_name,
                    error=str(e),
                )

        await db.commit()

    logger.info(
        "job_completed",
        job="repo_sync",
        synced=synced,
        errors=errors,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )


async def _sync_repository(db, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    """
    Sync a single repository's PR data from GitHub.

    FLOW:
      1. List recent PRs (last 30, sorted by update time)
      2. For each PR, check if we already have it
      3. If new or updated, fetch file details
      4. Detect migration and infra changes from file paths
      5. Upsert into pull_requests table
    """
    async with GitHubClient() as gh:
        prs = await gh.list_pull_requests(
            owner=repo.owner,
            repo=repo.repository_name,
            state="all",
            per_page=30,
        )

        for pr_data in prs:
            pr_number = pr_data["number"]

            # Check if PR already exists
            existing = await db.execute(
                select(PullRequest).where(
                    PullRequest.repository_id == repo.id,
                    PullRequest.pr_number == pr_number,
                )
            )
            existing_pr = existing.scalar_one_or_none()

            # Fetch detailed PR data (files changed, lines)
            pr_detail = await gh.get_pull_request_detail(
                owner=repo.owner,
                repo=repo.repository_name,
                pr_number=pr_number,
            )

            # Fetch files for migration/infra detection
            pr_files = await gh.get_pull_request_files(
                owner=repo.owner,
                repo=repo.repository_name,
                pr_number=pr_number,
            )

            has_migration = detect_migration(pr_files)
            has_infra = detect_infra_change(pr_files)

            if existing_pr:
                # Update existing PR
                existing_pr.state = pr_data.get("state")
                existing_pr.title = pr_data.get("title")
                existing_pr.files_changed = pr_detail.get("changed_files", 0)
                existing_pr.lines_added = pr_detail.get("additions", 0)
                existing_pr.lines_deleted = pr_detail.get("deletions", 0)
                existing_pr.has_db_migration = has_migration
                existing_pr.has_infra_change = has_infra
                if pr_data.get("merged_at"):
                    existing_pr.merged_at = datetime.fromisoformat(
                        pr_data["merged_at"].replace("Z", "+00:00")
                    )
            else:
                # Create new PR record
                created_at_str = pr_data.get("created_at", "")
                created_at = (
                    datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    if created_at_str
                    else datetime.utcnow()
                )

                merged_at = None
                if pr_data.get("merged_at"):
                    merged_at = datetime.fromisoformat(pr_data["merged_at"].replace("Z", "+00:00"))

                new_pr = PullRequest(
                    repository_id=repo.id,
                    pr_number=pr_number,
                    title=pr_data.get("title"),
                    author=pr_data.get("user", {}).get("login"),
                    state=pr_data.get("state"),
                    files_changed=pr_detail.get("changed_files", 0),
                    lines_added=pr_detail.get("additions", 0),
                    lines_deleted=pr_detail.get("deletions", 0),
                    has_db_migration=has_migration,
                    has_infra_change=has_infra,
                    created_at=created_at,
                    merged_at=merged_at,
                )
                db.add(new_pr)

        logger.info(
            "repository_synced",
            owner=repo.owner,
            repository=repo.repository_name,
            prs_processed=len(prs),
        )


async def metrics_collection_job() -> None:
    """
    Collect service metrics from Prometheus.

    IMPLEMENTATION:
      Queries Prometheus HTTP API for each monitored service.
      Stores a MetricsSnapshot in the database.

    PROMETHEUS QUERIES (via the /api/v1/query endpoint):
      error_rate:   rate(http_requests_total{status=~"5.."}[5m])
                    / rate(http_requests_total[5m])
      latency_p50:  histogram_quantile(0.5,
                    rate(http_request_duration_seconds_bucket[5m]))
      latency_p95:  histogram_quantile(0.95,
                    rate(http_request_duration_seconds_bucket[5m]))
      latency_p99:  histogram_quantile(0.99,
                    rate(http_request_duration_seconds_bucket[5m]))
      request_rate: sum(rate(http_requests_total[5m]))

    GRACEFUL DEGRADATION:
      If Prometheus is unreachable or a specific query fails,
      the metric defaults to 0.0. A partial snapshot is better
      than no snapshot — the Risk Engine handles missing data.
    """
    start = time.perf_counter()
    logger.info("job_started", job="metrics_collection")

    try:
        import httpx

        from deploysense.core import get_settings
        from deploysense.models import MetricsSnapshot, Service

        settings = get_settings()
        prometheus_url = getattr(settings, "prometheus_url", "http://localhost:9090")

        async with async_session_factory() as db:
            result = await db.execute(select(Service).where(Service.status == "ACTIVE"))
            services = result.scalars().all()

            if not services:
                logger.info("job_completed", job="metrics_collection", message="no active services")
                return

            async with httpx.AsyncClient(timeout=10.0) as client:
                for service in services:
                    try:
                        # Build a job label filter for this service.
                        # Convention: Prometheus job name matches service name.
                        job_label = service.name

                        # Query each metric from Prometheus
                        error_rate = await _query_prometheus(
                            client,
                            prometheus_url,
                            f'sum(rate(http_requests_total{{job="{job_label}",status=~"5.."}}[5m]))'
                            f' / sum(rate(http_requests_total{{job="{job_label}"}}[5m]))',
                        )

                        latency_p50 = await _query_prometheus(
                            client,
                            prometheus_url,
                            f'histogram_quantile(0.5, sum(rate(http_request_duration_seconds_bucket{{job="{job_label}"}}[5m])) by (le))',
                        )

                        latency_p95 = await _query_prometheus(
                            client,
                            prometheus_url,
                            f'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{job="{job_label}"}}[5m])) by (le))',
                        )

                        latency_p99 = await _query_prometheus(
                            client,
                            prometheus_url,
                            f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{job="{job_label}"}}[5m])) by (le))',
                        )

                        request_rate = await _query_prometheus(
                            client,
                            prometheus_url,
                            f'sum(rate(http_requests_total{{job="{job_label}"}}[5m]))',
                        )

                        snapshot = MetricsSnapshot(
                            service_id=service.id,
                            error_rate=error_rate,
                            latency_p50_ms=(latency_p50 or 0.0) * 1000,  # seconds → ms
                            latency_p95_ms=(latency_p95 or 0.0) * 1000,
                            latency_p99_ms=(latency_p99 or 0.0) * 1000,
                            request_rate_rps=request_rate or 0.0,
                            cpu_usage=0.0,  # CPU/memory require node_exporter queries
                            memory_usage=0.0,
                            collected_at=datetime.utcnow(),
                        )
                        db.add(snapshot)

                        logger.info(
                            "metrics_collected",
                            service=service.name,
                            error_rate=error_rate,
                            latency_p99_ms=round((latency_p99 or 0) * 1000, 2),
                            request_rate=request_rate,
                        )

                    except Exception as e:
                        logger.error(
                            "metrics_collection_failed",
                            service=service.name,
                            error=str(e),
                        )

            await db.commit()

        logger.info(
            "job_completed",
            job="metrics_collection",
            services_processed=len(services),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
    except Exception as e:
        logger.error("job_failed", job="metrics_collection", error=str(e))


async def _query_prometheus(
    client: Any,
    prometheus_url: str,
    query: str,
) -> float:
    """
    Execute a PromQL instant query against the Prometheus HTTP API.

    Returns the scalar result as a float, or 0.0 if the query fails
    or returns no data. This ensures the caller always gets a usable value.

    PROMETHEUS API:
      GET /api/v1/query?query=<promql>
      Response: { "data": { "result": [{"value": [timestamp, "value"]}] } }
    """
    try:
        response = await client.get(
            f"{prometheus_url}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        data = response.json()

        results = data.get("data", {}).get("result", [])
        if results:
            # Instant query returns [timestamp, "value"] pairs
            value_str = results[0].get("value", [None, "0"])[1]
            value = float(value_str)
            # Handle NaN from Prometheus (e.g., 0/0 division)
            if value != value:  # NaN check
                return 0.0
            return value
        return 0.0

    except Exception as e:
        logger.debug("prometheus_query_failed", query=query[:80], error=str(e))
        return 0.0


async def risk_recalculation_job() -> None:
    """
    Recalculate risk for active deployments.

    Finds deployments in DEPLOYED or MONITORING state and re-evaluates
    risk using current metrics. If risk increased significantly (>20 points),
    creates an Alert to notify the team.

    WHY re-evaluate:
    A deployment might be low-risk at deploy time, but if metrics degrade
    after deployment (error rate spike, latency increase), the risk score
    should increase. This is continuous risk assessment.
    """
    start = time.perf_counter()
    logger.info("job_started", job="risk_recalculation")

    try:
        from deploysense.models import Alert, Deployment, RiskAssessment
        from deploysense.risk_engine.historical import collect_historical_features
        from deploysense.risk_engine.scoring import RiskFeatures, compute_enhanced_risk

        async with async_session_factory() as db:
            result = await db.execute(
                select(Deployment).where(Deployment.status.in_(["DEPLOYED", "MONITORING"]))
            )
            deployments = result.scalars().all()

            recalculated = 0
            alerts_created = 0

            for deployment in deployments:
                try:
                    # Gather current historical features (includes latest metrics)
                    service_id = str(deployment.service_id) if deployment.service_id else None
                    historical = await collect_historical_features(
                        db, service_id, deployment.environment
                    )

                    # Build risk features with current data
                    features = RiskFeatures(
                        recent_failure_count=historical.get("recent_failure_count", 0),
                        deployments_last_24h=historical.get("deployments_last_24h", 0),
                        service_stability_score=historical.get("service_stability_score", 100),
                        current_error_rate=historical.get("current_error_rate", 0.0),
                        baseline_error_rate=historical.get("baseline_error_rate", 0.0),
                    )

                    # Compute new risk score
                    risk_result = compute_enhanced_risk(features)
                    previous_score = deployment.risk_score or 0
                    new_score = risk_result.risk_score

                    # Store new risk assessment
                    assessment = RiskAssessment(
                        deployment_id=deployment.id,
                        risk_score=new_score,
                        risk_level=risk_result.risk_level,
                        failure_probability=risk_result.failure_probability,
                        recommendation=risk_result.recommendation,
                        feature_snapshot=risk_result.feature_snapshot,
                        factors={"factors": [f.to_dict() for f in risk_result.factors]},
                    )
                    db.add(assessment)

                    # Update denormalized risk on deployment
                    deployment.risk_score = new_score
                    deployment.risk_level = risk_result.risk_level
                    deployment.failure_probability = risk_result.failure_probability

                    recalculated += 1

                    # If risk increased by >20 points, create an alert
                    if new_score - previous_score > 20:
                        alert = Alert(
                            service_id=deployment.service_id,
                            deployment_id=deployment.id,
                            severity="HIGH" if new_score > 75 else "WARNING",
                            title=f"Risk score increased: {previous_score} → {new_score}",
                            description=(
                                f"Deployment risk score increased by {new_score - previous_score} points "
                                f"(from {previous_score} to {new_score}). "
                                f"Risk level: {risk_result.risk_level}. "
                                f"Recommendation: {risk_result.recommendation}."
                            ),
                            status="OPEN",
                            triggered_at=datetime.utcnow(),
                        )
                        db.add(alert)
                        alerts_created += 1

                        logger.warning(
                            "risk_increase_alert",
                            deployment_id=str(deployment.id),
                            previous_score=previous_score,
                            new_score=new_score,
                        )

                except Exception as e:
                    logger.error(
                        "risk_recalculation_failed_for_deployment",
                        deployment_id=str(deployment.id),
                        error=str(e),
                    )

            await db.commit()

            logger.info(
                "job_completed",
                job="risk_recalculation",
                active_deployments=len(deployments),
                recalculated=recalculated,
                alerts_created=alerts_created,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )

    except Exception as e:
        logger.error("job_failed", job="risk_recalculation", error=str(e))


async def cache_cleanup_job() -> None:
    """
    Clean up expired cache entries in Redis.

    Scans for orphaned keys and reports cache stats.
    Uses SCAN (not KEYS) to avoid blocking Redis on large datasets.
    """
    start = time.perf_counter()
    logger.info("job_started", job="cache_cleanup")

    try:
        import redis.asyncio as aioredis

        from deploysense.core import get_settings

        settings = get_settings()
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

        try:
            # Get cache stats
            info = await redis_client.info("memory")
            db_size = await redis_client.dbsize()

            # Scan for orphaned keys matching known patterns
            cleaned = 0
            patterns = ["session:*", "risk:*", "dashboard:*"]

            for pattern in patterns:
                async for key in redis_client.scan_iter(match=pattern, count=100):
                    ttl = await redis_client.ttl(key)
                    # Remove keys with no TTL that are older than expected
                    # (keys with TTL=-1 have no expiry set — these may be orphaned)
                    if ttl == -1:
                        await redis_client.expire(key, 3600)  # Set 1hr TTL as safety net
                        cleaned += 1

            logger.info(
                "job_completed",
                job="cache_cleanup",
                total_keys=db_size,
                memory_used=info.get("used_memory_human", "unknown"),
                keys_cleaned=cleaned,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
        finally:
            await redis_client.aclose()

    except ImportError:
        logger.info("job_skipped", job="cache_cleanup", reason="redis package not installed")
    except Exception as e:
        logger.error("job_failed", job="cache_cleanup", error=str(e))
