"""
core/websocket.py
──────────────────
WebSocket connection manager.
Keeps track of all active browser connections per farm_id.
When new sensor data or AI decision arrives, broadcasts to all
connected clients watching that farm.
"""

import json
import asyncio
from typing import Dict, Set
from fastapi import WebSocket
import structlog

log = structlog.get_logger(__name__)


class ConnectionManager:
    """
    Manages all active WebSocket connections.
    Connections are grouped by farm_id so we only
    broadcast to clients watching the relevant farm.
    """

    def __init__(self):
        # farm_id -> set of active WebSocket connections
        self._connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, farm_id: str):
        """Accept a new WebSocket connection and register it."""
        await websocket.accept()
        if farm_id not in self._connections:
            self._connections[farm_id] = set()
        self._connections[farm_id].add(websocket)
        log.info("ws.connected", farm_id=farm_id, total=len(self._connections[farm_id]))

    def disconnect(self, websocket: WebSocket, farm_id: str):
        """Remove a disconnected WebSocket."""
        if farm_id in self._connections:
            self._connections[farm_id].discard(websocket)
            if not self._connections[farm_id]:
                del self._connections[farm_id]
        log.info("ws.disconnected", farm_id=farm_id)

    async def broadcast(self, farm_id: str, message: dict):
        """
        Send a message to all clients watching this farm.
        Automatically removes dead connections.
        """
        if farm_id not in self._connections:
            return

        dead = set()
        payload = json.dumps(message, default=str)

        for ws in self._connections[farm_id]:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)

        # Clean up dead connections
        for ws in dead:
            self._connections[farm_id].discard(ws)

    def active_farms(self) -> list:
        """Return list of farm_ids with active connections."""
        return list(self._connections.keys())

    def connection_count(self, farm_id: str) -> int:
        return len(self._connections.get(farm_id, set()))


# Global singleton — imported by both routes and MQTT ingestion
manager = ConnectionManager()
