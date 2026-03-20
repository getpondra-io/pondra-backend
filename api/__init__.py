from fastapi import APIRouter
from api.routes.auth import router as auth_router
from api.routes.farms import router as farms_router
from api.routes.sensors import router as sensors_router
from api.routes.decisions import (
    decisions_router,
    alerts_router,
    species_router,
)
from api.routes.ws import router as ws_router
from api.routes.alerts_admin import router as alerts_admin_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(farms_router)
api_router.include_router(sensors_router)
api_router.include_router(decisions_router)
api_router.include_router(alerts_router)
api_router.include_router(species_router)
api_router.include_router(ws_router)
api_router.include_router(alerts_admin_router)
