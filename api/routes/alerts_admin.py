"""
api/routes/alerts_admin.py
───────────────────────────
Admin endpoints for alert engine management.
POST /admin/alerts/send-summary  — manually trigger daily summary email (for testing)
"""

from fastapi import APIRouter
from api.deps import CurrentUser
from api.schemas import MessageResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/alerts/send-summary", response_model=MessageResponse)
async def trigger_summary(current_user: CurrentUser):
    """
    Manually trigger the daily summary email for the current user.
    Useful for testing — sends immediately without waiting for 07:00.
    """
    from main import alert_engine
    import asyncio
    asyncio.create_task(alert_engine.send_now())
    return MessageResponse(message="Daily summary email queued — check your inbox in a moment")
