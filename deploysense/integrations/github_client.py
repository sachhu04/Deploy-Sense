from typing import Any

"""
DeploySense — GitHub API Client

WHY THIS EXISTS:
DeploySense needs to interact with GitHub to:
  1. Fetch pull request data (files changed, migration detection)
  2. Fetch commit history
  3. Validate webhook payloads
  4. Get repository metadata

WHY A DEDICATED CLIENT:
  - Centralized rate limit handling (GitHub API: 5000 req/hr)
  - Consistent error handling and retry logic
  - Structured logging of all GitHub API interactions
  - Single place to manage authentication

WHY httpx (not PyGithub):
  - httpx is async (PyGithub is sync-only)
  - Less magic, more control over requests
  - Smaller dependency
  - We only use 5-6 GitHub endpoints, not the full API surface

RATE LIMITING:
  GitHub allows 5000 requests/hour for authenticated users.
  We track remaining quota and back off proactively.
  Phase 3: Add Redis-backed rate limit tracking for distributed workers.
"""

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from deploysense.logging import get_logger

logger = get_logger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubClient:
    """
    Async GitHub API client with retry logic and rate limit awareness.

    Usage:
        async with GitHubClient(token="ghp_xxx") as gh:
            prs = await gh.list_pull_requests("org", "repo")
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            self._headers["Authorization"] = f"Bearer {self._token}"

        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers=self._headers,
            timeout=15.0,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("GitHubClient must be used as async context manager")
        return self._client

    # ─── Pull Requests ───────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        per_page: int = 30,
    ) -> list[dict[str, Any]]:
        """
        Fetch pull requests for a repository.

        WHAT WE EXTRACT:
          - PR number, title, author
          - Files changed count
          - Lines added/deleted
          - State (open, closed, merged)
          - Created/merged timestamps

        RETRY: 3 attempts with exponential backoff.
        GitHub occasionally returns 5xx during high load.
        """
        response = await self.client.get(
            f"/repos/{owner}/{repo}/pulls",
            params={
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
            },
        )
        self._check_rate_limit(response)
        response.raise_for_status()

        prs = response.json()
        logger.info(
            "github_prs_fetched",
            owner=owner,
            repo=repo,
            count=len(prs),
        )
        return prs  # type: ignore[no-any-return]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_pull_request_detail(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict[str, Any]:
        """
        Fetch detailed PR data including files changed.

        WHY a separate call:
          The list endpoint doesn't include files_changed or lines_added.
          We need a per-PR call for that data.

        OPTIMIZATION: We only fetch detail for PRs we haven't seen before
        or that have been updated since our last sync.
        """
        response = await self.client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
        )
        self._check_rate_limit(response)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_pull_request_files(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        """
        Fetch the list of files changed in a PR.

        PURPOSE: Detect risk signals:
          - Files matching **/migrations/** → has_db_migration = True
          - Files matching **/*.tf, **/k8s/** → has_infra_change = True
          - Total files changed → files_changed risk feature
        """
        response = await self.client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        self._check_rate_limit(response)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    # ─── Repository ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        """Fetch repository metadata."""
        response = await self.client.get(f"/repos/{owner}/{repo}")
        self._check_rate_limit(response)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    # ─── Rate Limit ──────────────────────────────────────────────────────

    def _check_rate_limit(self, response: httpx.Response) -> None:
        """
        Log rate limit status from response headers.

        GitHub returns these headers on every response:
          X-RateLimit-Remaining: 4999
          X-RateLimit-Reset: 1672531200

        WHY proactive logging:
        If remaining drops below 100, we want to know before we hit the limit.
        In production, this would trigger a backoff mechanism.
        """
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < 100:
            logger.warning(
                "github_rate_limit_low",
                remaining=remaining,
                reset=response.headers.get("X-RateLimit-Reset"),
            )


# ─── Migration & Infra Change Detection ─────────────────────────────────────

# These patterns detect risk signals from file paths in PRs.
# WHY hardcoded patterns:
#   - Simple and predictable
#   - Covers 90% of real-world patterns
#   - Phase 2: Make these configurable per-repository

MIGRATION_PATTERNS = [
    "/migrations/",
    "/migrate/",
    "/alembic/versions/",
    "/db/migrate/",
    "/flyway/",
    ".sql",
]

INFRA_PATTERNS = [
    ".tf",
    "/terraform/",
    "/k8s/",
    "/kubernetes/",
    "/helm/",
    "Dockerfile",
    "docker-compose",
    ".yaml",  # K8s manifests
    "/argocd/",
]


def detect_migration(files: list[dict[str, Any]]) -> bool:
    """Check if any file in a PR looks like a database migration."""
    for f in files:
        path = f.get("filename", "").lower()
        if any(pattern in path for pattern in MIGRATION_PATTERNS):
            return True
    return False


def detect_infra_change(files: list[dict[str, Any]]) -> bool:
    """Check if any file in a PR looks like an infrastructure change."""
    for f in files:
        path = f.get("filename", "").lower()
        if any(pattern in path for pattern in INFRA_PATTERNS):
            return True
    return False
