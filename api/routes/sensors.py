"""
api/routes/sensors.py
──────────────────────
GET /farms/{farm_id}/sensors/latest     — most recent reading
GET /farms/{farm_id}/sensors/history    — paginated time-series
GET /farms/{farm_id}/sensors/summary    — stats over a period
"""

from fastapi import APIRouter, Query
from sqlalchemy import text, select
from typing import Optional
from datetime import datetime, timedelta

from api.schemas import (
    SensorReadingResponse, SensorHistoryResponse, SensorSummaryResponse
)
from api.deps import CurrentUser, DB, get_user_farm
from db.database import SensorReadingDB

router = APIRouter(tags=["sensors"])


@router.get("/farms/{farm_id}/sensors/latest", response_model=SensorReadingResponse)
async def get_latest_reading(
    farm_id: str,
    current_user: CurrentUser,
    db: DB,
):
    """Return the most recent sensor reading for this farm."""
    await get_user_farm(farm_id, current_user, db)

    result = await db.execute(text("""
        SELECT time, farm_id, do, ph, nh3, temp, salinity, turbidity, co2,
               battery_pct, signal_rssi
        FROM sensor_readings
        WHERE farm_id = :farm_id
        ORDER BY time DESC
        LIMIT 1
    """), {"farm_id": farm_id})

    row = result.fetchone()
    if not row:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No sensor data yet for this farm",
        )
    return SensorReadingResponse(**row._mapping)


@router.get("/farms/{farm_id}/sensors/history", response_model=SensorHistoryResponse)
async def get_sensor_history(
    farm_id: str,
    current_user: CurrentUser,
    db: DB,
    hours: int = Query(default=24, ge=1, le=168, description="Hours of history (max 168 = 7 days)"),
    limit: int = Query(default=200, ge=1, le=2000),
):
    """
    Return time-series sensor readings for the requested period.
    TimescaleDB automatically handles efficient time-range queries.
    """
    await get_user_farm(farm_id, current_user, db)

    result = await db.execute(text("""
        SELECT time, farm_id, do, ph, nh3, temp, salinity, turbidity, co2,
               battery_pct, signal_rssi
        FROM sensor_readings
        WHERE farm_id = :farm_id
          AND time > NOW() - INTERVAL ':hours hours'
        ORDER BY time DESC
        LIMIT :limit
    """), {"farm_id": farm_id, "hours": hours, "limit": limit})

    # Fallback if interval syntax varies
    since = datetime.utcnow() - timedelta(hours=hours)
    result = await db.execute(text("""
        SELECT time, farm_id, do, ph, nh3, temp, salinity, turbidity, co2,
               battery_pct, signal_rssi
        FROM sensor_readings
        WHERE farm_id = :farm_id
          AND time > :since
        ORDER BY time DESC
        LIMIT :limit
    """), {"farm_id": farm_id, "since": since, "limit": limit})

    rows = result.fetchall()
    readings = [SensorReadingResponse(**r._mapping) for r in rows]

    return SensorHistoryResponse(
        farm_id=farm_id,
        count=len(readings),
        readings=readings,
    )


@router.get("/farms/{farm_id}/sensors/summary", response_model=SensorSummaryResponse)
async def get_sensor_summary(
    farm_id: str,
    current_user: CurrentUser,
    db: DB,
    hours: int = Query(default=24, ge=1, le=168),
):
    """
    Return aggregated stats (min/max/avg) for the requested period.
    Plus the most recent reading as 'latest'.
    Uses TimescaleDB time_bucket for efficient aggregation.
    """
    await get_user_farm(farm_id, current_user, db)
    since = datetime.utcnow() - timedelta(hours=hours)

    # Aggregated stats
    agg = await db.execute(text("""
        SELECT
            MIN(do)   AS do_min,   MAX(do)   AS do_max,   AVG(do)   AS do_avg,
            MIN(ph)   AS ph_min,   MAX(ph)   AS ph_max,
            MAX(nh3)  AS nh3_max,
            MIN(temp) AS temp_min, MAX(temp) AS temp_max
        FROM sensor_readings
        WHERE farm_id = :farm_id AND time > :since
    """), {"farm_id": farm_id, "since": since})

    agg_row = agg.fetchone()

    # Latest reading
    latest_result = await db.execute(text("""
        SELECT time, farm_id, do, ph, nh3, temp, salinity, turbidity, co2,
               battery_pct, signal_rssi
        FROM sensor_readings
        WHERE farm_id = :farm_id
        ORDER BY time DESC
        LIMIT 1
    """), {"farm_id": farm_id})

    latest_row = latest_result.fetchone()
    latest = SensorReadingResponse(**latest_row._mapping) if latest_row else None

    agg_map = agg_row._mapping if agg_row else {}
    return SensorSummaryResponse(
        farm_id=farm_id,
        period_hours=hours,
        latest=latest,
        do_min=agg_map.get("do_min"),
        do_max=agg_map.get("do_max"),
        do_avg=round(agg_map.get("do_avg"), 2) if agg_map.get("do_avg") else None,
        ph_min=agg_map.get("ph_min"),
        ph_max=agg_map.get("ph_max"),
        nh3_max=agg_map.get("nh3_max"),
        temp_min=agg_map.get("temp_min"),
        temp_max=agg_map.get("temp_max"),
    )
