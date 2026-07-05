import asyncio
import subprocess
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


def get_git_info():
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    stat = subprocess.check_output(["git", "diff", "--shortstat", "HEAD~1"]).decode().strip()
    # 2 files changed, 20 insertions(+), 5 deletions(-)
    files_changed = 1
    insertions = 10
    deletions = 5
    if stat:
        parts = stat.split(",")
        try:
            files_changed = int(parts[0].strip().split(" ")[0])
            if len(parts) > 1:
                insertions = int(parts[1].strip().split(" ")[0])
            if len(parts) > 2:
                deletions = int(parts[2].strip().split(" ")[0])
        except Exception:
            pass
    return sha, files_changed, insertions, deletions


async def create_real_deploy():
    sha, fc, ins, dls = get_git_info()

    async with async_session_factory() as db:
        repo_result = await db.execute(select(Repository).where(Repository.owner == "test-org"))
        repo_result.scalar_one_or_none()

        service_result = await db.execute(select(Service).where(Service.name == "test-service"))
        service = service_result.scalar_one_or_none()

        now = datetime.utcnow()
        dt = now - timedelta(minutes=2)

        dep = Deployment(
            service=service,
            environment="production",
            version="v3.0.0-real",
            git_sha=sha[:8] + "-real",
            status="FAILED",
            risk_score=75,
            risk_level="HIGH",
            failure_probability=0.75,
            deployed_by="sachhu04",
            initiated_at=dt,
            deployed_at=dt + timedelta(minutes=1),
            completed_at=dt + timedelta(minutes=2),
        )
        db.add(dep)
        await db.flush()

        db.add(
            DeploymentEvent(
                deployment_id=dep.id,
                event_type="deployment.failed",
                current_state="FAILED",
                message="Deployment failed after UI rendering error detected in production.",
            )
        )

        db.add(
            RiskAssessment(
                deployment_id=dep.id,
                risk_score=75,
                risk_level="HIGH",
                failure_probability=0.75,
                recommendation="ROLLBACK_IMMEDIATELY",
                feature_snapshot={
                    "files_changed": fc,
                    "lines_added": ins,
                    "lines_deleted": dls,
                    "has_db_migration": False,
                    "has_infra_change": False,
                },
                factors=[
                    {"name": "High Code Churn", "impact": "+15"},
                    {"name": "No DB Migration", "impact": "-5"},
                ],
            )
        )

        db.add(
            Alert(
                id=uuid.uuid4(),
                service_id=service.id,
                deployment_id=dep.id,
                severity="critical",
                title="Next.js SSR Crash",
                description="TypeError: Cannot read properties of undefined (reading 'bg') at a.s.status",
                status="OPEN",
                triggered_at=now,
            )
        )

        await db.commit()
        print(f"Real deployment {dep.version} created for sha {sha[:8]}!")


if __name__ == "__main__":
    asyncio.run(create_real_deploy())
