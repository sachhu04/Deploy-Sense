import asyncio
import random
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from deploysense.database import async_session_factory
from deploysense.models import (
    Alert,
    Deployment,
    DeploymentEvent,
    Repository,
    RiskAssessment,
    Service,
)


async def seed_demo():
    async with async_session_factory() as db:
        repo_result = await db.execute(select(Repository).where(Repository.owner == "test-org"))
        repo = repo_result.scalar_one_or_none()

        service_result = await db.execute(select(Service).where(Service.name == "test-service"))
        service = service_result.scalar_one_or_none()

        if not repo or not service:
            print("Run seed_test_data.py first to create repo and service")
            return

        now = datetime.utcnow()

        demos = [
            {
                "version": "v2.6.1",
                "git_sha": "c1d2e3f4a5b6c7d8",
                "status": "MONITORING",
                "risk_score": 45,
                "risk_level": "MEDIUM",
                "failure_prob": 0.25,
                "time_offset": 5,
                "events": ["deployment.created", "risk.evaluated"],
                "recommendation": "MONITOR_CLOSELY",
            },
            {
                "version": "v2.6.0",
                "git_sha": "a8b7c6d5e4f3a2b1",
                "status": "STABLE",
                "risk_score": 15,
                "risk_level": "LOW",
                "failure_prob": 0.05,
                "time_offset": 60,
                "events": ["deployment.created", "deployment.stable"],
                "recommendation": "PROCEED_AUTOMATICALLY",
            },
            {
                "version": "v2.5.2",
                "git_sha": "f1a2b3c4d5e6f7a8",
                "status": "DEGRADED",
                "risk_score": 65,
                "risk_level": "MEDIUM",
                "failure_prob": 0.45,
                "time_offset": 120,
                "events": ["deployment.created", "deployment.degraded"],
                "recommendation": "MONITOR_CLOSELY",
                "alert": "API latency increased by 400ms",
            },
            {
                "version": "v2.5.1",
                "git_sha": "e4f5a6b7c8d9e0f1",
                "status": "FAILED",
                "risk_score": 88,
                "risk_level": "HIGH",
                "failure_prob": 0.85,
                "time_offset": 300,
                "events": ["deployment.created", "risk.evaluated", "deployment.failed"],
                "recommendation": "REQUIRE_MANUAL_APPROVAL",
                "alert": "Database migration locked table for >30s",
            },
            {
                "version": "v2.5.0",
                "git_sha": "d5e8f9a0b1c2d3e4",
                "status": "STABLE",
                "risk_score": 12,
                "risk_level": "LOW",
                "failure_prob": 0.02,
                "time_offset": 1440,
                "events": ["deployment.created", "deployment.stable"],
                "recommendation": "PROCEED_AUTOMATICALLY",
            },
        ]

        for demo in demos:
            res = await db.execute(select(Deployment).where(Deployment.git_sha == demo["git_sha"]))
            if res.scalar_one_or_none():
                print(f"Skipping {demo['version']}, already exists")
                continue

            dt = now - timedelta(minutes=demo["time_offset"])
            dep = Deployment(
                service=service,
                environment="production",
                version=demo["version"],
                git_sha=demo["git_sha"],
                status=demo["status"],
                risk_score=demo["risk_score"],
                risk_level=demo["risk_level"],
                failure_probability=demo["failure_prob"],
                deployed_by="sachhu04",
                initiated_at=dt,
                deployed_at=dt + timedelta(minutes=2),
                completed_at=dt + timedelta(minutes=5)
                if demo["status"] in ["STABLE", "FAILED", "DEGRADED"]
                else None,
            )
            db.add(dep)
            await db.flush()

            for ev in demo["events"]:
                db.add(
                    DeploymentEvent(
                        deployment_id=dep.id,
                        event_type=ev,
                        current_state=demo["status"],
                        message=f"Event {ev} occurred",
                    )
                )

            db.add(
                RiskAssessment(
                    deployment_id=dep.id,
                    risk_score=demo["risk_score"],
                    risk_level=demo["risk_level"],
                    failure_probability=demo["failure_prob"],
                    recommendation=demo["recommendation"],
                    feature_snapshot={
                        "files_changed": random.randint(1, 20),
                        "has_db_migration": random.choice([True, False]),
                    },
                )
            )

            if "alert" in demo:
                db.add(
                    Alert(
                        id=uuid.uuid4(),
                        service_id=service.id,
                        deployment_id=dep.id,
                        severity="critical" if demo["status"] == "FAILED" else "warning",
                        title="Performance Regression"
                        if "latency" in demo["alert"]
                        else "Deployment Failure",
                        description=demo["alert"],
                        status="RESOLVED" if demo["status"] == "STABLE" else "OPEN",
                        triggered_at=dt + timedelta(minutes=3),
                    )
                )

            print(f"Created {demo['version']}")

        await db.commit()
        print("Demo seed complete!")


if __name__ == "__main__":
    asyncio.run(seed_demo())
