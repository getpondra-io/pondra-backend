"""
AquaMind MQTT Ingestion Service
────────────────────────────────
Subscribes to:
  aqua/+/sensors   — incoming sensor readings from hardware
  aqua/+/status    — hardware heartbeat / actuator state

Publishes:
  aqua/{farm_id}/actions  — AI decision commands back to hardware

Flow:
  MQTT message → parse → validate → enrich → queue AI analysis
                                   ↓
                            store to TimescaleDB
                                   ↓
                          (async) AI analysis
                                   ↓
                          publish action back
"""

import asyncio
import json
import structlog
from datetime import datetime, timedelta
from typing import Optional

import aiomqtt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import get_settings
from db.database import AsyncSessionLocal, SensorReadingDB, AIDecisionDB, Alert, Farm
from models.sensor import (
    SensorReading, AIDecision, HardwareStatus,
    EnrichedReading, ThresholdConfig, Severity, ActionType
)
from mqtt.ai_engine import AIEngine

settings = get_settings()
log = structlog.get_logger(__name__)


class MQTTIngestionService:
    """
    Long-running async service that:
    1. Connects to the MQTT broker
    2. Subscribes to sensor + status topics
    3. Processes each message: validate → store → AI → publish
    """

    def __init__(self):
        self.ai_engine = AIEngine()
        self._client: Optional[aiomqtt.Client] = None
        self._running = False

        # Simple in-memory throttle: farm_id → last analysis timestamp
        self._last_analysis: dict[str, datetime] = {}

    # ── Entry point ──────────────────────────────────────────────────────────

    async def start(self):
        """Start the MQTT ingestion loop. Reconnects automatically on failure."""
        self._running = True
        log.info("mqtt.starting", broker=settings.mqtt_broker_host)

        while self._running:
            try:
                await self._connect_and_listen()
            except aiomqtt.MqttError as e:
                log.warning("mqtt.disconnected", error=str(e), retry_in=5)
                await asyncio.sleep(5)
            except Exception as e:
                log.error("mqtt.unexpected_error", error=str(e))
                await asyncio.sleep(10)

    async def stop(self):
        self._running = False
        log.info("mqtt.stopping")

    # ── Connection ───────────────────────────────────────────────────────────

    async def _connect_and_listen(self):
        async with aiomqtt.Client(
            hostname=settings.mqtt_broker_host,
            port=settings.mqtt_broker_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            identifier=settings.mqtt_client_id,
            keepalive=settings.mqtt_keepalive,
            tls_params=self._tls_params() if settings.mqtt_use_tls else None,
        ) as client:
            self._client = client
            log.info("mqtt.connected", broker=settings.mqtt_broker_host)

            # Subscribe to sensor data + hardware status
            await client.subscribe(settings.mqtt_topic_sensors)
            await client.subscribe(settings.mqtt_topic_status)
            log.info("mqtt.subscribed", topics=[
                settings.mqtt_topic_sensors,
                settings.mqtt_topic_status,
            ])

            async for message in client.messages:
                if not self._running:
                    break
                asyncio.create_task(self._handle_message(client, message))

    def _tls_params(self):
        import ssl
        tls = aiomqtt.TLSParameters(
            ca_certs=None,
            certfile=None,
            keyfile=None,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        return tls

    # ── Message routing ──────────────────────────────────────────────────────

    async def _handle_message(self, client: aiomqtt.Client, message: aiomqtt.Message):
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning("mqtt.invalid_json", topic=topic, error=str(e))
            return

        try:
            if "/sensors" in topic:
                await self._handle_sensor_reading(client, payload, topic)
            elif "/status" in topic:
                await self._handle_status(payload)
        except Exception as e:
            log.error("mqtt.handler_error", topic=topic, error=str(e))

    # ── Sensor reading handler ───────────────────────────────────────────────

    async def _handle_sensor_reading(
        self,
        client: aiomqtt.Client,
        payload: dict,
        topic: str,
    ):
        # 1. Parse + validate
        try:
            reading = SensorReading(**payload)
        except Exception as e:
            log.warning("mqtt.invalid_reading", topic=topic, error=str(e))
            return

        if not reading.has_minimum_data:
            log.warning("mqtt.no_sensor_data", farm_id=reading.farm_id)
            return

        log.info("mqtt.reading_received",
            farm_id=reading.farm_id,
            do=reading.do, ph=reading.ph,
            nh3=reading.nh3, temp=reading.temp,
        )

        async with AsyncSessionLocal() as db:
            # 2. Look up farm + validate it exists
            farm = await self._get_farm(db, reading.farm_id)
            if not farm:
                log.warning("mqtt.unknown_farm", farm_id=reading.farm_id)
                return

            # 3. Update farm last_seen_at
            farm.last_seen_at = datetime.utcnow()
            await db.commit()

            # 4. Store reading to TimescaleDB
            await self._store_reading(db, reading)

            # 4b. Broadcast sensor reading to WebSocket clients
            from core.websocket import manager as ws_manager
            await ws_manager.broadcast(reading.farm_id, {
                "type": "sensor",
                "data": {
                    "time": datetime.utcnow().isoformat(),
                    "farm_id": reading.farm_id,
                    "do": reading.do,
                    "ph": reading.ph,
                    "nh3": reading.nh3,
                    "temp": reading.temp,
                    "salinity": reading.salinity,
                    "turbidity": reading.turbidity,
                }
            })

            # 5. Check if AI analysis is due (throttled per farm)
            if self._should_analyse(reading.farm_id, farm.ai_analysis_interval):
                enriched = await self._enrich_reading(db, reading, farm)
                decision = await self.ai_engine.analyse(enriched)

                # 6. Store AI decision
                await self._store_decision(db, decision, reading)
                await db.commit()

                # 6b. Broadcast AI decision to WebSocket clients
                await ws_manager.broadcast(reading.farm_id, {
                    "type": "decision",
                    "data": {
                        "farm_id": decision.farm_id,
                        "severity": decision.severity.value,
                        "actions": [a.value for a in decision.actions],
                        "reasoning": decision.reasoning,
                        "confidence": decision.confidence,
                        "alert_message": decision.alert_message,
                    }
                })

                # 7. Publish action back to hardware
                await self._publish_action(client, decision)

                # 8. Create alert if needed
                if decision.severity in (Severity.WARNING, Severity.CRITICAL):
                    await self._create_alert(db, decision, reading)
                    await db.commit()

                    # 8b. Broadcast alert to WebSocket clients
                    await ws_manager.broadcast(reading.farm_id, {
                        "type": "alert",
                        "data": {
                            "farm_id": reading.farm_id,
                            "severity": decision.severity.value,
                            "message": decision.alert_message,
                        }
                    })

    # ── Status handler ───────────────────────────────────────────────────────

    async def _handle_status(self, payload: dict):
        try:
            status = HardwareStatus(**payload)
        except Exception as e:
            log.warning("mqtt.invalid_status", error=str(e))
            return

        async with AsyncSessionLocal() as db:
            farm = await self._get_farm(db, status.farm_id)
            if not farm:
                return

            # Sync actuator state from hardware
            farm.last_seen_at = datetime.utcnow()
            farm.aerator_on = status.aerator_on
            farm.pump_on = status.pump_on
            farm.heater_on = status.heater_on
            farm.feeder_on = status.feeder_on
            await db.commit()

            log.debug("mqtt.status_updated",
                farm_id=status.farm_id,
                aerator=status.aerator_on,
                pump=status.pump_on,
            )

    # ── AI publish ───────────────────────────────────────────────────────────

    async def _publish_action(self, client: aiomqtt.Client, decision: AIDecision):
        topic = f"aqua/{decision.farm_id}/actions"
        payload = json.dumps(decision.to_mqtt_payload())

        await client.publish(topic, payload, qos=1)
        log.info("mqtt.action_published",
            farm_id=decision.farm_id,
            actions=decision.actions,
            severity=decision.severity,
        )

    # ── DB helpers ───────────────────────────────────────────────────────────

    async def _get_farm(self, db: AsyncSession, farm_id: str) -> Optional[Farm]:
        result = await db.execute(
            select(Farm).where(Farm.farm_id == farm_id, Farm.is_active == True)
        )
        return result.scalar_one_or_none()

    async def _store_reading(self, db: AsyncSession, reading: SensorReading):
        row = SensorReadingDB(
            time=reading.received_at,
            farm_id=reading.farm_id,
            do=reading.do,
            ph=reading.ph,
            nh3=reading.nh3,
            temp=reading.temp,
            salinity=reading.salinity,
            turbidity=reading.turbidity,
            co2=reading.co2,
            battery_pct=reading.battery_pct,
            signal_rssi=reading.signal_rssi,
            firmware_version=reading.firmware_version,
        )
        db.add(row)
        await db.commit()

    async def _enrich_reading(
        self,
        db: AsyncSession,
        reading: SensorReading,
        farm: Farm,
    ) -> EnrichedReading:
        """Fetch history, species thresholds, and feeding context."""

        # Last hour of readings for this farm
        result = await db.execute(text("""
            SELECT time, "do", ph, nh3, temp, salinity
            FROM sensor_readings
            WHERE farm_id = :farm_id
              AND time > NOW() - INTERVAL '1 hour'
            ORDER BY time DESC
            LIMIT 120
        """), {"farm_id": reading.farm_id})

        history_rows = result.fetchall()
        history = [
            SensorReading(
                farm_id=reading.farm_id,
                timestamp=int(r.time.timestamp()),
                do=r.do, ph=r.ph, nh3=r.nh3, temp=r.temp, salinity=r.salinity,
            )
            for r in history_rows
        ]

        # Species thresholds
        thresholds = await self._get_thresholds(db, farm.species_id or "tilapia")

        # Time since last feed / water change
        last_fed_secs = None
        if farm.last_fed_at:
            last_fed_secs = int((datetime.utcnow() - farm.last_fed_at).total_seconds())

        last_wc_secs = None
        if farm.last_water_change_at:
            last_wc_secs = int((datetime.utcnow() - farm.last_water_change_at).total_seconds())

        return EnrichedReading(
            reading=reading,
            farm_name=farm.name,
            species=farm.species_id or "tilapia",
            thresholds=thresholds,
            history_1h=history,
            last_fed_seconds_ago=last_fed_secs,
            last_water_change_seconds_ago=last_wc_secs,
            growth_stage=farm.growth_stage,
        )

    async def _get_thresholds(self, db: AsyncSession, species_id: str) -> ThresholdConfig:
        from db.database import SpeciesProfile
        result = await db.execute(
            select(SpeciesProfile).where(SpeciesProfile.id == species_id)
        )
        profile = result.scalar_one_or_none()

        if profile and profile.thresholds:
            return ThresholdConfig(**profile.thresholds)

        # Fallback to global defaults
        return ThresholdConfig(
            do_min=settings.default_do_min,
            ph_min=settings.default_ph_min,
            ph_max=settings.default_ph_max,
            nh3_max=settings.default_nh3_max,
            temp_min=settings.default_temp_min,
            temp_max=settings.default_temp_max,
        )

    async def _store_decision(
        self,
        db: AsyncSession,
        decision: AIDecision,
        reading: SensorReading,
    ):
        row = AIDecisionDB(
            farm_id=decision.farm_id,
            decided_at=decision.decided_at,
            severity=decision.severity.value,
            actions=[a.value for a in decision.actions],
            reasoning=decision.reasoning,
            duration_min=decision.duration_min,
            feed_adjustment_pct=decision.feed_adjustment_pct,
            alert_message=decision.alert_message,
            confidence=decision.confidence,
            ai_provider=decision.ai_provider,
            ai_model=decision.ai_model,
            sensor_snapshot=reading.model_dump(exclude_none=True),
        )
        db.add(row)

    async def _create_alert(
        self,
        db: AsyncSession,
        decision: AIDecision,
        reading: SensorReading,
    ):
        alert = Alert(
            farm_id=decision.farm_id,
            severity=decision.severity.value,
            message=decision.alert_message or decision.reasoning[:500],
        )
        db.add(alert)
        log.warning("alert.created",
            farm_id=decision.farm_id,
            severity=decision.severity,
            message=alert.message[:80],
        )

    # ── Throttle ─────────────────────────────────────────────────────────────

    def _should_analyse(self, farm_id: str, interval_seconds: int) -> bool:
        """Rate-limit AI analysis per farm."""
        now = datetime.utcnow()
        last = self._last_analysis.get(farm_id)
        if last is None or (now - last).total_seconds() >= interval_seconds:
            self._last_analysis[farm_id] = now
            return True
        return False
