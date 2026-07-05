from typing import Any

"""
DeploySense — WebSocket Manager (Phase 2)

WHY THIS EXISTS:
The dashboard needs real-time updates. Without WebSockets, the user
must refresh the page to see:
  - Deployment status changes (PENDING → DEPLOYING → DEPLOYED)
  - New risk assessments completing
  - New alerts firing
  - AI analysis results

ARCHITECTURE (maps to architecture/03-api-definitions.md section 3.3):
  GET /ws/deployments — WebSocket connection for real-time events

EVENTS:
  deployment.created   — New deployment registered
  deployment.updated   — Deployment status changed
  risk.updated         — New risk assessment computed
  alert.created        — New alert fired
  analysis.completed   — AI analysis finished

WHY WebSocket (not SSE):
  - Bi-directional: Client can subscribe to specific services/deployments
  - Lower overhead: Single connection for all events
  - Better library support in modern browsers
  - FastAPI has native WebSocket support

SCALING:
  MVP: In-process connection manager (single API instance)
  Phase 3: Redis Pub/Sub for multi-instance broadcasting
"""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from deploysense.logging import get_logger

logger = get_logger(__name__)


class ConnectionManager:
    """
    Manages active WebSocket connections and broadcasts events.

    DESIGN:
      - Each connection is stored in a set
      - Broadcast sends to all connections concurrently
      - Disconnected clients are automatically removed
      - Thread-safe via asyncio (single event loop)

    FUTURE (Phase 3 — Multi-Instance):
      Replace direct broadcasting with Redis Pub/Sub:
        1. On event: Publish to Redis channel "deploysense:events"
        2. Each API instance subscribes to the channel
        3. On message: Broadcast to local WebSocket connections

      This decouples event producers from WebSocket handlers.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self._connections.add(websocket)
        logger.info(
            "websocket_connected",
            total_connections=len(self._connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket."""
        self._connections.discard(websocket)
        logger.info(
            "websocket_disconnected",
            total_connections=len(self._connections),
        )

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """
        Send an event to all connected clients.

        CONCURRENCY: Uses asyncio.gather to send to all connections
        simultaneously. If a connection fails, it's removed silently.
        """
        if not self._connections:
            return

        event["timestamp"] = datetime.now(UTC).isoformat()
        message = json.dumps(event)

        dead: list[WebSocket] = []
        tasks = []

        for ws in self._connections:
            if ws.client_state == WebSocketState.CONNECTED:
                tasks.append(self._safe_send(ws, message, dead))
            else:
                dead.append(ws)

        if tasks:
            await asyncio.gather(*tasks)

        for ws in dead:
            self._connections.discard(ws)

    async def _safe_send(self, ws: WebSocket, message: str, dead: list[WebSocket]) -> None:
        """Send a message, catching disconnections."""
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)

    async def send_to(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        """Send an event to a specific connection."""
        event["timestamp"] = datetime.now(UTC).isoformat()
        try:
            await websocket.send_text(json.dumps(event))
        except Exception:
            self._connections.discard(websocket)


# ─── Singleton Instance ──────────────────────────────────────────────────────
# Single connection manager shared across the application.
# FastAPI dependency injection would be cleaner, but WebSocket handlers
# and event producers both need access to the same instance.

ws_manager = ConnectionManager()


# ─── Event Helpers ───────────────────────────────────────────────────────────
# Convenience functions called by route handlers and workers when events occur.


async def emit_deployment_created(deployment_id: str, service: str, environment: str) -> None:
    await ws_manager.broadcast(
        {
            "event": "deployment.created",
            "deployment_id": deployment_id,
            "service": service,
            "environment": environment,
        }
    )


async def emit_deployment_updated(deployment_id: str, status: str) -> None:
    await ws_manager.broadcast(
        {
            "event": "deployment.updated",
            "deployment_id": deployment_id,
            "status": status,
        }
    )


async def emit_risk_updated(deployment_id: str, risk_score: int, risk_level: str) -> None:
    await ws_manager.broadcast(
        {
            "event": "risk.updated",
            "deployment_id": deployment_id,
            "risk_score": risk_score,
            "risk_level": risk_level,
        }
    )


async def emit_alert_created(alert_id: str, severity: str, title: str) -> None:
    await ws_manager.broadcast(
        {
            "event": "alert.created",
            "alert_id": alert_id,
            "severity": severity,
            "title": title,
        }
    )


async def emit_analysis_completed(analysis_id: str, deployment_id: str) -> None:
    await ws_manager.broadcast(
        {
            "event": "analysis.completed",
            "analysis_id": analysis_id,
            "deployment_id": deployment_id,
        }
    )
