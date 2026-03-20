from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_name: str = "AquaMind"
    app_env: Literal["development", "production"] = "development"
    app_secret_key: str = "change-me"
    debug: bool = True

    # ── API ──────────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://aquamind:password@localhost:5432/aquamind"
    database_pool_size: int = 20
    database_max_overflow: int = 40

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── MQTT ─────────────────────────────────────────────────────────────────
    mqtt_broker_host: str = "localhost"
    mqtt_broker_port: int = 1883
    mqtt_broker_port_tls: int = 8883
    mqtt_username: str = "aquamind"
    mqtt_password: str = "change-me"
    mqtt_client_id: str = "aquamind-server"
    mqtt_use_tls: bool = False
    mqtt_keepalive: int = 60

    mqtt_topic_sensors: str = "aqua/+/sensors"
    mqtt_topic_status: str = "aqua/+/status"

    # ── AI ───────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-5"
    ai_max_tokens: int = 1024
    ai_analysis_interval: int = 30  # seconds

    # ── Auth ─────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-jwt-secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440
    jwt_refresh_token_expire_days: int = 30

    # ── Notifications ────────────────────────────────────────────────────────
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "alerts@pondra.xyz"
    notification_from_email: str = "alerts@pondra.xyz"
    gmail_user: str = ""
    gmail_app_password: str = ""

    # ── Sensor defaults ──────────────────────────────────────────────────────
    default_do_min: float = 5.0
    default_ph_min: float = 6.0
    default_ph_max: float = 9.0
    default_nh3_max: float = 1.0
    default_temp_min: float = 10.0
    default_temp_max: float = 35.0

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
