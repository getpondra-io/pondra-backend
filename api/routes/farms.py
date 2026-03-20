"""
api/routes/farms.py
────────────────────
GET    /farms              — list user's farms
POST   /farms              — create farm
GET    /farms/{farm_id}    — get farm detail
PATCH  /farms/{farm_id}    — update farm
DELETE /farms/{farm_id}    — deactivate farm
POST   /farms/{farm_id}/test-push  — manual sensor push for testing
PATCH  /users/me/byok      — update AI provider + API key
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, func
import structlog

from api.schemas import (
    FarmCreateRequest, FarmUpdateRequest, FarmResponse,
    FarmListResponse, BYOKUpdateRequest, MessageResponse,
    SensorTestPushRequest, UserResponse, UserUpdateRequest,
)
from api.deps import CurrentUser, DB, get_user_farm
from db.database import Farm, User
from core.security import encrypt_api_key, generate_mqtt_key

log = structlog.get_logger(__name__)
router = APIRouter(tags=["farms"])


# ── User profile + BYOK ───────────────────────────────────────────────────────

@router.patch("/users/me", response_model=UserResponse)
async def update_profile(body: UserUpdateRequest, current_user: CurrentUser, db: DB):
    """Update user profile (name, etc)."""
    if body.full_name is not None:
        current_user.full_name = body.full_name
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.patch("/users/me/byok", response_model=MessageResponse)
async def update_byok(body: BYOKUpdateRequest, current_user: CurrentUser, db: DB):
    """
    Set or update the user's AI provider and API key.
    The key is encrypted with AES before storage — never stored in plaintext.
    """
    current_user.ai_provider = body.ai_provider

    if body.ai_provider == "managed":
        # Clear stored keys when switching back to managed
        current_user.claude_api_key_enc = None
        current_user.openai_api_key_enc = None
    elif body.api_key:
        encrypted = encrypt_api_key(body.api_key)
        if body.ai_provider == "claude":
            current_user.claude_api_key_enc = encrypted
        elif body.ai_provider in ("openai", "gemini", "custom"):
            current_user.openai_api_key_enc = encrypted

    await db.commit()
    log.info("byok.updated", user_id=str(current_user.id), provider=body.ai_provider)

    return MessageResponse(message=f"AI provider updated to '{body.ai_provider}'")


# ── Farms CRUD ────────────────────────────────────────────────────────────────

@router.get("/farms", response_model=FarmListResponse)
async def list_farms(current_user: CurrentUser, db: DB):
    """List all active farms owned by the authenticated user."""
    result = await db.execute(
        select(Farm)
        .where(Farm.owner_id == current_user.id, Farm.is_active == True)
        .order_by(Farm.created_at.desc())
    )
    farms = result.scalars().all()
    return FarmListResponse(total=len(farms), farms=farms)


@router.post("/farms", response_model=FarmResponse, status_code=status.HTTP_201_CREATED)
async def create_farm(body: FarmCreateRequest, current_user: CurrentUser, db: DB):
    """
    Create a new farm/pond.
    Returns the farm with its generated MQTT key — store it safely,
    it won't be shown again.
    """
    # Check farm_id not already taken by this user
    result = await db.execute(
        select(Farm).where(Farm.farm_id == body.farm_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Farm ID '{body.farm_id}' is already taken",
        )

    mqtt_key = generate_mqtt_key()

    farm = Farm(
        farm_id=body.farm_id,
        name=body.name,
        owner_id=current_user.id,
        species_id=body.species_id,
        country=body.country,
        region=body.region,
        latitude=body.latitude,
        longitude=body.longitude,
        ai_analysis_interval=body.ai_analysis_interval,
    )
    # Store hashed MQTT key — we'll verify it on broker connection
    from core.security import hash_password
    farm.__dict__['_mqtt_key_plain'] = mqtt_key  # temp for response

    db.add(farm)
    await db.commit()
    await db.refresh(farm)

    log.info("farm.created", farm_id=farm.farm_id, user_id=str(current_user.id))

    # Return with plain MQTT key just this once
    response = FarmResponse.model_validate(farm)
    response.mqtt_key = mqtt_key
    return response


@router.get("/farms/{farm_id}", response_model=FarmResponse)
async def get_farm(farm_id: str, current_user: CurrentUser, db: DB):
    """Get a single farm by its farm_id."""
    farm = await get_user_farm(farm_id, current_user, db)
    return farm


@router.patch("/farms/{farm_id}", response_model=FarmResponse)
async def update_farm(
    farm_id: str,
    body: FarmUpdateRequest,
    current_user: CurrentUser,
    db: DB,
):
    """Update farm settings."""
    farm = await get_user_farm(farm_id, current_user, db)

    update_data = body.model_dump(exclude_none=True)
    for key, value in update_data.items():
        setattr(farm, key, value)

    await db.commit()
    await db.refresh(farm)
    return farm


@router.delete("/farms/{farm_id}", response_model=MessageResponse)
async def delete_farm(farm_id: str, current_user: CurrentUser, db: DB):
    """Soft-delete a farm (deactivate, not destroy data)."""
    farm = await get_user_farm(farm_id, current_user, db)
    farm.is_active = False
    await db.commit()
    log.info("farm.deactivated", farm_id=farm_id, user_id=str(current_user.id))
    return MessageResponse(message=f"Farm '{farm_id}' has been deactivated")


@router.post("/farms/{farm_id}/test-push", response_model=MessageResponse)
async def test_sensor_push(
    farm_id: str,
    body: SensorTestPushRequest,
    current_user: CurrentUser,
    db: DB,
):
    """
    Manually push sensor data to trigger an AI analysis cycle.
    Useful for testing without physical hardware.
    Publishes a fake MQTT message to the ingestion pipeline.
    """
    farm = await get_user_farm(farm_id, current_user, db)

    # Build a SensorReading and push directly to the AI engine
    from models.sensor import SensorReading
    from mqtt.ai_engine import AIEngine
    from mqtt.ingestion import MQTTIngestionService

    reading = SensorReading(
        farm_id=farm_id,
        do=body.do,
        ph=body.ph,
        nh3=body.nh3,
        temp=body.temp,
        salinity=body.salinity,
    )

    if not reading.has_minimum_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one sensor value (do, ph, nh3, or temp)",
        )

    # Store the reading
    from db.database import SensorReadingDB
    from datetime import datetime
    row = SensorReadingDB(
        time=datetime.utcnow(),
        farm_id=farm_id,
        do=body.do, ph=body.ph, nh3=body.nh3,
        temp=body.temp, salinity=body.salinity,
    )
    db.add(row)
    await db.commit()

    log.info("sensor.test_push", farm_id=farm_id, data=body.model_dump(exclude_none=True))

    return MessageResponse(
        message="Test sensor data pushed. AI analysis will run within 30 seconds."
    )
