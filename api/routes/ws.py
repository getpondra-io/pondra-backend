"""
api/routes/ws.py
─────────────────
WebSocket endpoints:

  WS /ws/{farm_id}?token=<jwt>
    — Live sensor readings + AI decisions for one farm
    — Client receives JSON messages as data arrives

  WS /ws/farms/all?token=<jwt>
    — Lightweight heartbeat stream for all user's farms
    — Useful for multi-farm dashboard overview
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError
import uuid
import asyncio
import json

from core.websocket import manager
from core.security import decode_token
from db.database import get_db, User, Farm

router = APIRouter(tags=["websocket"])


async def _authenticate_ws(token: str, db: AsyncSession) -> User | None:
    """
    Authenticate a WebSocket connection via JWT query param.
    Returns User or None if invalid.
    """
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        token_type = payload.get("type")
        if not user_id or token_type != "access":
            return None
    except JWTError:
        return None

    result = await db.execute(
        select(User).where(User.id == uuid.UUID(user_id), User.is_active == True)
    )
    return result.scalar_one_or_none()


async def _verify_farm_ownership(
    farm_id: str, user: User, db: AsyncSession
) -> Farm | None:
    result = await db.execute(
        select(Farm).where(
            Farm.farm_id == farm_id,
            Farm.owner_id == user.id,
            Farm.is_active == True,
        )
    )
    return result.scalar_one_or_none()


@router.websocket("/ws/{farm_id}")
async def websocket_farm(
    websocket: WebSocket,
    farm_id: str,
    token: str = Query(..., description="JWT access token"),
):
    """
    Real-time stream for a single farm.

    Message types sent to client:
      { "type": "sensor", "data": { ...SensorReading } }
      { "type": "decision", "data": { ...AIDecision } }
      { "type": "alert", "data": { ...Alert } }
      { "type": "ping", "ts": "..." }
    """
    # Auth + farm ownership check
    async for db in get_db():
        user = await _authenticate_ws(token, db)
        if not user:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        farm = await _verify_farm_ownership(farm_id, user, db)
        if not farm:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await manager.connect(websocket, farm_id)

    try:
        # Send welcome message with current farm status
        await websocket.send_text(json.dumps({
            "type": "connected",
            "farm_id": farm_id,
            "message": f"Watching {farm.name} — live updates active",
        }))

        # Keep connection alive — ping every 30s
        while True:
            try:
                # Wait for client ping or timeout
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send server ping to keep connection alive
                await websocket.send_text(json.dumps({
                    "type": "ping",
                }))

    except WebSocketDisconnect:
        manager.disconnect(websocket, farm_id)
