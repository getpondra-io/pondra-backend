"""
api/schemas.py
───────────────
All Pydantic request/response models for the REST API.
Kept in one file for easy reference — split by domain section.
"""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: Optional[str] = Field(None, max_length=255)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


# ══════════════════════════════════════════════════════════════════════════════
# USER
# ══════════════════════════════════════════════════════════════════════════════

class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    is_active: bool
    ai_provider: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(None, max_length=255)


class BYOKUpdateRequest(BaseModel):
    """Update the user's Bring-Your-Own-Key AI provider config."""
    ai_provider: str = Field(..., pattern="^(managed|claude|openai|gemini|custom)$")
    api_key: Optional[str] = Field(None, min_length=10, max_length=512)
    custom_endpoint: Optional[str] = Field(None, max_length=512)


# ══════════════════════════════════════════════════════════════════════════════
# FARM
# ══════════════════════════════════════════════════════════════════════════════

class FarmCreateRequest(BaseModel):
    farm_id: str = Field(..., min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=255)
    species_id: str = Field(default="tilapia", max_length=64)
    country: Optional[str] = Field(None, max_length=100)
    region: Optional[str] = Field(None, max_length=100)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    ai_analysis_interval: int = Field(default=30, ge=10, le=3600)


class FarmUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    species_id: Optional[str] = Field(None, max_length=64)
    growth_stage: Optional[str] = Field(None, max_length=32)
    country: Optional[str] = Field(None, max_length=100)
    region: Optional[str] = Field(None, max_length=100)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    ai_analysis_interval: Optional[int] = Field(None, ge=10, le=3600)


class FarmResponse(BaseModel):
    id: uuid.UUID
    farm_id: str
    name: str
    species_id: Optional[str]
    growth_stage: str
    country: Optional[str]
    region: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    ai_analysis_interval: int
    aerator_on: bool
    pump_on: bool
    heater_on: bool
    feeder_on: bool
    last_seen_at: Optional[datetime]
    last_fed_at: Optional[datetime]
    last_water_change_at: Optional[datetime]
    created_at: datetime
    is_active: bool
    mqtt_key: Optional[str] = None  # only returned on creation

    model_config = {"from_attributes": True}


class FarmListResponse(BaseModel):
    total: int
    farms: List[FarmResponse]


# ══════════════════════════════════════════════════════════════════════════════
# SENSOR READINGS
# ══════════════════════════════════════════════════════════════════════════════

class SensorReadingResponse(BaseModel):
    time: datetime
    farm_id: str
    do: Optional[float]
    ph: Optional[float]
    nh3: Optional[float]
    temp: Optional[float]
    salinity: Optional[float]
    turbidity: Optional[float]
    co2: Optional[float]
    battery_pct: Optional[float]
    signal_rssi: Optional[int]

    model_config = {"from_attributes": True}


class SensorHistoryResponse(BaseModel):
    farm_id: str
    count: int
    readings: List[SensorReadingResponse]


class SensorSummaryResponse(BaseModel):
    """Latest reading + min/max/avg over requested period."""
    farm_id: str
    period_hours: int
    latest: Optional[SensorReadingResponse]
    do_min: Optional[float]
    do_max: Optional[float]
    do_avg: Optional[float]
    ph_min: Optional[float]
    ph_max: Optional[float]
    nh3_max: Optional[float]
    temp_min: Optional[float]
    temp_max: Optional[float]


# Manual sensor test push (for testing without hardware)
class SensorTestPushRequest(BaseModel):
    do: Optional[float] = Field(None, ge=0, le=25)
    ph: Optional[float] = Field(None, ge=0, le=14)
    nh3: Optional[float] = Field(None, ge=0, le=100)
    temp: Optional[float] = Field(None, ge=-5, le=50)
    salinity: Optional[float] = Field(None, ge=0, le=50)


# ══════════════════════════════════════════════════════════════════════════════
# AI DECISIONS
# ══════════════════════════════════════════════════════════════════════════════

class AIDecisionResponse(BaseModel):
    id: uuid.UUID
    farm_id: str
    decided_at: datetime
    severity: str
    actions: List[str]
    reasoning: str
    duration_min: Optional[int]
    feed_adjustment_pct: Optional[int]
    alert_message: Optional[str]
    confidence: float
    ai_provider: str
    ai_model: str

    model_config = {"from_attributes": True}


class AIDecisionListResponse(BaseModel):
    farm_id: str
    total: int
    decisions: List[AIDecisionResponse]


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

class AlertResponse(BaseModel):
    id: uuid.UUID
    farm_id: str
    created_at: datetime
    severity: str
    message: str
    parameter: Optional[str]
    value: Optional[float]
    threshold: Optional[float]
    is_resolved: bool
    resolved_at: Optional[datetime]

    model_config = {"from_attributes": True}


class AlertListResponse(BaseModel):
    total: int
    alerts: List[AlertResponse]


class AlertResolveRequest(BaseModel):
    note: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# SPECIES
# ══════════════════════════════════════════════════════════════════════════════

class SpeciesResponse(BaseModel):
    id: str
    common_name: str
    scientific_name: Optional[str]
    thresholds: dict
    feeding_schedule: dict
    notes: Optional[str]

    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC
# ══════════════════════════════════════════════════════════════════════════════

class MessageResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    detail: str


class PaginatedMeta(BaseModel):
    page: int
    limit: int
    total: int
    pages: int
