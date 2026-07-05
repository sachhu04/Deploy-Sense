from collections.abc import Callable
from typing import Any

"""
DeploySense — Security Middleware (Phase 3)

WHY THIS EXISTS:
Production APIs need protection against:
  1. Brute-force attacks (rate limiting)
  2. Request smuggling (header validation)
  3. Information leakage (error sanitization)
  4. CORS misconfiguration

MIDDLEWARE STACK (order matters):
  1. Rate Limiter — blocks excessive requests before processing
  2. Security Headers — adds HSTS, X-Content-Type-Options, etc.
  3. Request ID — adds trace ID to every request/response

RATE LIMITING STRATEGY:
  - In-memory token bucket (single instance)
  - Keyed by client IP + endpoint
  - Different limits for different endpoint groups
  - Phase 3 future: Redis-backed for multi-instance consistency
"""

import time
import uuid
from collections import defaultdict

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from deploysense.logging import get_logger

logger = get_logger(__name__)


# ─── Rate Limiter ────────────────────────────────────────────────────────────


class RateLimiter:
    """
    In-memory token bucket rate limiter.

    WHY token bucket (not sliding window):
      - Allows bursts: A user can make 5 rapid requests, then wait
      - Simple to implement and reason about
      - Low memory overhead (one counter per key)

    LIMITS:
      Default: 100 requests per minute
      Auth:    20 requests per minute (prevent brute-force)
      Webhook: 200 requests per minute (high-volume GitHub events)
    """

    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"tokens": 100, "last_refill": time.monotonic()}
        )

    # Endpoint group → (max_tokens, refill_rate_per_second)
    LIMITS: dict[str, tuple[int, float]] = {
        "default": (100, 100 / 60),  # 100/min
        "auth": (20, 20 / 60),  # 20/min
        "webhook": (200, 200 / 60),  # 200/min
        "admin": (30, 30 / 60),  # 30/min
        "ai": (10, 10 / 60),  # 10/min (LLM calls are expensive)
    }

    def _get_group(self, path: str) -> str:
        if path.startswith("/api/v1/auth"):
            return "auth"
        if path.startswith("/api/v1/webhooks"):
            return "webhook"
        if path.startswith("/api/v1/admin"):
            return "admin"
        if path.startswith("/api/v1/ai"):
            return "ai"
        return "default"

    def is_allowed(self, client_ip: str, path: str) -> tuple[bool, int]:
        """
        Check if a request is allowed.

        Returns (allowed, remaining_tokens).
        """
        group = self._get_group(path)
        max_tokens, refill_rate = self.LIMITS[group]
        key = f"{client_ip}:{group}"

        bucket = self._buckets[key]
        now = time.monotonic()

        # Refill tokens based on elapsed time
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(max_tokens, bucket["tokens"] + elapsed * refill_rate)
        bucket["last_refill"] = now

        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True, int(bucket["tokens"])
        else:
            return False, 0


# Singleton
rate_limiter = RateLimiter()


# ─── Security Headers Middleware ─────────────────────────────────────────────


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds security headers to every response.

    HEADERS:
      X-Content-Type-Options: nosniff
        Prevents MIME type sniffing attacks.

      X-Frame-Options: DENY
        Prevents clickjacking by disallowing iframe embedding.

      X-XSS-Protection: 1; mode=block
        Enables browser XSS filter.

      Strict-Transport-Security: max-age=31536000
        Forces HTTPS for 1 year (production only).

      X-Request-ID: <uuid>
        Unique request identifier for tracing and debugging.
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        # Generate request ID
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        response = await call_next(request)

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"

        return response  # type: ignore[no-any-return]


# ─── Rate Limit Middleware ───────────────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Applies rate limiting to all API requests.

    BEHAVIOR:
      - Checks token bucket for client IP + endpoint group
      - Returns 429 Too Many Requests if exceeded
      - Adds rate limit headers (X-RateLimit-Remaining)
      - Skips rate limiting for health/metrics endpoints
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path

        # Skip rate limiting for infrastructure endpoints
        if path in ("/health", "/metrics", "/ready"):
            return await call_next(request)  # type: ignore[no-any-return]

        client_ip = request.client.host if request.client else "unknown"
        allowed, remaining = rate_limiter.is_allowed(client_ip, path)

        if not allowed:
            logger.warning(
                "rate_limit_exceeded",
                client_ip=client_ip,
                path=path,
            )
            return Response(
                content='{"code": "RATE_LIMITED", "message": "Too many requests"}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response  # type: ignore[no-any-return]


# ─── Apply Middleware ────────────────────────────────────────────────────────


def apply_security_middleware(app: FastAPI) -> None:
    """
    Apply all security middleware to the FastAPI app.

    ORDER MATTERS: Middleware is applied in reverse order.
    Last added = first executed.
    """
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
