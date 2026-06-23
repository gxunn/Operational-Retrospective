from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    app_name: str = "日拱一卒 · 自媒体复盘"
    database_url: str = "sqlite:///./data/app.db"
    admin_username: str = "admin"
    admin_password: str = "admin123456"
    session_secret: str = "local-development-change-me"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    app_timezone: str = "Asia/Shanghai"
    report_hour: int = 10
    report_minute: int = 0
    hotspot_hour: int = 9
    hotspot_minute: int = 0
    max_upload_mb: int = 30
    cookie_secure: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = True
    mail_from: str = ""
    mail_from_name: str = "自媒体每日复盘"

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        def flag(name: str, default: bool) -> bool:
            return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}

        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/app.db"),
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("ADMIN_PASSWORD", "admin123456"),
            session_secret=os.getenv("SESSION_SECRET", "local-development-change-me"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
            report_hour=int(os.getenv("REPORT_HOUR", "10")),
            report_minute=int(os.getenv("REPORT_MINUTE", "0")),
            hotspot_hour=int(os.getenv("HOTSPOT_HOUR", "9")),
            hotspot_minute=int(os.getenv("HOTSPOT_MINUTE", "0")),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "30")),
            cookie_secure=flag("COOKIE_SECURE", False),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "465")),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_use_ssl=flag("SMTP_USE_SSL", True),
            mail_from=os.getenv("MAIL_FROM", ""),
            mail_from_name=os.getenv("MAIL_FROM_NAME", "自媒体每日复盘"),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()


settings = get_settings()
