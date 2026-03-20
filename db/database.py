"""
Database layer — PostgreSQL + TimescaleDB
- Standard tables: farms, users, species_profiles, ai_decisions, alerts
- Hypertable: sensor_readings (time-series, auto-partitioned by TimescaleDB)
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Text, JSON,
    ForeignKey, Index, text, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from datetime import datetime
from typing import Optional, AsyncGenerator
import uuid

from config.settings import get_settings

settings = get_settings()

# ── Engine ────────────────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.debug,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Base ──────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Tables ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # BYOK: encrypted API keys per provider
    claude_api_key_enc: Mapped[Optional[str]] = mapped_column(Text)
    openai_api_key_enc: Mapped[Optional[str]] = mapped_column(Text)
    ai_provider: Mapped[str] = mapped_column(String(32), default="managed")  # managed | claude | openai | custom

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    farms: Mapped[list["Farm"]] = relationship("Farm", back_populates="owner")


class Farm(Base):
    __tablename__ = "farms"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    farm_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)

    # Location
    country: Mapped[Optional[str]] = mapped_column(String(100))
    region: Mapped[Optional[str]] = mapped_column(String(100))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)

    # Species & AI config
    species_id: Mapped[Optional[str]] = mapped_column(String(64), default="tilapia")
    growth_stage: Mapped[str] = mapped_column(String(32), default="juvenile")
    ai_analysis_interval: Mapped[int] = mapped_column(Integer, default=30)  # seconds

    # Actuator state (latest known)
    aerator_on: Mapped[bool] = mapped_column(Boolean, default=False)
    pump_on: Mapped[bool] = mapped_column(Boolean, default=False)
    heater_on: Mapped[bool] = mapped_column(Boolean, default=False)
    feeder_on: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_fed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_water_change_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped["User"] = relationship("User", back_populates="farms")


class SpeciesProfile(Base):
    """
    Water quality thresholds and feeding schedules per species.
    Community-contributed, stored as YAML in /species/ + synced to DB.
    """
    __tablename__ = "species_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "atlantic_salmon"
    common_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scientific_name: Mapped[Optional[str]] = mapped_column(String(128))

    # Thresholds (all as JSON for flexibility)
    thresholds: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    feeding_schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    disease_risk_factors: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SensorReadingDB(Base):
    """
    Time-series sensor readings.
    Converted to a TimescaleDB hypertable on first migration.
    Partitioned by time (daily chunks), auto-compressed after 7 days.
    """
    __tablename__ = "sensor_readings"

    # TimescaleDB requires time as first column of hypertable
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False, primary_key=True)
    farm_id: Mapped[str] = mapped_column(String(64), nullable=False, primary_key=True, index=True)

    # Sensor data
    do: Mapped[Optional[float]] = mapped_column(Float)
    ph: Mapped[Optional[float]] = mapped_column(Float)
    nh3: Mapped[Optional[float]] = mapped_column(Float)
    temp: Mapped[Optional[float]] = mapped_column(Float)
    salinity: Mapped[Optional[float]] = mapped_column(Float)
    turbidity: Mapped[Optional[float]] = mapped_column(Float)
    co2: Mapped[Optional[float]] = mapped_column(Float)

    # Device metadata
    battery_pct: Mapped[Optional[float]] = mapped_column(Float)
    signal_rssi: Mapped[Optional[int]] = mapped_column(Integer)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_sensor_readings_farm_time", "farm_id", "time"),
    )


class AIDecisionDB(Base):
    """Full audit log of every AI decision."""
    __tablename__ = "ai_decisions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    farm_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    severity: Mapped[str] = mapped_column(String(16))
    actions: Mapped[list] = mapped_column(JSON)
    reasoning: Mapped[str] = mapped_column(Text)
    duration_min: Mapped[Optional[int]] = mapped_column(Integer)
    feed_adjustment_pct: Mapped[Optional[int]] = mapped_column(Integer)
    alert_message: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    # AI provider info
    ai_provider: Mapped[str] = mapped_column(String(32))
    ai_model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer)

    # Snapshot of sensor data that triggered this decision
    sensor_snapshot: Mapped[dict] = mapped_column(JSON)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    farm_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    parameter: Mapped[Optional[str]] = mapped_column(String(32))  # e.g. "nh3"
    value: Mapped[Optional[float]] = mapped_column(Float)
    threshold: Mapped[Optional[float]] = mapped_column(Float)

    # Notification status
    notified_email: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_push: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)


# ── DB Init helpers ───────────────────────────────────────────────────────────

async def init_db():
    """Create all tables and set up TimescaleDB hypertable."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # TimescaleDB not available on Railway - using plain PostgreSQL
