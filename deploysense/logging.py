"""
DeploySense — Structured Logging

WHY THIS EXISTS:
Production systems need structured (JSON) logs, not print statements.
Structured logs enable:
  1. Machine parsing by Loki/ELK
  2. Filtering by service, level, request_id, deployment_id
  3. Correlation across services via trace_id
  4. Consistent format across all three services

WHY structlog:
  - Produces JSON in production, human-readable in development
  - Adds context processors (timestamps, log level, caller info)
  - Thread-safe context binding (add request_id once, appears in all logs)
  - Zero-dependency on logging infrastructure (works with stdlib logging)

ALTERNATIVE: python-json-logger — simpler but lacks context binding.
"""

import logging
import sys
from typing import Any

import structlog

from deploysense.core import get_settings


def setup_logging() -> None:
    """
    Configure structured logging for the entire application.

    Call this once at application startup, before any log statements.
    """
    settings = get_settings()

    # Development: human-readable colored output
    # Production: JSON for machine parsing
    renderer: Any
    if settings.is_development:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.CallsiteParameterAdder(
                [
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                ],
            ),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger instance.

    Usage:
        logger = get_logger(__name__)
        logger.info("deployment_created", deployment_id="dep_123", service="payments-api")
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
