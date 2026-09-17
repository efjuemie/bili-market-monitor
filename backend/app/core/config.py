from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]
VERSION_FILE = ROOT_DIR / "VERSION"


class Settings(BaseSettings):
    app_env: str = "development"
    app_base_url: str = "http://localhost:5173"
    app_timezone: str = "Asia/Shanghai"
    version: str = "0.1.1"
    database_url: str = "sqlite:///./app.db"

    session_cookie_name: str = "session"
    session_ttl_days: int = Field(default=14, ge=1, le=90)
    session_cookie_secure: bool = False

    bili_api_base_url: str = "https://mall.bilibili.com"
    bili_request_timeout_seconds: float = Field(default=10, gt=0, le=60)
    bili_max_concurrency: int = Field(default=4, ge=1, le=32)
    bili_product_lock_timeout_seconds: float = Field(default=2, gt=0, le=30)
    bili_global_min_interval_seconds: int = Field(default=10, ge=10)
    bili_cache_reuse_seconds: int = Field(default=5, ge=0)
    bili_price_history_retention_days: int = Field(default=90, ge=1)
    bili_request_event_retention_days: int = Field(default=30, ge=1)
    manual_refresh_cooldown_seconds: int = Field(default=30, ge=1)

    email_verify_code_ttl_minutes: int = Field(default=10, ge=1)
    email_verify_resend_cooldown_seconds: int = Field(default=60, ge=1)
    email_verify_max_attempts: int = Field(default=5, ge=1)
    password_reset_ttl_minutes: int = Field(default=15, ge=1)
    login_rate_limit_window_seconds: int = Field(default=60, ge=1)
    login_rate_limit_max_attempts: int = Field(default=10, ge=1)
    forgot_password_rate_limit_window_seconds: int = Field(default=3600, ge=1)
    forgot_password_rate_limit_max_attempts: int = Field(default=5, ge=1)

    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "B站市集好价提示系统"
    smtp_use_tls: bool = True
    smtp_unconfigured_max_attempts: int = Field(default=3, ge=1, le=10)

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    @property
    def version_value(self) -> str:
        if VERSION_FILE.exists():
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value
        return self.version

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_username and self.smtp_password and self.smtp_from_email)


@lru_cache
def get_settings() -> Settings:
    return Settings()
