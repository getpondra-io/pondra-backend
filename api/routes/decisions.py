"""
api/routes/decisions.py
api/routes/alerts.py
api/routes/species.py
─────────────────────
Combined in one file for brevity.
"""

from fastapi import APIRouter, Query, HTTPException, status
from sqlalchemy import select, text, func
from datetime import datetime, timedelta

from api.schemas import (
    AIDecisionResponse, AIDecisionListResponse,
    AlertResponse, AlertListResponse, AlertResolveRequest,
    SpeciesResponse, MessageResponse,
)
from api.deps import CurrentUser, DB, get_user_farm
from db.database import AIDecisionDB, Alert, SpeciesProfile

# ── Decisions ─────────────────────────────────────────────────────────────────

decisions_router = APIRouter(tags=["decisions"])


@decisions_router.get(
    "/farms/{farm_id}/decisions",
    response_model=AIDecisionListResponse,
)
async def list_decisions(
    farm_id: str,
    current_user: CurrentUser,
    db: DB,
    hours: int = Query(default=24, ge=1, le=168),
    severity: str = Query(default=None, pattern="^(ok|warning|critical)$"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List AI decisions for a farm, most recent first."""
    await get_user_farm(farm_id, current_user, db)
    since = datetime.utcnow() - timedelta(hours=hours)

    query = (
        select(AIDecisionDB)
        .where(AIDecisionDB.farm_id == farm_id, AIDecisionDB.decided_at > since)
        .order_by(AIDecisionDB.decided_at.desc())
        .limit(limit)
    )
    if severity:
        query = query.where(AIDecisionDB.severity == severity)

    result = await db.execute(query)
    decisions = result.scalars().all()

    return AIDecisionListResponse(
        farm_id=farm_id,
        total=len(decisions),
        decisions=decisions,
    )


@decisions_router.get(
    "/farms/{farm_id}/decisions/latest",
    response_model=AIDecisionResponse,
)
async def get_latest_decision(farm_id: str, current_user: CurrentUser, db: DB):
    """Return the most recent AI decision for this farm."""
    await get_user_farm(farm_id, current_user, db)

    result = await db.execute(
        select(AIDecisionDB)
        .where(AIDecisionDB.farm_id == farm_id)
        .order_by(AIDecisionDB.decided_at.desc())
        .limit(1)
    )
    decision = result.scalar_one_or_none()
    if not decision:
        raise HTTPException(status_code=404, detail="No AI decisions yet for this farm")
    return decision


# ── Alerts ────────────────────────────────────────────────────────────────────

alerts_router = APIRouter(tags=["alerts"])


@alerts_router.get("/farms/{farm_id}/alerts", response_model=AlertListResponse)
async def list_alerts(
    farm_id: str,
    current_user: CurrentUser,
    db: DB,
    unresolved_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List alerts for a farm."""
    await get_user_farm(farm_id, current_user, db)

    query = (
        select(Alert)
        .where(Alert.farm_id == farm_id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    if unresolved_only:
        query = query.where(Alert.is_resolved == False)

    result = await db.execute(query)
    alerts = result.scalars().all()

    return AlertListResponse(total=len(alerts), alerts=alerts)


@alerts_router.post(
    "/farms/{farm_id}/alerts/{alert_id}/resolve",
    response_model=MessageResponse,
)
async def resolve_alert(
    farm_id: str,
    alert_id: str,
    body: AlertResolveRequest,
    current_user: CurrentUser,
    db: DB,
):
    """Mark an alert as resolved."""
    await get_user_farm(farm_id, current_user, db)

    import uuid
    result = await db.execute(
        select(Alert).where(
            Alert.id == uuid.UUID(alert_id),
            Alert.farm_id == farm_id,
        )
    )
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.is_resolved = True
    alert.resolved_at = datetime.utcnow()
    await db.commit()

    return MessageResponse(message="Alert resolved")


# ── Species ───────────────────────────────────────────────────────────────────

species_router = APIRouter(tags=["species"])


@species_router.get("/species", response_model=list[SpeciesResponse])
async def list_species(db: DB):
    """List all available species profiles. No auth required."""
    result = await db.execute(
        select(SpeciesProfile)
        .where(SpeciesProfile.is_active == True)
        .order_by(SpeciesProfile.common_name)
    )
    return result.scalars().all()


@species_router.get("/species/{species_id}", response_model=SpeciesResponse)
async def get_species(species_id: str, db: DB):
    """Get a species profile by ID."""
    result = await db.execute(
        select(SpeciesProfile).where(
            SpeciesProfile.id == species_id,
            SpeciesProfile.is_active == True,
        )
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail=f"Species '{species_id}' not found")
    return profile
