import base64
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppSetting


ROLE_SUPERADMIN = "superadmin"
ROLE_MANAGER = "ops_manager"
ROLE_MEMBER = "member"

ROLE_LABELS = {
    ROLE_SUPERADMIN: "超级管理员",
    ROLE_MANAGER: "运营人员",
    ROLE_MEMBER: "运营人员",
    "admin": "超级管理员",
}

PERMISSIONS = {
    "view": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "manage_accounts": {ROLE_SUPERADMIN, "admin"},
    "manage_members": {ROLE_SUPERADMIN, "admin"},
    "manage_settings": {ROLE_SUPERADMIN, "admin"},
    "manage_recipients": {ROLE_SUPERADMIN, "admin"},
    "send_email": {ROLE_SUPERADMIN, "admin"},
    "manage_hotspots": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "manage_reports": {ROLE_SUPERADMIN, "admin"},
    "import_data": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "use_ai_reports": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "use_report_builder": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "use_topic_center": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "use_breakdown": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "manage_materials": {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER, "admin"},
    "delete_data": {ROLE_SUPERADMIN, "admin"},
}


def normalize_role(role: str) -> str:
    if role == "admin":
        return ROLE_SUPERADMIN
    if role == ROLE_MANAGER:
        return ROLE_MEMBER
    if role in {ROLE_SUPERADMIN, ROLE_MANAGER, ROLE_MEMBER}:
        return ROLE_MEMBER if role == ROLE_MANAGER else role
    return ROLE_MEMBER


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, ROLE_LABELS.get(normalize_role(role), role or "成员"))


def can(user: Any, permission: str) -> bool:
    if not user:
        return False
    return normalize_role(getattr(user, "role", ROLE_MEMBER)) in PERMISSIONS.get(permission, set())


def _secret_key() -> bytes:
    return hashlib.sha256(settings.session_secret.encode("utf-8")).digest()


def encrypt_secret(value: str) -> str:
    data = value.encode("utf-8")
    key = _secret_key()
    masked = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))
    return "enc:" + base64.urlsafe_b64encode(masked).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("enc:"):
        return value
    payload = base64.urlsafe_b64decode(value.removeprefix("enc:").encode("ascii"))
    key = _secret_key()
    plain = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(payload))
    return plain.decode("utf-8")


def get_setting(db: Session, key: str, default: str = "", secret: bool = False) -> str:
    row = db.get(AppSetting, key)
    if not row:
        return default
    value = decrypt_secret(row.value) if secret else row.value
    return value if value != "" else default


def set_setting(db: Session, key: str, value: Any, secret: bool = False) -> None:
    text = "" if value is None else str(value)
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = encrypt_secret(text) if secret and text else text
    db.add(row)


def get_json_setting(db: Session, key: str, default: Any) -> Any:
    raw = get_setting(db, key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def set_json_setting(db: Session, key: str, value: Any, secret: bool = False) -> None:
    set_setting(db, key, json.dumps(value, ensure_ascii=False), secret=secret)


def runtime_settings(db: Session):
    data = settings.model_dump()
    overrides = {
        "openai_api_key": get_setting(db, "openai_api_key", data["openai_api_key"], secret=True),
        "openai_model": get_setting(db, "openai_model", data["openai_model"]),
        "app_timezone": get_setting(db, "app_timezone", data["app_timezone"]),
        "report_hour": int(get_setting(db, "report_hour", str(data["report_hour"]))),
        "report_minute": int(get_setting(db, "report_minute", str(data["report_minute"]))),
        "hotspot_hour": int(get_setting(db, "hotspot_hour", str(data["hotspot_hour"]))),
        "hotspot_minute": int(get_setting(db, "hotspot_minute", str(data["hotspot_minute"]))),
        "smtp_host": get_setting(db, "smtp_host", data["smtp_host"]),
        "smtp_port": int(get_setting(db, "smtp_port", str(data["smtp_port"]))),
        "smtp_username": get_setting(db, "smtp_username", data["smtp_username"]),
        "smtp_password": get_setting(db, "smtp_password", data["smtp_password"], secret=True),
        "smtp_use_ssl": get_setting(db, "smtp_use_ssl", str(data["smtp_use_ssl"])) in {"1", "true", "True", "yes", "on"},
        "mail_from": get_setting(db, "mail_from", data["mail_from"]),
        "mail_from_name": get_setting(db, "mail_from_name", data["mail_from_name"]),
    }
    return settings.model_copy(update=overrides)


def get_tags(text: str) -> list[str]:
    return [part.strip() for part in (text or "").replace("，", ",").split(",") if part.strip()]


def tags_to_text(tags: list[str]) -> str:
    return json.dumps([tag for tag in tags if tag], ensure_ascii=False)
