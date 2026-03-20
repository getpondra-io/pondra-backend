from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid


# ── Enums ─────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(str, Enum):
    AERATOR_ON = "aerator_on"
    AERATOR_OFF = "aerator_off"
    PUMP_ON = "pump_on"
    PUMP_OFF = "pump_off"
    HEATER_ON = "heater_on"
    HEATER_OFF = "heater_off"
    FEED = "feed"
    FEED_REDUCE = "feed_reduce"
    FEED_SKIP = "feed_skip"
    ALERT = "alert"
    WATER_CHANGE = "water_change"
    NO_ACTION = "no_action"


# ── Inbound: sensor payload from hardware ─────────────────────────────────────

class SensorReading(BaseModel):
    """
    JSON payload published by hardware to:
    aqua/{farm_id}/sensors
    """
    farm_id: str = Field(..., description="Unique farm/pond identifier")
    timestamp: Optional[int] = Field(
        default=None,
        description="Unix timestamp from hardware. Uses server time if absent."
    )

    # Water quality — all optional so partial readings are accepted
    do: Optional[float] = Field(None, ge=0, le=25, description="Dissolved oxygen mg/L")
    ph: Optional[float] = Field(None, ge=0, le=14, description="pH level")
    nh3: Optional[float] = Field(None, ge=0, le=100, description="Ammonia ppm")
    temp: Optional[float] = Field(None, ge=-5, le=50, description="Temperature °C")
    salinity: Optional[float] = Field(None, ge=0, le=50, description="Salinity ppt")
    turbidity: Optional[float] = Field(None, ge=0, description="Turbidity NTU")
    co2: Optional[float] = Field(None, ge=0, description="CO2 ppm")

    # Optional metadata
    battery_pct: Optional[float] = Field(None, ge=0, le=100)
    signal_rssi: Optional[int] = None
    firmware_version: Optional[str] = None

    @field_validator("farm_id")
    @classmethod
    def farm_id_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 64:
            raise ValueError("farm_id must be 1–64 characters")
        return v

    @property
    def received_at(self) -> datetime:
        if self.timestamp:
            return datetime.utcfromtimestamp(self.timestamp)
        return datetime.utcnow()

    @property
    def has_minimum_data(self) -> bool:
        """At least one water quality reading is required."""
        return any([self.do, self.ph, self.nh3, self.temp])


# ── Validated + enriched reading (after DB lookup) ───────────────────────────

class EnrichedReading(BaseModel):
    reading: SensorReading
    farm_name: str
    species: str
    thresholds: "ThresholdConfig"
    history_1h: list["SensorReading"] = []
    last_fed_seconds_ago: Optional[int] = None
    last_water_change_seconds_ago: Optional[int] = None
    growth_stage: str = "unknown"


# ── Threshold config per species ─────────────────────────────────────────────

class ThresholdConfig(BaseModel):
    do_min: float = 5.0
    do_max: float = 15.0
    ph_min: float = 6.0
    ph_max: float = 9.0
    nh3_max: float = 1.0
    temp_min: float = 10.0
    temp_max: float = 35.0
    salinity_min: Optional[float] = None
    salinity_max: Optional[float] = None

    def evaluate(self, reading: SensorReading) -> tuple[Severity, list[str]]:
        """Evaluate a reading against thresholds. Returns severity + issues."""
        issues = []
        severity = Severity.OK

        if reading.do is not None:
            if reading.do < self.do_min:
                issues.append(f"DO low: {reading.do} mg/L (min {self.do_min})")
                severity = Severity.CRITICAL if reading.do < self.do_min * 0.8 else Severity.WARNING

        if reading.ph is not None:
            if reading.ph < self.ph_min or reading.ph > self.ph_max:
                issues.append(f"pH out of range: {reading.ph} (range {self.ph_min}–{self.ph_max})")
                severity = Severity.WARNING if severity == Severity.OK else severity

        if reading.nh3 is not None:
            if reading.nh3 > self.nh3_max:
                issues.append(f"NH₃ high: {reading.nh3} ppm (max {self.nh3_max})")
                severity = Severity.CRITICAL if reading.nh3 > self.nh3_max * 2 else Severity.WARNING

        if reading.temp is not None:
            if reading.temp < self.temp_min or reading.temp > self.temp_max:
                issues.append(f"Temp out of range: {reading.temp}°C ({self.temp_min}–{self.temp_max})")
                severity = Severity.WARNING if severity == Severity.OK else severity

        return severity, issues


# ── Outbound: AI decision / action ───────────────────────────────────────────

class AIDecision(BaseModel):
    """
    Structured output from the AI agent.
    Published back to hardware via:
    aqua/{farm_id}/actions
    """
    farm_id: str
    decided_at: datetime = Field(default_factory=datetime.utcnow)
    severity: Severity = Severity.OK
    actions: list[ActionType] = [ActionType.NO_ACTION]
    reasoning: str
    duration_min: Optional[int] = Field(None, description="Duration for timed actions")
    feed_adjustment_pct: Optional[int] = Field(
        None, ge=-100, le=50,
        description="% adjustment to next feeding. Negative = reduce."
    )
    alert_message: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0, le=1)

    # Which AI model produced this decision
    ai_provider: str = "claude"
    ai_model: str = ""

    def to_mqtt_payload(self) -> dict:
        """Serialise for publishing to hardware via MQTT."""
        payload = {
            "farm_id": self.farm_id,
            "timestamp": int(self.decided_at.timestamp()),
            "severity": self.severity.value,
            "actions": [a.value for a in self.actions],
            "reasoning": self.reasoning,
        }
        if self.duration_min:
            payload["duration_min"] = self.duration_min
        if self.feed_adjustment_pct is not None:
            payload["feed_adjustment_pct"] = self.feed_adjustment_pct
        if self.alert_message:
            payload["alert_message"] = self.alert_message
        return payload


# ── Status payload from hardware ─────────────────────────────────────────────

class HardwareStatus(BaseModel):
    """
    Published by hardware to aqua/{farm_id}/status
    Heartbeat + actuator state
    """
    farm_id: str
    timestamp: int
    online: bool = True
    aerator_on: bool = False
    pump_on: bool = False
    heater_on: bool = False
    feeder_on: bool = False
    uptime_seconds: Optional[int] = None
    firmware_version: Optional[str] = None
