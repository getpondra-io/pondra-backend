"""
AquaMind — Main Application Entry Point
"""

import asyncio
import structlog
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import get_settings
from db.database import init_db
from mqtt.ingestion import MQTTIngestionService
from api import api_router
from services.alert_engine import AlertEngine

settings = get_settings()
log = structlog.get_logger(__name__)

# ── Services (singletons) ─────────────────────────────────────────────────────
mqtt_service = MQTTIngestionService()
alert_engine = AlertEngine()


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("aquamind.starting", env=settings.app_env)

    # Init database + TimescaleDB hypertable
    await init_db()
    log.info("db.ready")

    # Start MQTT ingestion in background
    mqtt_task = asyncio.create_task(mqtt_service.start())
    log.info("mqtt.task_started")

    # Start alert engine (daily email scheduler)
    await alert_engine.start()

    yield  # App is running

    # Graceful shutdown
    log.info("aquamind.shutting_down")
    await alert_engine.stop()
    await mqtt_service.stop()
    mqtt_task.cancel()
    try:
        await mqtt_task
    except asyncio.CancelledError:
        pass
    log.info("aquamind.stopped")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="AquaMind API",
    description="Autonomous AI aquaculture monitoring platform",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else ["https://app.aquamind.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}

@app.get("/")
async def root():
    return {"service": "Pondra", "version": "1.0.0"}

app.include_router(api_router)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )
