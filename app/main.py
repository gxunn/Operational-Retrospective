import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import markdown as md
from openai import OpenAI
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .database import Base, SessionLocal, engine
from .models import (
    AiReview,
    AppSetting,
    ContentDailyMetric,
    DailyAccountMetric,
    GeneratedReportDraft,
    EmailRecipient,
    HotspotReport,
    ImportBatch,
    OperationLog,
    PlatformAccount,
    Report,
    MaterialAsset,
    TopicIdea,
    VideoBreakdown,
    SavedView,
    SummaryReport,
    User,
)
from .security import csrf_token, current_user, hash_password, verify_csrf, verify_password
from .services.emailer import send_report_email
from .services.importer import FIELDS, file_sha256, import_batch, preview
from .services.insights import detect_anomalies, explain_forecast, forecast_trend
from .services.hotspots import generate_hotspots, report_payload
from .services.breakdown import (
    clean_text,
    detect_platform_from_url,
    friendly_openai_error,
    fetch_video_info,
    generate_breakdown_analysis,
    normalize_breakdown_url,
)
from .services.metrics import comparison_groups, summarize_metrics
from .services.reporting import LABELS, METRICS, generate_report, generate_summary_report, report_stats, render_docx
from .services.runtime import (
    PERMISSIONS,
    ROLE_MANAGER,
    ROLE_MEMBER,
    ROLE_SUPERADMIN,
    can,
    get_json_setting,
    get_setting,
    get_tags,
    normalize_role,
    role_label,
    runtime_settings,
    set_json_setting,
    set_setting,
)
from .services.v2 import fetchHotTopics, generateAIReport, resolve_range, syncPlatformData
from .services.scheduler import reload_scheduler, start_scheduler, stop_scheduler


APP_VERSION = "2026-06-29-stability-1"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("self_media_review")
if not logger.handlers:
    handler = logging.FileHandler(LOG_DIR / "runtime-errors.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

LAST_RUNTIME_ERROR = {"time": "", "path": "", "message": ""}


def remember_runtime_error(path: str, exc: Exception) -> None:
    message = f"{type(exc).__name__}: {exc}"
    LAST_RUNTIME_ERROR.update({"time": datetime.now().replace(microsecond=0).isoformat(sep=" "), "path": path, "message": message})
    logger.exception("Unhandled error at %s: %s", path, message)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default


def safe_text(value: object, default: str = "") -> str:
    text = "" if value is None else str(value).strip()
    return text or default


def json_error(message: str, status_code: int = 400, **extra) -> JSONResponse:
    payload = {"success": False, "message": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def json_success(**extra) -> JSONResponse:
    payload = {"success": True}
    payload.update(extra)
    return JSONResponse(payload)


def wants_json_response(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return request.url.path.startswith("/api/") or "application/json" in accept


def fallback_html(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>数据加载失败</title>
<body style="margin:0;font-family:'Microsoft YaHei',sans-serif;background:#f5f5f5;color:#1f1f1f;">
<main style="max-width:760px;margin:80px auto;padding:32px;background:#fff;border-radius:20px;box-shadow:0 20px 50px rgba(0,0,0,.06);">
<h1 style="font-size:24px;margin:0 0 12px;">数据加载失败，请稍后重试</h1>
<p style="font-size:14px;line-height:1.7;margin:0 0 16px;">{message}</p>
<a href="/" style="display:inline-block;padding:10px 18px;background:#d92d20;color:#fff;text-decoration:none;border-radius:999px;">返回首页</a>
</main></body></html>""",
        status_code=200,
    )


def latest_import_status(db: Session) -> dict[str, str]:
    try:
        batch = db.scalar(select(ImportBatch).where(ImportBatch.deleted_at.is_(None)).order_by(ImportBatch.created_at.desc()))
    except Exception:
        batch = None
    if not batch:
        return {"file": "暂无记录", "status": "暂无记录", "time": "", "message": ""}
    return {
        "file": safe_text(batch.original_filename, "未命名文件"),
        "status": safe_text(batch.status, "未知"),
        "time": batch.created_at.strftime("%Y-%m-%d %H:%M") if getattr(batch, "created_at", None) else "",
        "message": safe_text(batch.error_message),
    }


def diagnostics_status(db: Session) -> dict[str, str]:
    database = "connected"
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database = "disconnected"
    openai_configured = bool(runtime_settings(db).openai_api_key)
    return {
        "site": "ok",
        "database": database,
        "openai": "configured" if openai_configured else "missing",
        "version": APP_VERSION,
        "last_error": LAST_RUNTIME_ERROR.get("message", ""),
        "last_error_time": LAST_RUNTIME_ERROR.get("time", ""),
        "last_error_path": LAST_RUNTIME_ERROR.get("path", ""),
    }


def initialize() -> None:
    if os.getenv("RAILWAY_PROJECT_ID") and str(settings.database_url).startswith("sqlite"):
        raise RuntimeError("Railway 生产环境禁止使用 SQLite，请将 DATABASE_URL 配置为 Railway Postgres。")
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        dialect = conn.dialect.name

        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                table.create(conn, checkfirst=True)
        tables = set(inspect(conn).get_table_names())

        ddl_by_dialect = {
            "sqlite": {
                "datetime": "DATETIME",
                "integer": "INTEGER",
                "boolean": "BOOLEAN",
                "float": "FLOAT",
                "text": "TEXT",
            },
            "postgresql": {
                "datetime": "TIMESTAMP",
                "integer": "INTEGER",
                "boolean": "BOOLEAN",
                "float": "DOUBLE PRECISION",
                "text": "TEXT",
            },
        }
        ddl_types = ddl_by_dialect.get(dialect, ddl_by_dialect["sqlite"])

        def add_column(table: str, name: str, definition: str) -> None:
            if table not in tables:
                return
            columns = {column["name"] for column in inspect(conn).get_columns(table)}
            if name not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {definition}"))

        add_column("users", "deleted_at", f"deleted_at {ddl_types['datetime']}")
        add_column("users", "deleted_by", f"deleted_by {ddl_types['integer']}")
        add_column("users", "updated_at", f"updated_at {ddl_types['datetime']}")
        add_column("platform_accounts", "last_synced_at", f"last_synced_at {ddl_types['datetime']}")
        add_column("platform_accounts", "deleted_at", f"deleted_at {ddl_types['datetime']}")
        add_column("platform_accounts", "deleted_by", f"deleted_by {ddl_types['integer']}")
        add_column("platform_accounts", "manager_name", "manager_name VARCHAR(80) DEFAULT ''")
        add_column("platform_accounts", "business_type", "business_type VARCHAR(80) DEFAULT ''")
        add_column("platform_accounts", "positioning", "positioning VARCHAR(255) DEFAULT ''")
        add_column("platform_accounts", "data_source", "data_source VARCHAR(30) DEFAULT 'manual'")
        add_column("platform_accounts", "updated_at", f"updated_at {ddl_types['datetime']}")
        add_column("import_batches", "deleted_at", f"deleted_at {ddl_types['datetime']}")
        add_column("import_batches", "deleted_by", f"deleted_by {ddl_types['integer']}")
        add_column("import_batches", "updated_at", f"updated_at {ddl_types['datetime']}")
        add_column("reports", "title", "title VARCHAR(200) DEFAULT ''")
        add_column("reports", "report_type", "report_type VARCHAR(20) DEFAULT 'daily'")
        add_column("reports", "deleted_at", f"deleted_at {ddl_types['datetime']}")
        add_column("reports", "deleted_by", f"deleted_by {ddl_types['integer']}")
        add_column("topic_ideas", "is_favorite", f"is_favorite {ddl_types['boolean']} DEFAULT FALSE")
        add_column("topic_ideas", "updated_at", f"updated_at {ddl_types['datetime']}")
        add_column("topic_ideas", "status", "status VARCHAR(20) DEFAULT '待拍摄'")
        add_column("topic_ideas", "owner_name", "owner_name VARCHAR(80) DEFAULT ''")
        add_column("email_recipients", "tags_json", f"tags_json {ddl_types['text']} DEFAULT '[]'")
        add_column("content_daily_metrics", "private_messages", f"private_messages {ddl_types['float']} DEFAULT 0")
        add_column("content_daily_metrics", "conversion_note", f"conversion_note {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "analysis_markdown", f"analysis_markdown {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "analysis_status", "analysis_status VARCHAR(20) DEFAULT '未开始'")
        add_column("video_breakdowns", "status", "status VARCHAR(20) DEFAULT '未开始'")
        add_column("video_breakdowns", "progress", f"progress {ddl_types['integer']} DEFAULT 0")
        add_column("video_breakdowns", "cover_url", "cover_url VARCHAR(500) DEFAULT ''")
        add_column("video_breakdowns", "author_name", "author_name VARCHAR(120) DEFAULT ''")
        add_column("video_breakdowns", "publish_time", "publish_time VARCHAR(50) DEFAULT ''")
        add_column("video_breakdowns", "collect_count", f"collect_count {ddl_types['float']} DEFAULT 0")
        add_column("video_breakdowns", "share_count", f"share_count {ddl_types['float']} DEFAULT 0")
        add_column("video_breakdowns", "video_text", f"video_text {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "transcript", f"transcript {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "fetch_status", "fetch_status VARCHAR(20) DEFAULT '未抓取'")
        add_column("video_breakdowns", "fetch_error", f"fetch_error {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "error_message", f"error_message {ddl_types['text']} DEFAULT ''")
        add_column("video_breakdowns", "updated_at", f"updated_at {ddl_types['datetime']}")

        breakdown_rows = conn.execute(text("SELECT id, analysis_json, analysis_markdown, status, analysis_status, progress, error_message FROM video_breakdowns")).mappings().all()
        for row in breakdown_rows:
            status = str(row["status"] or "").strip()
            analysis_status = str(row["analysis_status"] or "").strip()
            analysis_markdown = str(row["analysis_markdown"] or "").strip()
            analysis_json = str(row["analysis_json"] or "{}")
            if not status or status == "未开始":
                try:
                    payload = json.loads(analysis_json)
                except Exception:
                    payload = {}
                if analysis_markdown or payload.get("markdown"):
                    conn.execute(
                        text(
                            "UPDATE video_breakdowns SET status = :status, analysis_status = :analysis_status, progress = :progress, analysis_markdown = :markdown, updated_at = :updated_at WHERE id = :id"
                        ),
                        {
                            "status": "已完成",
                            "analysis_status": "已完成",
                            "progress": 100,
                            "markdown": analysis_markdown or str(payload.get("markdown", "")),
                            "updated_at": datetime.now().replace(microsecond=0),
                            "id": row["id"],
                        },
                    )
            elif not analysis_status:
                conn.execute(
                    text("UPDATE video_breakdowns SET analysis_status = :analysis_status WHERE id = :id"),
                    {"analysis_status": status, "id": row["id"]},
                )
    storage_dir = settings.storage_dir
    for folder in (storage_dir, storage_dir / "uploads", storage_dir / "reports"):
        folder.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        total_users = db.scalar(select(func.count()).select_from(User)) or 0
        if total_users == 0:
            db.add(
                User(
                    username=settings.admin_username,
                    display_name="管理员",
                    password_hash=hash_password(settings.admin_password),
                    role=ROLE_SUPERADMIN,
                )
            )
            db.commit()
        db.query(User).filter(User.role == "admin").update({User.role: ROLE_SUPERADMIN})
        db.query(User).filter(User.role == ROLE_MANAGER).update({User.role: ROLE_MEMBER})
        db.query(User).filter(User.display_name == "").update({User.display_name: User.username})
        db.query(PlatformAccount).filter(PlatformAccount.deleted_at.is_not(None)).update({PlatformAccount.is_active: False})
        db.query(TopicIdea).filter(TopicIdea.status == "待写").update({TopicIdea.status: "待拍摄"})
        db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.cookie_secure,
    same_site="lax",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals.update(app_name=settings.app_name, metric_labels=LABELS)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    remember_runtime_error(request.url.path, exc)
    if wants_json_response(request):
        return json_error("提交的数据格式不正确，请检查后重试。", status_code=422, details=exc.errors())
    return redirect(str(request.url.path), "提交的数据格式不正确，请检查后重试。")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if wants_json_response(request):
        return json_error(str(exc.detail or "请求失败"), status_code=exc.status_code)
    if exc.status_code >= 500:
        remember_runtime_error(request.url.path, exc)
        return fallback_html("页面暂时不可用，请稍后刷新再试。")
    return fallback_html(str(exc.detail or "请求失败"))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    remember_runtime_error(request.url.path, exc)
    if wants_json_response(request):
        return json_error("服务暂时不可用，请稍后重试。", status_code=500)
    return fallback_html("页面暂时不可用，系统已记录这次错误。")


def redirect(url: str, message: str = "") -> RedirectResponse:
    if message:
        url += ("&" if "?" in url else "?") + "message=" + quote(message)
    return RedirectResponse(url, status_code=303)


def page(request: Request, template: str, db: Session, **context):
    user = current_user(request, db)
    if not user:
        return redirect("/auth/login")
    runtime = runtime_settings(db)
    return templates.TemplateResponse(
        request,
        template,
        {
            "user": user,
            "user_role_label": role_label(user.role),
            "permissions": {name: can(user, name) for name in PERMISSIONS},
            "settings": runtime,
            "csrf_token": csrf_token(request),
            "message": request.query_params.get("message", ""),
            **context,
        },
    )


def current_actor(request: Request, db: Session) -> User | None:
    return current_user(request, db)


def log_operation(
    db: Session,
    actor: User | None,
    operation_type: str,
    object_type: str,
    object_name: str,
    detail: str = "",
) -> None:
    db.add(
        OperationLog(
            operator_id=actor.id if actor else None,
            operator_name=(actor.display_name or actor.username) if actor else "系统",
            operation_type=operation_type,
            object_type=object_type,
            object_name=object_name,
            detail=detail,
        )
    )


def require_permission(request: Request, db: Session, permission: str, message: str = "没有权限") -> User | None:
    user = current_actor(request, db)
    if not user or not can(user, permission):
        return None
    return user


def parse_iso_date(value: str, fallback: date | None = None) -> date | None:
    try:
        return date.fromisoformat(value) if value else fallback
    except ValueError:
        return fallback


def account_platform_choices(db: Session) -> list[str]:
    fixed = ["抖音", "小红书", "视频号", "公众号", "其他"]
    existing = sorted(
        {
            item.platform
            for item in db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None))).all()
            if item.platform not in fixed
        }
    )
    return fixed + existing


def safe_json_loads(value: str, default):
    try:
        return json.loads(value)
    except Exception:
        return default


def is_valid_http_url(value: str) -> bool:
    value = value.strip()
    if not value:
        return True
    try:
        if "://" not in value and "." in value:
            value = f"https://{value}"
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def suggest_material_tags(*parts: str) -> list[str]:
    text = " ".join(part for part in parts if part)
    candidates = ["张家界", "无人机足球", "研学", "招生", "证书", "端午", "活动", "海报", "合同", "课程", "案例"]
    tags = [item for item in candidates if item in text]
    if not tags and text:
        tags = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()][:3]
    return list(dict.fromkeys(tags))[:8]


def breakdown_display_status(value: str, progress: int) -> tuple[str, int]:
    status = (value or "未开始").strip() or "未开始"
    safe_progress = max(0, min(100, int(progress or 0)))
    if status == "已完成":
        safe_progress = 100
    if status == "未开始" and safe_progress > 0:
        safe_progress = 0
    return status, safe_progress


def breakdown_report_data(case: VideoBreakdown | None) -> dict:
    if not case:
        return {}
    data = safe_json_loads(case.analysis_json, {})
    data["source_url"] = case.source_url or ""
    data["platform"] = case.platform or "其他"
    data["title"] = case.title or ""
    data["cover_url"] = case.cover_url or ""
    data["cover_description"] = case.cover_description or ""
    data["author_name"] = case.author_name or ""
    data["publish_time"] = case.publish_time or ""
    data["duration"] = case.duration or ""
    data["play_count"] = case.views or 0
    data["like_count"] = case.likes or 0
    data["comment_count"] = case.comments or 0
    data["collect_count"] = case.collect_count or 0
    data["share_count"] = case.share_count or 0
    data["video_text"] = case.video_text or ""
    data["transcript"] = case.transcript or ""
    data["fetch_status"] = case.fetch_status or "未抓取"
    data["fetch_error"] = case.fetch_error or ""
    data["analysis_status"] = case.analysis_status or case.status or "未开始"
    data["analysis_markdown"] = case.analysis_markdown or ""
    data["markdown_html"] = md.markdown(case.analysis_markdown or "", extensions=["tables", "fenced_code"])
    data["status"] = case.status or "未开始"
    data["progress"] = int(case.progress or 0)
    data["error_message"] = case.error_message or ""
    return data



@app.api_route("/health", methods=["GET", "HEAD"])
@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    try:
        with SessionLocal() as db:
            runtime = runtime_settings(db)
            database = "connected"
            try:
                db.execute(text("SELECT 1"))
            except Exception:
                database = "disconnected"
            payload = {
                "status": "ok",
                "time": datetime.now().replace(microsecond=0).isoformat(sep=" "),
                "database": database,
                "openai": "configured" if runtime.openai_api_key else "missing",
                "version": APP_VERSION,
            }
            return payload
    except Exception as exc:
        remember_runtime_error("/api/health", exc)
        return JSONResponse(
            {
                "status": "error",
                "time": datetime.now().replace(microsecond=0).isoformat(sep=" "),
                "database": "disconnected",
                "openai": "missing",
                "version": APP_VERSION,
            },
            status_code=200,
        )


@app.post("/assistant/ask")
async def ask_assistant(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return json_error("请先登录", status_code=401)
        runtime = runtime_settings(db)
        if not runtime.openai_api_key:
            return json_success(answer="系统还没有配置 OpenAI API Key，暂时无法使用 AI 助手。")
        payload = await request.json()
        question = str(payload.get("question", "")).strip()
        if not question:
            return json_error("请输入你想问的问题。", status_code=400)
        latest = db.scalar(select(func.max(DailyAccountMetric.metric_date)))
        stats = report_stats(db, latest or (date.today() - timedelta(days=1)))
        anomalies = detect_anomalies(db, latest or (date.today() - timedelta(days=1)))[:5]
        prompt = (
            "你是自媒体运营分析助手。请基于以下统计信息，用简洁中文回答用户问题，"
            "给出原因判断和下一步建议，不要编造未提供的数据。\n"
            f"统计信息：{json.dumps(stats, ensure_ascii=False)}\n"
            f"异常提示：{json.dumps(anomalies, ensure_ascii=False)}\n"
            f"用户问题：{question}"
        )
        try:
            client = OpenAI(api_key=runtime.openai_api_key, timeout=45)
            response = client.responses.create(model=runtime.openai_model, input=prompt)
            return json_success(answer=response.output_text.strip() or "暂时没有可返回的内容。")
        except Exception as exc:
            remember_runtime_error("/assistant/ask", exc)
            return json_error(f"AI 助手暂时不可用：{type(exc).__name__}", status_code=500)


@app.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request):
    with SessionLocal() as db:
        if current_user(request, db):
            return redirect("/")
    return templates.TemplateResponse(request, "login.html", {"csrf_token": csrf_token(request), "error": ""})


@app.post("/auth/login")
def login(request: Request, username: str = Form(), password: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username.strip()))
        if not user or not user.is_active or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"csrf_token": csrf_token(request), "error": "账号或密码不正确"},
                status_code=400,
            )
        request.session.clear()
        request.session["user_id"] = user.id
        csrf_token(request)
    return redirect("/")


@app.post("/auth/logout")
def logout(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    request.session.clear()
    return redirect("/auth/login")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return redirect("/auth/login")
        runtime = runtime_settings(db)
        message = request.query_params.get("message", "")
        try:
            latest = db.scalar(select(func.max(DailyAccountMetric.metric_date)))
            target = latest or (date.today() - timedelta(days=1))
            stats = report_stats(db, target)
            days = [target - timedelta(days=offset) for offset in range(6, -1, -1)]
            trends = [{"date": day.strftime("%m/%d"), "views": safe_float(report_stats(db, day)["current"]["views"])} for day in days]
            max_trend = max((item["views"] for item in trends), default=0) or 1
            for index, item in enumerate(trends):
                item["x"] = round(7 + index * 86 / 6, 2)
                item["y"] = round(82 - item["views"] / max_trend * 58, 2)
            accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.is_active.is_(True))).all()
            account_rows = []
            for account in accounts:
                metric = db.scalar(
                    select(DailyAccountMetric).where(
                        DailyAccountMetric.account_id == account.id,
                        DailyAccountMetric.metric_date == target,
                    )
                )
                previous_metric = db.scalar(
                    select(DailyAccountMetric).where(
                        DailyAccountMetric.account_id == account.id,
                        DailyAccountMetric.metric_date == target - timedelta(days=1),
                    )
                )
                interactions = safe_float(getattr(metric, "likes", 0)) + safe_float(getattr(metric, "comments", 0)) + safe_float(getattr(metric, "favorites", 0)) + safe_float(getattr(metric, "shares", 0))
                previous_interactions = safe_float(getattr(previous_metric, "likes", 0)) + safe_float(getattr(previous_metric, "comments", 0)) + safe_float(getattr(previous_metric, "favorites", 0)) + safe_float(getattr(previous_metric, "shares", 0))
                metric_views = safe_float(getattr(metric, "views", 0))
                previous_views = safe_float(getattr(previous_metric, "views", 0))
                rate = interactions / metric_views if metric_views else 0
                previous_rate = previous_interactions / previous_views if previous_views else 0
                prev_followers = safe_float(getattr(previous_metric, "followers_new", 0))
                cur_followers = safe_float(getattr(metric, "followers_new", 0))
                follower_change = ((cur_followers - prev_followers) / prev_followers * 100) if prev_followers else None
                account_rows.append({
                    "account": account,
                    "metric": metric,
                    "rate": rate,
                    "rate_change": (rate - previous_rate) * 100,
                    "follower_change": follower_change,
                })
            contents = db.scalars(
                select(ContentDailyMetric).where(ContentDailyMetric.metric_date == target).order_by(ContentDailyMetric.views.desc()).limit(8)
            ).all()
            history_rows = db.scalars(
                select(DailyAccountMetric).where(
                    DailyAccountMetric.metric_date >= target - timedelta(days=14),
                    DailyAccountMetric.metric_date <= target,
                )
            ).all()
            views_series = [(row.metric_date, safe_float(row.views)) for row in history_rows if getattr(row, "metric_date", None)]
            followers_series = [(row.metric_date, safe_float(row.followers_new)) for row in history_rows if getattr(row, "metric_date", None)]
            anomalies = detect_anomalies(db, target)
        except Exception as exc:
            remember_runtime_error("/", exc)
            target = date.today() - timedelta(days=1)
            stats = {"current": {field: 0 for field in METRICS}, "previous": {field: 0 for field in METRICS}}
            trends = [{"date": (target - timedelta(days=offset)).strftime("%m/%d"), "views": 0, "x": round(7 + index * 86 / 6, 2), "y": 82} for index, offset in enumerate(range(6, -1, -1))]
            account_rows = []
            contents = []
            views_series = []
            followers_series = []
            anomalies = []
            message = message or "数据加载失败，请稍后重试"
        return page(
            request,
            "dashboard.html",
            db,
            target=target,
            stats=stats,
            trends=trends,
            account_rows=account_rows,
            contents=contents,
            report_hour=f"{runtime.report_hour:02d}:{runtime.report_minute:02d}",
            target_weekday="星期" + "一二三四五六日"[target.weekday()],
            anomalies=anomalies,
            forecast_views=forecast_trend(views_series),
            forecast_followers=forecast_trend(followers_series),
            message=message,
        )


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, q: str = "", platform: str = ""):
    with SessionLocal() as db:
        try:
            query = select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None))
            if q:
                needle = q.strip()
                query = query.where(
                    or_(
                        PlatformAccount.name.contains(needle),
                        PlatformAccount.external_id.contains(needle),
                        PlatformAccount.manager_name.contains(needle),
                        PlatformAccount.business_type.contains(needle),
                        PlatformAccount.positioning.contains(needle),
                    )
                )
            if platform:
                query = query.where(PlatformAccount.platform == platform)
            accounts = db.scalars(query.order_by(PlatformAccount.platform, PlatformAccount.name)).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/accounts", exc)
            accounts = []
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "accounts.html",
            db,
            accounts=accounts,
            platforms=account_platform_choices(db),
            search=q,
            selected_platform=platform,
            can_manage_accounts=can(current_actor(request, db), "manage_accounts"),
            message=message,
        )


@app.post("/accounts")
def add_account(
    request: Request,
    platform: str = Form(),
    name: str = Form(),
    external_id: str = Form(""),
    manager_name: str = Form(""),
    business_type: str = Form(""),
    positioning: str = Form(""),
    data_source: str = Form("manual"),
    is_active: str = Form("on"),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_accounts"):
            return redirect("/accounts", "只有超级管理员可以新增账号")
        if not platform.strip() or not name.strip():
            return redirect("/accounts", "平台和账号名称不能为空")
        if db.scalar(
            select(PlatformAccount).where(
                PlatformAccount.platform == platform,
                PlatformAccount.name == name.strip(),
                PlatformAccount.deleted_at.is_(None),
            )
        ):
            return redirect("/accounts", "这个平台账号已经存在")
        db.add(
            PlatformAccount(
                platform=platform,
                name=name.strip(),
                external_id=external_id.strip(),
                manager_name=manager_name.strip(),
                business_type=business_type.strip(),
                positioning=positioning.strip(),
                data_source=data_source.strip() or "manual",
                is_active=is_active == "on",
            )
        )
        actor = current_actor(request, db)
        log_operation(db, actor, "新增", "账号", f"{platform} · {name.strip()}", f"账号ID：{external_id.strip()}；负责人：{manager_name.strip()}")
        db.commit()
    return redirect("/accounts", "账号已添加")


@app.post("/accounts/{account_id}/edit")
def edit_account(
    account_id: int,
    request: Request,
    platform: str = Form(),
    name: str = Form(),
    external_id: str = Form(""),
    manager_name: str = Form(""),
    business_type: str = Form(""),
    positioning: str = Form(""),
    data_source: str = Form("manual"),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_accounts"):
            return redirect("/accounts", "没有权限")
        account = db.get(PlatformAccount, account_id)
        if not account or account.deleted_at:
            return redirect("/accounts", "账号不存在")
        if not platform.strip() or not name.strip():
            return redirect("/accounts", "平台和账号名称不能为空")
        duplicate = db.scalar(
            select(PlatformAccount).where(
                PlatformAccount.id != account_id,
                PlatformAccount.platform == platform,
                PlatformAccount.name == name.strip(),
                PlatformAccount.deleted_at.is_(None),
            )
        )
        if duplicate:
            return redirect("/accounts", "这个平台账号已经存在")
        account.platform = platform
        account.name = name.strip()
        account.external_id = external_id.strip()
        account.manager_name = manager_name.strip()
        account.business_type = business_type.strip()
        account.positioning = positioning.strip()
        account.data_source = data_source.strip() or "manual"
        account.last_synced_at = datetime.now().replace(microsecond=0)
        actor = current_actor(request, db)
        log_operation(db, actor, "编辑", "账号", f"{platform} · {name.strip()}", f"账号ID：{external_id.strip()}；负责人：{manager_name.strip()}")
        db.commit()
    return redirect("/accounts", "账号已更新")


@app.post("/accounts/{account_id}/toggle")
def toggle_account(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_accounts"):
            return redirect("/accounts", "只有超级管理员可以修改账号")
        account = db.get(PlatformAccount, account_id)
        if account and not account.deleted_at:
            account.is_active = not account.is_active
            actor = current_actor(request, db)
            log_operation(db, actor, "启用" if account.is_active else "停用", "账号", f"{account.platform} · {account.name}", f"账号ID：{account.external_id}")
            db.commit()
    return redirect("/accounts", "账号状态已更新")


@app.post("/accounts/{account_id}/delete")
def delete_account(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/accounts", "没有权限")
        account = db.get(PlatformAccount, account_id)
        if account and not account.deleted_at:
            account.deleted_at = datetime.now().replace(microsecond=0)
            account.deleted_by = current_actor(request, db).id if current_actor(request, db) else None
            account.is_active = False
            log_operation(db, current_actor(request, db), "删除", "账号", f"{account.platform} · {account.name}", f"账号ID：{account.external_id}")
            db.commit()
    return redirect("/accounts", "账号已删除")


@app.post("/accounts/bulk")
def bulk_accounts(request: Request, action: str = Form(), account_ids: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/accounts", "没有权限")
        ids = [int(item) for item in account_ids.split(",") if item.strip().isdigit()]
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.id.in_(ids), PlatformAccount.deleted_at.is_(None))).all()
        actor = current_actor(request, db)
        for account in accounts:
            if action == "enable":
                account.is_active = True
                log_operation(db, actor, "启用", "账号", f"{account.platform} · {account.name}", "批量启用")
            elif action == "disable":
                account.is_active = False
                log_operation(db, actor, "停用", "账号", f"{account.platform} · {account.name}", "批量停用")
            elif action == "delete":
                account.deleted_at = datetime.now().replace(microsecond=0)
                account.deleted_by = current_actor(request, db).id if current_actor(request, db) else None
                account.is_active = False
                log_operation(db, actor, "删除", "账号", f"{account.platform} · {account.name}", "批量删除")
        db.commit()
    return redirect("/accounts", "批量操作已完成")


@app.post("/accounts/{account_id}/sync")
def sync_account_data(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "use_ai_reports"):
            return redirect("/accounts", "没有权限")
        account = db.get(PlatformAccount, account_id)
        if not account or account.deleted_at:
            return redirect("/accounts", "账号不存在")
        account.last_synced_at = datetime.now().replace(microsecond=0)
        log_operation(db, current_actor(request, db), "上传", "账号同步", f"{account.platform} · {account.name}", "手动同步账号数据")
        db.commit()
        return redirect("/accounts", syncPlatformData(account))


@app.post("/accounts/import")
async def import_accounts(request: Request, file: UploadFile = File(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_accounts"):
            return redirect("/accounts", "没有权限")
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".csv", ".xlsx", ".xls"}:
            return redirect("/accounts", "请上传 CSV、XLSX 或 XLS 模板")
        stored = settings.storage_dir / "uploads" / f"accounts-{uuid.uuid4().hex}{suffix}"
        with stored.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        try:
            frame = preview(stored, "账号导入", db)[0]
        except Exception:
            frame = []
        try:
            import pandas as pd
            table = pd.read_excel(stored) if suffix in {".xlsx", ".xls"} else pd.read_csv(stored, encoding="utf-8-sig")
            imported = 0
            for _, row in table.iterrows():
                platform = str(row.get("平台", row.get("platform", ""))).strip()
                name = str(row.get("账号名称", row.get("name", ""))).strip()
                external_id = str(row.get("账号ID", row.get("ID", row.get("external_id", "")))).strip()
                manager_name = str(row.get("负责人", row.get("manager_name", ""))).strip()
                business_type = str(row.get("业务类型", row.get("business_type", row.get("positioning", "")))).strip()
                positioning = str(row.get("账号定位", row.get("positioning", ""))).strip()
                data_source = str(row.get("数据获取方式", row.get("data_source", "manual"))).strip() or "manual"
                if not platform or not name:
                    continue
                exists = db.scalar(
                    select(PlatformAccount).where(
                        PlatformAccount.platform == platform,
                        PlatformAccount.name == name,
                        PlatformAccount.deleted_at.is_(None),
                    )
                )
                if exists:
                    continue
                db.add(
                    PlatformAccount(
                        platform=platform,
                        name=name,
                        external_id=external_id,
                        manager_name=manager_name,
                        business_type=business_type,
                        positioning=positioning,
                        data_source=data_source,
                    )
                )
                imported += 1
            db.commit()
            log_operation(db, current_actor(request, db), "导入", "账号", Path(file.filename or "data").name, f"新增 {imported} 个账号")
            return redirect("/accounts", f"批量导入完成：新增 {imported} 个账号")
        except Exception as exc:
            db.rollback()
            return redirect("/accounts", f"批量导入失败：{exc}")


@app.get("/accounts/trash", response_class=HTMLResponse)
def accounts_trash_page(request: Request):
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/accounts", "没有权限")
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_not(None)).order_by(PlatformAccount.deleted_at.desc())).all()
        return page(
            request,
            "accounts.html",
            db,
            accounts=accounts,
            platforms=[],
            search="",
            selected_platform="",
            in_trash=True,
            can_manage_accounts=can(current_actor(request, db), "delete_data"),
        )


@app.post("/accounts/{account_id}/restore")
def restore_account(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/accounts/trash", "没有权限")
        account = db.get(PlatformAccount, account_id)
        if account and account.deleted_at:
            account.deleted_at = None
            account.deleted_by = None
            account.is_active = True
            db.commit()
    return redirect("/accounts/trash", "账号已恢复")


@app.post("/accounts/{account_id}/purge")
def purge_account(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/accounts/trash", "没有权限")
        account = db.get(PlatformAccount, account_id)
        if account:
            db.delete(account)
            db.commit()
    return redirect("/accounts/trash", "账号已彻底删除")


@app.get("/imports/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    with SessionLocal() as db:
        try:
            accounts = db.scalars(
                select(PlatformAccount).where(PlatformAccount.is_active.is_(True), PlatformAccount.deleted_at.is_(None)).order_by(PlatformAccount.platform)
            ).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/imports/upload", exc)
            accounts = []
            message = "数据加载失败，请稍后重试"
        return page(request, "upload.html", db, accounts=accounts, max_mb=runtime_settings(db).max_upload_mb, message=message)


@app.post("/imports/upload")
async def upload_file(request: Request, account_id: int = Form(), file: UploadFile = File(), csrf: str = Form()):
    verify_csrf(request, csrf)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        return redirect("/imports/upload", "文件格式不支持，请上传 CSV、XLSX 或 XLS")
    with SessionLocal() as db:
        user = current_user(request, db)
        account = db.get(PlatformAccount, account_id)
        if not user or not account or account.deleted_at:
            return redirect("/imports/upload", "请选择有效的平台账号")
        if not can(user, "import_data"):
            return redirect("/imports/upload", "你没有上传数据的权限")
        stored = settings.storage_dir / "uploads" / f"{uuid.uuid4().hex}{suffix}"
        with stored.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        runtime = runtime_settings(db)
        if stored.stat().st_size == 0:
            stored.unlink(missing_ok=True)
            return redirect("/imports/upload", "上传文件为空，请重新选择")
        if stored.stat().st_size > runtime.max_upload_mb * 1024 * 1024:
            stored.unlink(missing_ok=True)
            return redirect("/imports/upload", f"文件不能超过 {runtime.max_upload_mb}MB")
        batch = ImportBatch(
            account_id=account.id,
            uploaded_by=user.id,
            original_filename=Path(file.filename or "data").name,
            stored_path=str(stored),
            file_hash=file_sha256(stored),
        )
        db.add(batch)
        db.flush()
        log_operation(db, user, "上传", "上传记录", batch.original_filename, f"{account.platform} · {account.name}")
        try:
            columns, rows, mapping = preview(stored, account.platform, db)
            batch.mapping_json = json.dumps({"columns": columns, "rows": rows, "suggested": mapping}, ensure_ascii=False)
            db.commit()
        except Exception as exc:
            remember_runtime_error("/imports/upload", exc)
            batch.status = "failed"
            batch.error_message = str(exc)
            db.commit()
            return redirect("/imports", f"预览失败：{exc}")
        return redirect(f"/imports/{batch.id}/preview")


@app.post("/imports/upload-multi")
async def upload_multiple_files(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user or not can(user, "import_data"):
            return json_error("没有权限", status_code=403)
        form = await request.form()
        verify_csrf(request, str(form.get("csrf", "")))
        account_id = int(str(form.get("account_id", "0")) or "0")
        account = db.get(PlatformAccount, account_id)
        if not account or account.deleted_at:
            return json_error("请选择有效的平台账号", status_code=400)
        files = form.getlist("files")
        runtime = runtime_settings(db)
        results = []
        for file in files:
            suffix = Path(file.filename or "").suffix.lower()
            if suffix not in {".csv", ".xlsx", ".xls"}:
                results.append({"name": file.filename, "status": "failed", "message": "格式不支持"})
                continue
            stored = settings.storage_dir / "uploads" / f"{uuid.uuid4().hex}{suffix}"
            with stored.open("wb") as output:
                shutil.copyfileobj(file.file, output)
            if stored.stat().st_size == 0:
                stored.unlink(missing_ok=True)
                results.append({"name": file.filename, "status": "failed", "message": "文件为空"})
                continue
            if stored.stat().st_size > runtime.max_upload_mb * 1024 * 1024:
                stored.unlink(missing_ok=True)
                results.append({"name": file.filename, "status": "failed", "message": f"超过 {runtime.max_upload_mb}MB"})
                continue
            batch = ImportBatch(
                account_id=account.id,
                uploaded_by=user.id,
                original_filename=Path(file.filename or "data").name,
                stored_path=str(stored),
                file_hash=file_sha256(stored),
            )
            db.add(batch)
            db.flush()
            log_operation(db, user, "上传", "上传记录", batch.original_filename, f"{account.platform} · {account.name}")
            try:
                columns, rows, mapping = preview(stored, account.platform, db)
                batch.mapping_json = json.dumps({"columns": columns, "rows": rows, "suggested": mapping}, ensure_ascii=False)
                results.append({"name": file.filename, "status": "preview", "batch_id": batch.id})
            except Exception as exc:
                remember_runtime_error("/imports/upload-multi", exc)
                batch.status = "failed"
                batch.error_message = str(exc)
                results.append({"name": file.filename, "status": "failed", "message": str(exc)})
        db.commit()
        return json_success(results=results, message="批量上传已完成")


@app.get("/imports/{batch_id}/preview", response_class=HTMLResponse)
def import_preview_page(batch_id: int, request: Request):
    with SessionLocal() as db:
        batch = db.get(ImportBatch, batch_id)
        if not batch or batch.status != "preview":
            return redirect("/imports", "这条记录不能再预览")
        try:
            columns, rows, mapping = preview(Path(batch.stored_path), batch.account.platform, db)
            data = {"columns": columns, "rows": rows, "suggested": mapping}
            batch.mapping_json = json.dumps(data, ensure_ascii=False)
            db.commit()
        except Exception as exc:
            return redirect("/imports", f"重新识别失败：{exc}")
        return page(request, "import_preview.html", db, batch=batch, fields=FIELDS, **data)


@app.post("/imports/{batch_id}/commit")
async def commit_import(batch_id: int, request: Request):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf", "")))
    mapping = {field: str(form.get(field, "")) for field in FIELDS}
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        batch = db.get(ImportBatch, batch_id)
        if not batch or batch.status != "preview":
            return redirect("/imports", "导入记录状态不正确")
        try:
            count = import_batch(db, batch, mapping)
            log_operation(db, current_actor(request, db), "导入", "上传记录", batch.original_filename, f"导入 {count} 行")
            db.commit()
            return redirect("/imports", f"成功导入 {count} 行数据")
        except Exception as exc:
            remember_runtime_error(f"/imports/{batch_id}/commit", exc)
            db.rollback()
            batch = db.get(ImportBatch, batch_id)
            batch.status = "failed"
            batch.error_message = str(exc)
            db.commit()
            return redirect("/imports", f"导入失败：{exc}")


@app.get("/imports", response_class=HTMLResponse)
def imports_page(
    request: Request,
    status: str = "",
    platform: str = "",
    account_id: int = 0,
    start_date: str = "",
    end_date: str = "",
):
    with SessionLocal() as db:
        try:
            query = select(ImportBatch).where(ImportBatch.deleted_at.is_(None))
            if status:
                query = query.where(ImportBatch.status == status)
            if platform:
                query = query.join(PlatformAccount).where(PlatformAccount.platform == platform)
            if account_id:
                query = query.where(ImportBatch.account_id == account_id)
            if start_date:
                try:
                    query = query.where(ImportBatch.created_at >= datetime.combine(date.fromisoformat(start_date), datetime.min.time()))
                except ValueError:
                    pass
            if end_date:
                try:
                    query = query.where(ImportBatch.created_at <= datetime.combine(date.fromisoformat(end_date), datetime.max.time()))
                except ValueError:
                    pass
            batches = db.scalars(query.order_by(ImportBatch.created_at.desc()).limit(100)).all()
            accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None)).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/imports", exc)
            batches = []
            accounts = []
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "imports.html",
            db,
            batches=batches,
            status=status,
            platforms=sorted({account.platform for account in accounts}),
            selected_platform=platform,
            selected_account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            accounts=accounts,
            message=message,
        )


@app.post("/imports/{batch_id}/retry")
def retry_import(batch_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "import_data"):
            return redirect("/imports", "没有权限")
        batch = db.get(ImportBatch, batch_id)
        if not batch or not batch.stored_path or not Path(batch.stored_path).exists():
            return redirect("/imports", "原始文件不可用，无法重新上传")
        suffix = Path(batch.stored_path).suffix.lower() or ".xlsx"
        stored = settings.storage_dir / "uploads" / f"retry-{uuid.uuid4().hex}{suffix}"
        shutil.copy2(batch.stored_path, stored)
        new_batch = ImportBatch(
            account_id=batch.account_id,
            uploaded_by=actor.id,
            original_filename=batch.original_filename,
            stored_path=str(stored),
            file_hash=file_sha256(stored),
            status="preview",
        )
        db.add(new_batch)
        db.flush()
        try:
            columns, rows, mapping = preview(stored, batch.account.platform, db)
            new_batch.mapping_json = json.dumps({"columns": columns, "rows": rows, "suggested": mapping}, ensure_ascii=False)
            log_operation(db, actor, "上传", "上传记录", new_batch.original_filename, f"重新上传至 {batch.account.platform} · {batch.account.name}")
            db.commit()
            return redirect(f"/imports/{new_batch.id}/preview", "已重新创建上传记录")
        except Exception as exc:
            db.rollback()
            return redirect("/imports", f"重新上传失败：{exc}")


@app.post("/imports/{batch_id}/delete")
def delete_import(batch_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "delete_data"):
            return redirect("/imports", "没有权限")
        batch = db.get(ImportBatch, batch_id)
        if batch and batch.deleted_at is None:
            batch.deleted_at = datetime.now().replace(microsecond=0)
            batch.deleted_by = current_actor(request, db).id if current_actor(request, db) else None
            log_operation(db, current_actor(request, db), "删除", "上传记录", batch.original_filename, f"状态：{batch.status}")
            db.commit()
        if batch and batch.stored_path and Path(batch.stored_path).exists():
            Path(batch.stored_path).unlink()
    return redirect("/imports", "导入记录已删除")


@app.post("/imports/failed/clear")
def clear_failed_imports(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "delete_data"):
            return redirect("/imports", "没有权限")
        failed_batches = db.scalars(select(ImportBatch).where(ImportBatch.status == "failed", ImportBatch.deleted_at.is_(None))).all()
        for batch in failed_batches:
            batch.deleted_at = datetime.now().replace(microsecond=0)
            log_operation(db, current_actor(request, db), "删除", "上传记录", batch.original_filename, "清理失败记录")
            if batch.stored_path and Path(batch.stored_path).exists():
                Path(batch.stored_path).unlink()
        db.commit()
    return redirect("/imports", "失败记录已清理")


@app.post("/imports/{batch_id}/rollback")
def rollback_import(batch_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "delete_data"):
            return redirect("/imports", "没有权限")
        batch = db.get(ImportBatch, batch_id)
        if not batch or not batch.imported_at:
            return redirect("/imports", "这条记录还没有成功导入")
        if datetime.now().replace(microsecond=0) - batch.imported_at > timedelta(hours=12):
            return redirect("/imports", "只能在 12 小时内回滚最近导入")
        db.query(DailyAccountMetric).filter(DailyAccountMetric.source_batch_id == batch.id).delete(synchronize_session=False)
        db.query(ContentDailyMetric).filter(ContentDailyMetric.source_batch_id == batch.id).delete(synchronize_session=False)
        batch.status = "rolled_back"
        batch.deleted_at = None
        log_operation(db, current_actor(request, db), "编辑", "上传记录", batch.original_filename, "回滚最近导入")
        db.commit()
    return redirect("/imports", "最近一次导入已回滚")


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    comparison_day: str = "",
    day: str = "",
    platform: list[str] = Query(default=[]),
    account_id: list[int] = Query(default=[]),
):
    with SessionLocal() as db:
        latest = db.scalar(select(func.max(DailyAccountMetric.metric_date)))
        default_end = latest or date.today() - timedelta(days=1)

        def parsed(value: str, fallback: date) -> date:
            try:
                return date.fromisoformat(value) if value else fallback
            except ValueError:
                return fallback

        if day and not start_date and not end_date:
            start_date = end_date = day
        range_end = parsed(end_date, default_end)
        range_start = parsed(start_date, range_end - timedelta(days=6))
        if range_start > range_end:
            range_start, range_end = range_end, range_start

        try:
            accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
            platforms = sorted({account.platform for account in accounts if safe_text(getattr(account, "platform", ""))})
            selected_platforms = set(platform)
            selected_account_ids = set(account_id)

            query = (
                select(DailyAccountMetric)
                .join(PlatformAccount)
                .where(
                    DailyAccountMetric.metric_date >= range_start,
                    DailyAccountMetric.metric_date <= range_end,
                )
            )
            if selected_platforms:
                query = query.where(PlatformAccount.platform.in_(selected_platforms))
            if selected_account_ids:
                query = query.where(DailyAccountMetric.account_id.in_(selected_account_ids))
            rows = db.scalars(
                query.order_by(DailyAccountMetric.metric_date.desc(), DailyAccountMetric.views.desc())
            ).all()
            compare_target = parsed(comparison_day, range_end)
            if compare_target < range_start or compare_target > range_end:
                compare_target = range_end
            compare_rows = [row for row in rows if row.metric_date == compare_target]
            saved_views = db.scalars(select(SavedView).where(SavedView.scope == "metrics").order_by(SavedView.is_default.desc(), SavedView.updated_at.desc())).all()
            saved_view_links = []
            for view in saved_views:
                try:
                    params = json.loads(view.params_json or "{}")
                except json.JSONDecodeError:
                    params = {}
                query_parts: list[tuple[str, str]] = []
                for key in ("start_date", "end_date", "comparison_day"):
                    value = params.get(key)
                    if value:
                        query_parts.append((key, str(value)))
                for item in params.get("platform", []) or []:
                    if item:
                        query_parts.append(("platform", str(item)))
                for item in params.get("account_ids", []) or []:
                    query_parts.append(("account_id", str(item)))
                saved_view_links.append(
                    {
                        "id": view.id,
                        "name": view.name,
                        "updated_at": view.updated_at,
                        "href": "/metrics" + (f"?{urlencode(query_parts)}" if query_parts else ""),
                    }
                )
            anomalies = detect_anomalies(db, compare_target)
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/metrics", exc)
            accounts = []
            platforms = []
            selected_platforms = set(platform)
            selected_account_ids = set(account_id)
            rows = []
            compare_target = range_end
            compare_rows = []
            saved_view_links = []
            anomalies = []
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "metrics.html",
            db,
            rows=rows,
            totals=summarize_metrics(rows, METRICS),
            start_date=range_start,
            end_date=range_end,
            comparison_day=compare_target,
            metrics=METRICS,
            platforms=platforms,
            accounts=accounts,
            selected_platforms=selected_platforms,
            selected_account_ids=selected_account_ids,
            account_groups=comparison_groups(compare_rows, "account"),
            platform_groups=comparison_groups(compare_rows, "platform"),
            anomalies=anomalies,
            forecast_views=forecast_trend([(row.metric_date, row.views) for row in rows if row.metric_date >= range_end - timedelta(days=14)]),
            saved_views=saved_view_links,
            message=message,
        )


@app.post("/metrics/views")
def save_metric_view(
    request: Request,
    name: str = Form(),
    start_date: str = Form(""),
    end_date: str = Form(""),
    comparison_day: str = Form(""),
    platform: str = Form(""),
    account_ids: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = current_actor(request, db)
        if not user:
            return redirect("/metrics", "没有权限")
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "comparison_day": comparison_day,
            "platform": [item for item in platform.split(",") if item],
            "account_ids": [int(item) for item in account_ids.split(",") if item.isdigit()],
        }
        existing = db.scalar(select(SavedView).where(SavedView.name == name.strip(), SavedView.scope == "metrics"))
        if existing:
            existing.params_json = json.dumps(params, ensure_ascii=False)
            existing.updated_at = datetime.now().replace(microsecond=0)
            existing.created_by = user.id
        else:
            db.add(SavedView(name=name.strip(), scope="metrics", params_json=json.dumps(params, ensure_ascii=False), created_by=user.id))
        db.commit()
    return redirect("/metrics", "筛选条件已保存")


@app.post("/metrics/views/{view_id}/delete")
def delete_metric_view(view_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = current_actor(request, db)
        if not can(user, "delete_data"):
            return redirect("/metrics", "没有权限")
        view = db.get(SavedView, view_id)
        if view:
            db.delete(view)
            db.commit()
    return redirect("/metrics", "已删除看板")


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    with SessionLocal() as db:
        try:
            reports = db.scalars(select(Report).where(Report.deleted_at.is_(None)).order_by(Report.report_date.desc())).all()
            summary_reports = db.scalars(select(SummaryReport).where(SummaryReport.deleted_at.is_(None)).order_by(SummaryReport.end_date.desc())).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/reports", exc)
            reports = []
            summary_reports = []
            message = "数据加载失败，请稍后重试"
        return page(request, "reports.html", db, reports=reports, summary_reports=summary_reports, message=message)


@app.get("/hotspots", response_class=HTMLResponse)
def hotspots_page(request: Request, report_date: str = ""):
    with SessionLocal() as db:
        try:
            selected_date = None
            if report_date:
                try:
                    selected_date = date.fromisoformat(report_date)
                except ValueError:
                    selected_date = None
            history = db.scalars(select(HotspotReport).order_by(HotspotReport.report_date.desc()).limit(14)).all()
            report = None
            if selected_date:
                report = db.scalar(select(HotspotReport).where(HotspotReport.report_date == selected_date))
            if not report:
                report = history[0] if history else None
            payload = report_payload(report)
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/hotspots", exc)
            selected_date = None
            history = []
            report = None
            payload = report_payload(None)
            message = "数据加载失败，请稍后重试"
        return page(request, "hotspots.html", db, report=report, payload=payload, hotspot_history=history, selected_hotspot_date=selected_date or (report.report_date if report else None), message=message)


@app.post("/hotspots/refresh")
def refresh_hotspots(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_hotspots"):
            return redirect("/hotspots", "没有权限")
        report = generate_hotspots(db)
        log_operation(db, current_actor(request, db), "生成", "热点", report.report_date.isoformat(), "刷新热点")
        db.commit()
        if report.status == "failed":
            return redirect("/hotspots", report.error_message)
        message = "热点已更新" if not report.error_message else report.error_message
        return redirect("/hotspots", message)


@app.post("/reports/generate")
def create_report(request: Request, report_date: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_reports"):
            return redirect("/reports", "没有权限")
        try:
            target = date.fromisoformat(report_date) if report_date else None
            report = generate_report(db, target=target, use_latest=not bool(target))
            log_operation(db, current_actor(request, db), "生成", "日报", report.title, f"日期：{report.report_date}")
            db.commit()
            return redirect(f"/reports/{report.id}", "日报已生成")
        except Exception as exc:
            db.rollback()
            return redirect("/reports", f"生成失败：{exc}")


@app.post("/reports/generate-summary")
def create_summary_report(request: Request, period_type: str = Form(), report_date: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_reports"):
            return redirect("/reports", "没有权限")
        try:
            target = date.fromisoformat(report_date) if report_date else None
            report = generate_summary_report(db, period_type=period_type, target=target)
            log_operation(db, current_actor(request, db), "生成", "周月报", report.title, f"周期：{period_type}")
            db.commit()
            return redirect(f"/summary-reports/{report.id}", f"{'周报' if period_type == 'weekly' else '月报'}已生成")
        except Exception as exc:
            db.rollback()
            return redirect("/reports", f"生成失败：{exc}")


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request):
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        return page(request, "report_detail.html", db, report=report)


@app.post("/reports/{report_id}/edit")
def edit_report(report_id: int, request: Request, title: str = Form(""), markdown_content: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_reports")
        if not actor:
            return redirect("/reports", "没有权限")
        report = db.get(Report, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        report.title = title.strip() or report.title
        report.markdown_content = markdown_content.strip()
        report.html_content = md.markdown(report.markdown_content, extensions=["tables", "fenced_code"])
        report.updated_at = datetime.now().replace(microsecond=0)
        log_operation(db, actor, "编辑", "报告", report.title, "手动编辑日报内容")
        db.commit()
    return redirect(f"/reports/{report_id}", "报告已保存")


@app.get("/reports/{report_id}/markdown")
def download_markdown(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(Report, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
        log_operation(db, current_user(request, db), "导出", "报告", report.title, "导出 Markdown")
        db.commit()
        from fastapi.responses import Response

        return Response(
            report.markdown_content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="daily-{report.report_date}.md"'},
        )


@app.get("/reports/{report_id}/pdf")
def download_pdf(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(Report, report_id)
        if not report or not report.pdf_path or not Path(report.pdf_path).exists():
            return redirect(f"/reports/{report_id}", "PDF 暂时不可用，请重新生成报告")
        log_operation(db, current_user(request, db), "导出", "报告", report.title, "导出 PDF")
        db.commit()
        return FileResponse(report.pdf_path, filename=f"daily-{report.report_date}.pdf", media_type="application/pdf")


@app.get("/reports/{report_id}/docx")
def download_docx(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(Report, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
        path = render_docx(report)
        log_operation(db, current_user(request, db), "导出", "报告", report.title, "导出 Word")
        db.commit()
        return FileResponse(
            path,
            filename=f"daily-{report.report_date}.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


@app.post("/reports/{report_id}/email")
def email_report(report_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "send_email"):
            return redirect(f"/reports/{report_id}", "只有管理员可以发送邮件")
        report = db.get(Report, report_id)
        try:
            count = send_report_email(db, report)
            db.commit()
            return redirect(f"/reports/{report_id}", f"已发送给 {count} 位收件人")
        except Exception as exc:
            db.rollback()
            return redirect(f"/reports/{report_id}", f"发送失败：{exc}")


@app.post("/reports/{report_id}/rename")
def rename_report(report_id: int, request: Request, title: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_reports"):
            return redirect("/reports", "没有权限")
        report = db.get(Report, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        report.title = title.strip() or report.title
        log_operation(db, current_actor(request, db), "编辑", "报告", report.title, "重命名日报")
        db.commit()
    return redirect("/reports", "报告名称已更新")


@app.post("/reports/{report_id}/delete")
def delete_report(report_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "delete_data")
        if not actor:
            return redirect("/reports", "没有权限")
        report = db.get(Report, report_id)
        if report and not report.deleted_at:
            report.deleted_at = datetime.now().replace(microsecond=0)
            report.deleted_by = actor.id
            log_operation(db, actor, "删除", "报告", report.title, "删除日报")
            db.commit()
    return redirect("/reports", "报告已删除")


@app.get("/summary-reports/{report_id}", response_class=HTMLResponse)
def summary_report_detail(report_id: int, request: Request):
    with SessionLocal() as db:
        report = db.get(SummaryReport, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        return page(request, "report_detail.html", db, report=report)


@app.post("/summary-reports/{report_id}/edit")
def edit_summary_report(report_id: int, request: Request, title: str = Form(""), markdown_content: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_reports")
        if not actor:
            return redirect("/reports", "没有权限")
        report = db.get(SummaryReport, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        report.title = title.strip() or report.title
        report.markdown_content = markdown_content.strip()
        report.html_content = md.markdown(report.markdown_content, extensions=["tables", "fenced_code"])
        report.updated_at = datetime.now().replace(microsecond=0)
        log_operation(db, actor, "编辑", "周月报", report.title, "手动编辑周报/月报内容")
        db.commit()
    return redirect(f"/summary-reports/{report_id}", "报告已保存")


@app.post("/summary-reports/{report_id}/rename")
def rename_summary_report(report_id: int, request: Request, title: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_reports"):
            return redirect("/reports", "没有权限")
        report = db.get(SummaryReport, report_id)
        if not report or report.deleted_at:
            return redirect("/reports", "报告不存在")
        report.title = title.strip() or report.title
        log_operation(db, current_actor(request, db), "编辑", "周月报", report.title, "重命名周报/月报")
        db.commit()
    return redirect("/reports", "报告名称已更新")


@app.post("/summary-reports/{report_id}/delete")
def delete_summary_report(report_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "delete_data")
        if not actor:
            return redirect("/reports", "没有权限")
        report = db.get(SummaryReport, report_id)
        if report and not report.deleted_at:
            report.deleted_at = datetime.now().replace(microsecond=0)
            report.deleted_by = actor.id
            log_operation(db, actor, "删除", "周月报", report.title, "删除周报/月报")
            db.commit()
    return redirect("/reports", "报告已删除")


@app.get("/summary-reports/{report_id}/pdf")
def summary_report_pdf(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(SummaryReport, report_id)
        if not report or not report.pdf_path or not Path(report.pdf_path).exists():
            return redirect(f"/summary-reports/{report_id}", "PDF 暂时不可用")
        log_operation(db, current_user(request, db), "导出", "周月报", report.title, "导出 PDF")
        db.commit()
        return FileResponse(report.pdf_path, filename=f"summary-{report.end_date}.pdf", media_type="application/pdf")


@app.get("/summary-reports/{report_id}/docx")
def summary_report_docx(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(SummaryReport, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
        path = render_docx(report)
        log_operation(db, current_user(request, db), "导出", "周月报", report.title, "导出 Word")
        db.commit()
        return FileResponse(
            path,
            filename=f"summary-{report.end_date}.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


@app.get("/summary-reports/{report_id}/markdown")
def summary_report_markdown(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(SummaryReport, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
        log_operation(db, current_user(request, db), "导出", "周月报", report.title, "导出 Markdown")
        db.commit()
        from fastapi.responses import Response

        return Response(
            report.markdown_content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="summary-{report.end_date}.md"'},
        )


def _review_scope_label(range_type: str) -> str:
    return {
        "yesterday": "昨日",
        "7d": "最近7天",
        "30d": "最近30天",
        "custom": "自定义日期",
    }.get(range_type, "自定义日期")


def _report_kind_label(report_kind: str) -> str:
    return {
        "weekly": "周报",
        "monthly": "月报",
        "project": "项目复盘",
        "event": "活动复盘",
    }.get(report_kind, "周报")


def _topic_business(keyword: str) -> str:
    value = keyword or ""
    if "足球" in value:
        return "无人机足球"
    if "考证" in value or "证书" in value:
        return "无人机考证"
    if "学历" in value or "提升" in value:
        return "成人学历"
    return "其他"


def _topic_status_options() -> list[str]:
    return ["待拍摄", "已拍摄", "已发布", "已放弃"]


@app.get("/ai-review", response_class=HTMLResponse)
def ai_review_page(request: Request, range_type: str = "7d", platform: str = "", account_id: int = 0):
    return redirect("/report-builder", "AI分析已合并到周报月报")


@app.post("/ai-review/generate")
def generate_ai_review(
    request: Request,
    range_type: str = Form("7d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    platform: str = Form(""),
    account_id: int = Form(0),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    return redirect("/report-builder", "AI分析已合并到周报月报")


@app.get("/report-builder", response_class=HTMLResponse)
def report_builder_page(request: Request):
    with SessionLocal() as db:
        try:
            accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None), PlatformAccount.is_active.is_(True)).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
            drafts = db.scalars(select(GeneratedReportDraft).order_by(GeneratedReportDraft.created_at.desc()).limit(8)).all()
            ai_reviews = db.scalars(select(AiReview).order_by(AiReview.created_at.desc()).limit(8)).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/report-builder", exc)
            accounts = []
            drafts = []
            ai_reviews = []
            message = "数据加载失败，请稍后重试"
        return page(request, "report_builder.html", db, accounts=accounts, drafts=drafts, ai_reviews=ai_reviews, draft=None, message=message)


@app.post("/report-builder/generate")
def generate_report_builder(
    request: Request,
    report_kind: str = Form("weekly"),
    range_type: str = Form("7d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    platform: str = Form(""),
    account_id: int = Form(0),
    include_charts: str = Form(""),
    include_topic_suggestions: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "use_report_builder"):
            return redirect("/report-builder", "没有权限")
        result = generateAIReport(
            db,
            {
                "range_type": range_type,
                "start_date": start_date,
                "end_date": end_date,
                "platform": platform,
                "account_id": account_id,
                "include_charts": include_charts,
                "include_topic_suggestions": include_topic_suggestions,
            },
            report_kind,
        )
        title = f"{_report_kind_label(report_kind)} · {result['start_date']} 至 {result['end_date']}"
        sections = [
            f"# {title}",
            "",
            "## 本周/本月运营概况" if report_kind in {"weekly", "monthly"} else "## 项目/活动概况",
            result["markdown"],
        ]
        draft = GeneratedReportDraft(
            report_kind=report_kind,
            title=title,
            start_date=result["start_date"],
            end_date=result["end_date"],
            platform=platform,
            account_id=account_id or None,
            markdown_content="\n".join(sections),
            text_content=result["copy_text"],
            ppt_outline="",
        )
        db.add(draft)
        ai_review = AiReview(
            report_type="ai_review",
            range_type=range_type,
            start_date=result["start_date"],
            end_date=result["end_date"],
            platform=platform,
            account_id=account_id or None,
            prompt_type=report_kind,
            markdown_content=result["markdown"],
            summary_json=json.dumps(result["summary_json"], ensure_ascii=False),
            copy_text=result["copy_text"],
        )
        db.add(ai_review)
        log_operation(db, current_actor(request, db), "生成", "报告草稿", title, "生成周报/月报草稿")
        db.commit()
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None), PlatformAccount.is_active.is_(True)).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
        drafts = db.scalars(select(GeneratedReportDraft).order_by(GeneratedReportDraft.created_at.desc()).limit(8)).all()
        ai_reviews = db.scalars(select(AiReview).order_by(AiReview.created_at.desc()).limit(8)).all()
        return page(request, "report_builder.html", db, accounts=accounts, drafts=drafts, ai_reviews=ai_reviews, draft=draft, message="周报/月报已生成")


@app.get("/topic-center", response_class=HTMLResponse)
def topic_center_page(request: Request, keyword: str = "", business: str = "无人机足球"):
    with SessionLocal() as db:
        try:
            topics = db.scalars(select(TopicIdea).order_by(TopicIdea.created_at.desc()).limit(40)).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/topic-center", exc)
            topics = []
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "topic_center.html",
            db,
            topics=topics,
            keyword=keyword,
            business=business,
            topic_status_options=_topic_status_options(),
            message=message,
        )


@app.post("/topic-center")
def add_topic_idea(
    request: Request,
    title: str = Form(),
    platform: str = Form("抖音"),
    business: str = Form("其他"),
    content_type: str = Form("科普"),
    reference_link: str = Form(""),
    owner_name: str = Form(""),
    priority: str = Form("B"),
    status: str = Form("待拍摄"),
    note: str = Form(""),
    angle: str = Form(""),
    script_direction: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        if not title.strip():
            return redirect("/topic-center", "标题不能为空")
        item = TopicIdea(
            title=title.strip(),
            platform=platform.strip(),
            business=business.strip() or "其他",
            content_type=content_type.strip() or "科普",
            reference_link=reference_link.strip(),
            owner_name=owner_name.strip(),
            priority=priority.strip() or "B",
            status=status.strip() or "待拍摄",
            note=note.strip(),
            angle=angle.strip(),
            script_direction=script_direction.strip(),
        )
        db.add(item)
        log_operation(db, actor, "新增", "选题", item.title, f"平台：{item.platform}；负责人：{item.owner_name}")
        db.commit()
    return redirect("/topic-center", "选题已添加")


@app.post("/topic-center/{topic_id}/edit")
def edit_topic_idea(
    topic_id: int,
    request: Request,
    title: str = Form(),
    platform: str = Form(""),
    business: str = Form(""),
    content_type: str = Form(""),
    reference_link: str = Form(""),
    owner_name: str = Form(""),
    priority: str = Form("B"),
    status: str = Form("待拍摄"),
    note: str = Form(""),
    angle: str = Form(""),
    script_direction: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        topic = db.get(TopicIdea, topic_id)
        if not topic:
            return redirect("/topic-center", "选题不存在")
        if not title.strip():
            return redirect("/topic-center", "标题不能为空")
        topic.title = title.strip()
        topic.platform = platform.strip()
        topic.business = business.strip() or "其他"
        topic.content_type = content_type.strip() or "科普"
        topic.reference_link = reference_link.strip()
        topic.owner_name = owner_name.strip()
        topic.priority = priority.strip() or "B"
        topic.status = status.strip() or "待拍摄"
        topic.note = note.strip()
        topic.angle = angle.strip()
        topic.script_direction = script_direction.strip()
        log_operation(db, actor, "编辑", "选题", topic.title, f"平台：{topic.platform}；状态：{topic.status}")
        db.commit()
    return redirect("/topic-center", "选题已更新")


@app.post("/topic-center/{topic_id}/favorite")
def favorite_topic_idea(topic_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        topic = db.get(TopicIdea, topic_id)
        if topic:
            topic.is_favorite = not topic.is_favorite
            log_operation(db, actor, "收藏", "选题", topic.title, "标记收藏" if topic.is_favorite else "取消收藏")
            db.commit()
    return redirect("/topic-center", "收藏状态已更新")


@app.post("/topic-center/{topic_id}/status")
def update_topic_status(topic_id: int, request: Request, status: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        topic = db.get(TopicIdea, topic_id)
        if topic:
            topic.status = status.strip() or topic.status
            log_operation(db, actor, "编辑", "选题", topic.title, f"状态：{topic.status}")
            db.commit()
    return redirect("/topic-center", "状态已更新")


@app.post("/topic-center/{topic_id}/delete")
def delete_topic_idea(topic_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        topic = db.get(TopicIdea, topic_id)
        if topic:
            log_operation(db, actor, "删除", "选题", topic.title, f"平台：{topic.platform}")
            db.delete(topic)
            db.commit()
    return redirect("/topic-center", "选题已删除")


@app.post("/topic-center/generate")
def generate_topic_center(
    request: Request,
    keyword: str = Form(""),
    platform: str = Form("抖音"),
    business: str = Form("其他"),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "use_topic_center"):
            return redirect("/topic-center", "没有权限")
        hot_topics = fetchHotTopics(platform, keyword)
        created = 0
        base_business = business if business and business != "其他" else _topic_business(keyword)
        content_types = ["科普", "招生", "活动", "热点", "故事", "成交案例"]
        platforms = ["抖音", "小红书", "视频号", "公众号", "其他"]
        for index in range(20):
            hot = hot_topics[index % len(hot_topics)]
            title = f"{keyword or base_business} 选题 {index + 1}：{hot['topic']}"
            db.add(
                TopicIdea(
                    title=title,
                    business=base_business,
                    content_type=content_types[index % len(content_types)],
                    platform="、".join(platforms[index % 3 : index % 3 + 2]),
                    priority="S" if index < 4 else ("A" if index < 12 else "B"),
                    status="待拍摄",
                    reference_link=hot["reference"],
                    note=hot["trend"],
                    angle=hot["angle"],
                    script_direction=f"围绕 {keyword or base_business} 的真实问题展开，优先讲场景和结果。",
                    is_ai_generated=True,
                )
            )
            created += 1
        log_operation(db, current_actor(request, db), "生成", "选题", keyword or base_business, f"新增 {created} 条选题")
        db.commit()
        topics = db.scalars(select(TopicIdea).order_by(TopicIdea.created_at.desc()).limit(40)).all()
        return page(request, "topic_center.html", db, topics=topics, keyword=keyword, business=base_business, message=f"已生成 {created} 个选题")


def build_breakdown_overview(
    db: Session,
    q: str = "",
    platform: str = "",
    sort_by: str = "created_at",
    order: str = "desc",
    page_no: int = 1,
    selected_case_id: int | None = None,
):
    try:
        query = select(VideoBreakdown)
        if q:
            needle = q.strip()
            query = query.where(or_(VideoBreakdown.title.contains(needle), VideoBreakdown.platform.contains(needle), VideoBreakdown.cover_description.contains(needle), VideoBreakdown.source_url.contains(needle)))
        if platform:
            query = query.where(VideoBreakdown.platform == platform)
        sort_map = {
            "views": VideoBreakdown.views,
            "likes": VideoBreakdown.likes,
            "comments": VideoBreakdown.comments,
            "created_at": VideoBreakdown.created_at,
        }
        sort_column = sort_map.get(sort_by, VideoBreakdown.created_at)
        sort_order = sort_column.asc() if order == "asc" else sort_column.desc()
        all_cases = db.scalars(query.order_by(sort_order)).all()
    except Exception:
        all_cases = []
    total_cases = len(all_cases)
    per_page = 8
    page_no = max(1, page_no)
    total_pages = max(1, (total_cases + per_page - 1) // per_page)
    page_no = min(page_no, total_pages)
    start = (page_no - 1) * per_page
    end = start + per_page
    cases = all_cases[start:end]
    case_cards = []
    for item in cases:
        try:
            payload = safe_json_loads(getattr(item, "analysis_json", "") or "", {})
            score = payload.get("score", {}) if isinstance(payload, dict) else {}
            score_value = score.get("explosive_potential") if isinstance(score, dict) else None
            case_cards.append(
                {
                    "item": item,
                    "analysis_status": item.analysis_status or item.status or "未开始",
                    "fetch_status": item.fetch_status or "未抓取",
                    "score": score_value if item.analysis_status == "已完成" and score_value not in {None, "", 0, 0.0} else "数据未填写",
                }
            )
        except Exception:
            case_cards.append(
                {
                    "item": item,
                    "analysis_status": getattr(item, "analysis_status", "") or getattr(item, "status", "") or "未开始",
                    "fetch_status": getattr(item, "fetch_status", "") or "未抓取",
                    "score": "数据未填写",
                }
            )
    try:
        avg_views = sum((case.views or 0) for case in all_cases) / total_cases if total_cases else 0
    except Exception:
        avg_views = 0
    try:
        latest_case = db.scalar(select(VideoBreakdown).order_by(VideoBreakdown.created_at.desc()))
    except Exception:
        latest_case = None
    try:
        report = db.get(VideoBreakdown, selected_case_id) if selected_case_id else latest_case
    except Exception:
        report = latest_case
    if report and report.id not in {item.id for item in all_cases}:
        report = latest_case
    latest_status, latest_progress = breakdown_display_status((report.analysis_status if report else "未开始") or (report.status if report else "未开始"), report.progress if report else 0)
    eta = "处理中" if latest_status == "分析中" else ("已完成" if latest_status == "已完成" else ("失败" if latest_status == "失败" else "未开始"))
    selected_platform = platform if platform in {"抖音", "小红书", "视频号", "公众号", "其他"} else ""
    return {
        "cases": cases,
        "recent_cases": case_cards[:5],
        "hot_rankings": sorted(all_cases, key=lambda item: item.views or 0, reverse=True)[:5],
        "report": report,
        "report_data": breakdown_report_data(report),
        "q": q,
        "selected_platform": selected_platform,
        "sort_by": sort_by if sort_by in sort_map else "created_at",
        "sort_order": "asc" if order == "asc" else "desc",
        "page_no": page_no,
        "total_pages": total_pages,
        "total_cases": total_cases,
        "breakdown_stats": {
            "total_cases": total_cases,
            "avg_views": avg_views,
            "progress": latest_progress if report else 0,
            "analysis_status": latest_status if report else "未开始",
            "fetch_status": report.fetch_status if report else "未抓取",
            "eta": eta if report else "未开始",
        },
    }


def _breakdown_draft_payload(payload: dict[str, object] | None = None, *, step2_open: bool = True) -> dict[str, object]:
    payload = payload or {}
    return {
        "source_url": str(payload.get("source_url") or ""),
        "platform": str(payload.get("platform") or "其他"),
        "title": str(payload.get("title") or ""),
        "cover_url": str(payload.get("cover_url") or ""),
        "cover_description": str(payload.get("cover_description") or ""),
        "author_name": str(payload.get("author_name") or ""),
        "publish_time": str(payload.get("publish_time") or ""),
        "duration": str(payload.get("duration") or ""),
        "views": float(payload.get("views") or 0),
        "likes": float(payload.get("likes") or 0),
        "comments": float(payload.get("comments") or 0),
        "collect_count": float(payload.get("collect_count") or 0),
        "share_count": float(payload.get("share_count") or 0),
        "video_text": str(payload.get("video_text") or ""),
        "transcript": str(payload.get("transcript") or ""),
        "fetch_status": str(payload.get("fetch_status") or "未抓取"),
        "fetch_error": str(payload.get("fetch_error") or ""),
        "step2_open": step2_open,
    }


@app.get("/breakdown", response_class=HTMLResponse)
def breakdown_page(request: Request, q: str = "", platform: str = "", sort_by: str = "created_at", order: str = "desc", page_no: int = 1):
    with SessionLocal() as db:
        try:
            context = build_breakdown_overview(db, q=q, platform=platform, sort_by=sort_by, order=order, page_no=page_no)
            draft = request.session.get("breakdown_draft")
            if isinstance(draft, dict):
                context["draft_breakdown"] = draft
                context["draft_step2_open"] = bool(draft.get("step2_open"))
            return page(request, "breakdown.html", db, **context)
        except Exception as exc:
            user = current_user(request, db)
            if not user:
                return redirect("/auth/login")
            runtime = runtime_settings(db)
            fallback_context = {
                "user": user,
                "user_role_label": role_label(user.role),
                "permissions": {name: can(user, name) for name in PERMISSIONS},
                "settings": runtime,
                "csrf_token": csrf_token(request),
                "message": "爆款拆解数据加载失败，请稍后重试。",
                "q": q,
                "selected_platform": "",
                "sort_by": "created_at",
                "sort_order": "desc",
                "page_no": 1,
                "total_pages": 1,
                "total_cases": 0,
                "cases": [],
                "recent_cases": [],
                "hot_rankings": [],
                "report": None,
                "report_data": {},
                "breakdown_stats": {
                    "total_cases": 0,
                    "avg_views": 0,
                    "progress": 0,
                    "analysis_status": "未开始",
                    "fetch_status": "未抓取",
                    "eta": "未开始",
                },
                "draft_breakdown": _breakdown_draft_payload({}, step2_open=False),
                "draft_step2_open": False,
            }
            return templates.TemplateResponse(request, "breakdown.html", fallback_context)


@app.get("/breakdown/{breakdown_id}", response_class=HTMLResponse)
def breakdown_detail(request: Request, breakdown_id: int, q: str = "", platform: str = "", sort_by: str = "created_at", order: str = "desc", page_no: int = 1):
    with SessionLocal() as db:
        try:
            if not db.get(VideoBreakdown, breakdown_id):
                return redirect("/breakdown", "案例不存在")
            context = build_breakdown_overview(db, q=q, platform=platform, sort_by=sort_by, order=order, page_no=page_no, selected_case_id=breakdown_id)
            draft = request.session.get("breakdown_draft")
            if isinstance(draft, dict):
                context["draft_breakdown"] = draft
                context["draft_step2_open"] = bool(draft.get("step2_open"))
            return page(request, "breakdown.html", db, **context)
        except Exception:
            return redirect("/breakdown", "爆款拆解数据加载失败，请稍后重试。")


def _parse_breakdown_number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return max(float(value), 0.0)
    text = clean_text(value)
    if not text or text == "数据未填写":
        return 0.0
    text = text.replace(",", "").replace("，", "")
    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    try:
        return max(float(text) * multiplier, 0.0)
    except Exception:
        return 0.0


def _has_negative_breakdown_value(*values: object) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (int, float)) and float(value) < 0:
            return True
        text = clean_text(value).replace(",", "").replace("，", "")
        if not text or text == "数据未填写":
            continue
        try:
            if float(text.rstrip("万亿")) < 0:
                return True
        except Exception:
            continue
    return False


def _breakdown_payload_from_input(data: dict[str, object], fetched: dict[str, object] | None = None) -> dict[str, object]:
    fetched = fetched or {}
    source_url = normalize_breakdown_url(str(data.get("source_url") or fetched.get("source_url") or ""))
    platform = clean_text(data.get("platform")) or clean_text(fetched.get("platform")) or detect_platform_from_url(source_url)
    title = clean_text(data.get("title")) or clean_text(fetched.get("title")) or "未填写"
    return {
        "source_url": source_url,
        "platform": platform or "其他",
        "title": title,
        "cover_url": clean_text(data.get("cover_url")) or clean_text(fetched.get("cover_url")) or "",
        "cover_description": clean_text(data.get("cover_description")) or clean_text(fetched.get("cover_description")) or "",
        "author_name": clean_text(data.get("author_name")) or clean_text(fetched.get("author_name")) or "",
        "publish_time": clean_text(data.get("publish_time")) or clean_text(fetched.get("publish_time")) or "",
        "duration": clean_text(data.get("duration")) or clean_text(fetched.get("duration")) or "",
        "views": _parse_breakdown_number(data.get("views") or fetched.get("views")),
        "likes": _parse_breakdown_number(data.get("likes") or fetched.get("likes")),
        "comments": _parse_breakdown_number(data.get("comments") or fetched.get("comments")),
        "collect_count": _parse_breakdown_number(data.get("collect_count") or fetched.get("collect_count")),
        "share_count": _parse_breakdown_number(data.get("share_count") or fetched.get("share_count")),
        "video_text": clean_text(data.get("video_text")) or clean_text(fetched.get("video_text")) or "",
        "transcript": clean_text(data.get("transcript")) or clean_text(fetched.get("transcript")) or "",
        "fetch_status": clean_text(data.get("fetch_status")) or clean_text(fetched.get("fetch_status")) or "未抓取",
        "fetch_error": clean_text(data.get("fetch_error")) or clean_text(fetched.get("fetch_error")) or "",
    }


def _apply_breakdown_payload(case: VideoBreakdown, payload: dict[str, object]) -> None:
    case.source_url = str(payload.get("source_url") or "")
    case.platform = str(payload.get("platform") or "")
    case.title = str(payload.get("title") or "")
    case.cover_url = str(payload.get("cover_url") or "")
    case.cover_description = str(payload.get("cover_description") or "")
    case.author_name = str(payload.get("author_name") or "")
    case.publish_time = str(payload.get("publish_time") or "")
    case.duration = str(payload.get("duration") or "")
    case.views = float(payload.get("views") or 0)
    case.likes = float(payload.get("likes") or 0)
    case.comments = float(payload.get("comments") or 0)
    case.collect_count = float(payload.get("collect_count") or 0)
    case.share_count = float(payload.get("share_count") or 0)
    case.video_text = str(payload.get("video_text") or "")
    case.transcript = str(payload.get("transcript") or "")
    case.fetch_status = str(payload.get("fetch_status") or "未抓取")
    case.fetch_error = str(payload.get("fetch_error") or "")


def _create_breakdown_case(db: Session, payload: dict[str, object], actor: User | None, *, status: str = "分析中", progress: int = 15, error_message: str = "") -> VideoBreakdown:
    case = VideoBreakdown(
        source_url=str(payload.get("source_url") or ""),
        platform=str(payload.get("platform") or ""),
        title=str(payload.get("title") or ""),
        cover_url=str(payload.get("cover_url") or ""),
        cover_description=str(payload.get("cover_description") or ""),
        author_name=str(payload.get("author_name") or ""),
        publish_time=str(payload.get("publish_time") or ""),
        duration=str(payload.get("duration") or ""),
        views=float(payload.get("views") or 0),
        likes=float(payload.get("likes") or 0),
        comments=float(payload.get("comments") or 0),
        collect_count=float(payload.get("collect_count") or 0),
        share_count=float(payload.get("share_count") or 0),
        video_text=str(payload.get("video_text") or ""),
        transcript=str(payload.get("transcript") or ""),
        fetch_status=str(payload.get("fetch_status") or "未抓取"),
        fetch_error=str(payload.get("fetch_error") or ""),
        analysis_json=json.dumps({"status": status}, ensure_ascii=False),
        analysis_markdown="",
        analysis_status=status,
        status=status,
        progress=progress,
        error_message=error_message,
    )
    db.add(case)
    log_operation(db, actor, "新增", "爆款拆解", case.title or case.source_url or "未命名", f"抓取状态：{case.fetch_status}；分析状态：{status}")
    return case


def _finalize_breakdown_failure(case: VideoBreakdown, message: str) -> None:
    case.analysis_status = "失败"
    case.status = "失败"
    case.progress = min(max(int(case.progress or 0), 0), 99)
    case.analysis_markdown = ""
    case.error_message = message
    case.analysis_json = json.dumps({"error": message}, ensure_ascii=False)


def _run_breakdown_task(breakdown_id: int, payload: dict[str, str]) -> None:
    with SessionLocal() as db:
        case = db.get(VideoBreakdown, breakdown_id)
        if not case:
            return
        runtime = runtime_settings(db)
        case.analysis_status = "分析中"
        case.status = "分析中"
        case.progress = 15
        case.error_message = ""
        case.updated_at = datetime.now().replace(microsecond=0)
        db.commit()
        try:
            case.progress = 34
            case.updated_at = datetime.now().replace(microsecond=0)
            db.commit()
            analysis, markdown = generate_breakdown_analysis(runtime, payload)
            if not markdown.strip():
                raise RuntimeError("未生成拆解结果，请重试。")
            case.analysis_json = json.dumps(analysis, ensure_ascii=False)
            case.analysis_markdown = markdown
            case.analysis_status = "已完成"
            case.status = "已完成"
            case.progress = 100
            case.error_message = ""
        except Exception as exc:
            remember_runtime_error(f"/breakdown/tasks/{breakdown_id}", exc)
            case.analysis_status = "失败"
            case.status = "失败"
            case.progress = min(max(case.progress or 0, 0), 99)
            case.analysis_markdown = ""
            if isinstance(exc, RuntimeError) and str(exc).strip():
                case.error_message = str(exc).strip()
            else:
                case.error_message = friendly_openai_error(exc)
            case.analysis_json = json.dumps({"error": case.error_message}, ensure_ascii=False)
        case.updated_at = datetime.now().replace(microsecond=0)
        db.commit()


@app.get("/breakdown/{breakdown_id}/status")
def breakdown_status(request: Request, breakdown_id: int):
    with SessionLocal() as db:
        if not current_user(request, db):
            return json_error("请先登录", status_code=401)
        case = db.get(VideoBreakdown, breakdown_id)
        if not case:
            return json_error("案例不存在", status_code=404)
        data = breakdown_report_data(case)
        return json_success(
            ok=True,
            id=case.id,
            status=case.status or "未开始",
            analysis_status=case.analysis_status or case.status or "未开始",
            fetch_status=case.fetch_status or "未抓取",
            progress=int(case.progress or 0),
            error_message=case.error_message or "",
            fetch_error=case.fetch_error or "",
            analysis_markdown=case.analysis_markdown or "",
            markdown_html=data.get("markdown_html", ""),
            title=case.title or "爆款拆解报告",
            created_at=case.created_at.isoformat(),
            updated_at=case.updated_at.isoformat() if getattr(case, "updated_at", None) else "",
        )


def _serialize_breakdown_case(case: VideoBreakdown) -> dict[str, object]:
    report_data = breakdown_report_data(case)
    return {
        "id": case.id,
        "platform": case.platform or "其他",
        "source_url": case.source_url or "",
        "title": case.title or "",
        "cover_url": case.cover_url or "",
        "cover_description": case.cover_description or "",
        "author_name": case.author_name or "",
        "publish_time": case.publish_time or "",
        "duration": case.duration or "",
        "views": case.views or 0,
        "likes": case.likes or 0,
        "comments": case.comments or 0,
        "collect_count": case.collect_count or 0,
        "share_count": case.share_count or 0,
        "video_text": case.video_text or "",
        "transcript": case.transcript or "",
        "fetch_status": case.fetch_status or "未抓取",
        "fetch_error": case.fetch_error or "",
        "analysis_status": case.analysis_status or case.status or "未开始",
        "status": case.status or "未开始",
        "progress": int(case.progress or 0),
        "analysis_markdown": case.analysis_markdown or "",
        "markdown_html": report_data.get("markdown_html", ""),
        "error_message": case.error_message or "",
        "created_at": case.created_at.isoformat() if case.created_at else "",
        "updated_at": case.updated_at.isoformat() if getattr(case, "updated_at", None) else "",
        "score": report_data.get("score", {}) if isinstance(report_data, dict) else {},
    }


@app.post("/api/viral/fetch-video-info")
async def api_fetch_video_info(request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return json_error("请先登录", status_code=401)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        video_url = str(payload.get("video_url") or payload.get("source_url") or "").strip()
        if not video_url:
            return json_error("请输入视频链接。", status_code=400, ok=False, fetch_status="未抓取", fetch_error="请输入视频链接。")
        result = fetch_video_info(video_url)
        normalized_url = normalize_breakdown_url(video_url)
        if not result.get("ok"):
            error = str(result.get("error") or "该平台可能限制自动抓取，请手动补充视频信息后继续拆解。")
            platform = str(result.get("platform") or detect_platform_from_url(normalized_url))
            return json_error(
                error,
                status_code=200,
                ok=False,
                platform=platform,
                fetch_status="抓取失败",
                fetch_error=error,
                data={
                    "source_url": normalized_url,
                    "platform": platform,
                    "title": "",
                    "cover_url": "",
                    "cover_description": "",
                    "author_name": "",
                    "publish_time": "",
                    "duration": "",
                    "views": 0,
                    "likes": 0,
                    "comments": 0,
                    "collect_count": 0,
                    "share_count": 0,
                    "video_text": "",
                    "transcript": "",
                    "fetch_status": "抓取失败",
                    "fetch_error": error,
                },
            )
        return json_success(ok=True, **result.get("data", {}), fetch_status="抓取成功", fetch_error="")


@app.post("/breakdown/actions/manual")
def breakdown_manual(request: Request, source_url: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not can(actor, "use_breakdown"):
            return redirect("/breakdown", "没有权限")
        draft = _breakdown_draft_payload({"source_url": source_url}, step2_open=True)
        request.session["breakdown_draft"] = draft
        return redirect("/breakdown", "已切换到手动填写")


@app.post("/breakdown/actions/fetch")
def breakdown_fetch(request: Request, source_url: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not can(actor, "use_breakdown"):
            return redirect("/breakdown", "没有权限")
        normalized_url = normalize_breakdown_url(source_url)
        if not normalized_url:
            request.session["breakdown_draft"] = _breakdown_draft_payload({"source_url": ""}, step2_open=True)
            return redirect("/breakdown", "请先输入视频链接")
        result = fetch_video_info(normalized_url)
        if not result.get("ok"):
            error = str(result.get("error") or "该平台可能限制自动抓取，请手动补充视频信息后继续拆解。")
            draft_data = dict(result.get("data") or {})
            draft_data.update({"source_url": normalized_url, "fetch_status": "抓取失败", "fetch_error": error})
            request.session["breakdown_draft"] = _breakdown_draft_payload(draft_data, step2_open=True)
            return redirect("/breakdown", error)
        draft_data = dict(result.get("data") or {})
        draft_data.update({"source_url": normalized_url, "fetch_status": "抓取成功", "fetch_error": ""})
        request.session["breakdown_draft"] = _breakdown_draft_payload(draft_data, step2_open=True)
        return redirect("/breakdown", "抓取成功，已自动填充基础信息")


@app.get("/api/viral/list")
def api_viral_list(request: Request, q: str = "", platform: str = "", page: int = 1, page_size: int = 20):
    with SessionLocal() as db:
        if not current_user(request, db):
            return json_error("请先登录", status_code=401)
        try:
            query = select(VideoBreakdown)
            if q:
                needle = q.strip()
                query = query.where(or_(VideoBreakdown.title.contains(needle), VideoBreakdown.platform.contains(needle), VideoBreakdown.cover_description.contains(needle), VideoBreakdown.source_url.contains(needle)))
            if platform:
                query = query.where(VideoBreakdown.platform == platform)
            total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
            page = max(1, page)
            page_size = max(1, min(page_size, 100))
            rows = db.scalars(query.order_by(VideoBreakdown.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        except Exception:
            total = 0
            page = max(1, page)
            page_size = max(1, min(page_size, 100))
            rows = []
        return json_success(ok=True, total=total, page=page, page_size=page_size, items=[_serialize_breakdown_case(item) for item in rows])


@app.get("/api/viral/{viral_id}")
def api_viral_detail(request: Request, viral_id: int):
    with SessionLocal() as db:
        if not current_user(request, db):
            return json_error("请先登录", status_code=401)
        try:
            case = db.get(VideoBreakdown, viral_id)
            if not case:
                return json_error("案例不存在", status_code=404)
            return json_success(ok=True, item=_serialize_breakdown_case(case))
        except Exception as exc:
            remember_runtime_error(f"/api/viral/{viral_id}", exc)
            return json_error("爆款拆解数据加载失败，请稍后重试。", status_code=500, ok=False)


@app.delete("/api/viral/{viral_id}")
def api_delete_viral(request: Request, viral_id: int):
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "delete_data"):
            return json_error("没有权限", status_code=403)
        case = db.get(VideoBreakdown, viral_id)
        if not case:
            return json_error("案例不存在", status_code=404)
        db.delete(case)
        log_operation(db, actor, "删除", "爆款拆解", case.title or case.source_url or "未命名", "通过 API 删除拆解案例")
        db.commit()
        return json_success(ok=True)


def _start_breakdown_analysis(db: Session, actor: User | None, payload: dict[str, object], background_tasks: BackgroundTasks | None = None) -> VideoBreakdown:
    runtime = runtime_settings(db)
    case = _create_breakdown_case(db, payload, actor, status="分析中", progress=15)
    db.commit()
    db.refresh(case)
    if not runtime.openai_api_key:
        _finalize_breakdown_failure(case, "未配置 OpenAI API Key，请在环境变量中配置 OPENAI_API_KEY。")
        db.commit()
        db.refresh(case)
        return case
    if background_tasks is not None:
        background_tasks.add_task(
            _run_breakdown_task,
            case.id,
            {
                "source_url": case.source_url,
                "title": case.title,
                "platform": case.platform,
                "views": case.views,
                "likes": case.likes,
                "comments": case.comments,
                "collect_count": case.collect_count,
                "share_count": case.share_count,
                "duration": case.duration,
                "cover_url": case.cover_url,
                "cover_description": case.cover_description,
                "author_name": case.author_name,
                "publish_time": case.publish_time,
                "video_text": case.video_text,
                "transcript": case.transcript,
                "script_content": case.video_text or case.transcript,
            },
        )
    else:
        _run_breakdown_task(
            case.id,
            {
                "source_url": case.source_url,
                "title": case.title,
                "platform": case.platform,
                "views": case.views,
                "likes": case.likes,
                "comments": case.comments,
                "collect_count": case.collect_count,
                "share_count": case.share_count,
                "duration": case.duration,
                "cover_url": case.cover_url,
                "cover_description": case.cover_description,
                "author_name": case.author_name,
                "publish_time": case.publish_time,
                "video_text": case.video_text,
                "transcript": case.transcript,
                "script_content": case.video_text or case.transcript,
            },
        )
        db.refresh(case)
    return case


@app.post("/api/viral/analyze")
async def api_viral_analyze(request: Request, background_tasks: BackgroundTasks):
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not can(actor, "use_breakdown"):
            return json_error("没有权限", status_code=403)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if _has_negative_breakdown_value(payload.get("views"), payload.get("likes"), payload.get("comments"), payload.get("collect_count"), payload.get("share_count")):
            return json_error("播放量、点赞、评论、收藏和转发不能为负数。", status_code=400)
        normalized = _breakdown_payload_from_input(payload)
        if not normalized["source_url"]:
            return json_error("视频链接格式不正确，请填写常见平台的 http 或 https 链接。", status_code=400)
        case = _start_breakdown_analysis(db, actor, normalized, background_tasks)
        db.commit()
        if case.status == "失败":
            return json_error(case.error_message or "拆解失败", status_code=400, ok=False, id=case.id)
        return json_success(ok=True, id=case.id, status=case.status, progress=case.progress, message="拆解任务已开始，完成后会自动显示结果")


@app.post("/breakdown/generate")
def generate_breakdown(
    request: Request,
    background_tasks: BackgroundTasks,
    source_url: str = Form(""),
    cover_url: str = Form(""),
    title: str = Form(""),
    platform: str = Form(""),
    author_name: str = Form(""),
    publish_time: str = Form(""),
    views: float = Form(0),
    likes: float = Form(0),
    comments: float = Form(0),
    collect_count: float = Form(0),
    share_count: float = Form(0),
    duration: str = Form(""),
    cover_description: str = Form(""),
    video_text: str = Form(""),
    transcript: str = Form(""),
    fetch_status: str = Form("未抓取"),
    fetch_error: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not can(actor, "use_breakdown"):
            return redirect("/breakdown", "没有权限")
        request.session.pop("breakdown_draft", None)
        normalized = _breakdown_payload_from_input(
            {
                "source_url": source_url,
                "cover_url": cover_url,
                "title": title,
                "platform": platform,
                "author_name": author_name,
                "publish_time": publish_time,
                "views": views,
                "likes": likes,
                "comments": comments,
                "collect_count": collect_count,
                "share_count": share_count,
                "duration": duration,
                "cover_description": cover_description,
                "video_text": video_text,
                "transcript": transcript,
                "fetch_status": fetch_status,
                "fetch_error": fetch_error,
            }
        )
        if not normalized["source_url"]:
            return redirect("/breakdown", "视频链接格式不正确，请填写常见平台的 http 或 https 链接")
        if _has_negative_breakdown_value(views, likes, comments, collect_count, share_count):
            return redirect("/breakdown", "播放量、点赞、评论、收藏和转发不能为负数")
        case = _start_breakdown_analysis(db, actor, normalized, background_tasks)
        db.commit()
        if case.analysis_status == "失败":
            return redirect(f"/breakdown/{case.id}", case.error_message)
        return redirect(f"/breakdown/{case.id}", "拆解任务已开始，完成后会自动显示结果")


@app.post("/breakdown/{breakdown_id}/delete")
def delete_breakdown(breakdown_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = current_actor(request, db)
        if not actor or not can(actor, "delete_data"):
            return redirect("/breakdown", "没有权限")
        case = db.get(VideoBreakdown, breakdown_id)
        if case:
            log_operation(db, actor, "删除", "爆款拆解", case.title or case.source_url or "未命名", "通过页面删除拆解案例")
            db.delete(case)
            db.commit()
    return redirect("/breakdown", "拆解案例已删除")


@app.get("/materials", response_class=HTMLResponse)
def materials_page(request: Request, q: str = "", tag: str = ""):
    with SessionLocal() as db:
        try:
            query = select(MaterialAsset).where(MaterialAsset.deleted_at.is_(None))
            if q:
                needle = q.strip()
                query = query.where(or_(MaterialAsset.name.contains(needle), MaterialAsset.note.contains(needle), MaterialAsset.project_name.contains(needle)))
            materials = db.scalars(query.order_by(MaterialAsset.is_favorite.desc(), MaterialAsset.created_at.desc()).limit(100)).all()
            material_rows = []
            for item in materials:
                tags = safe_json_loads(item.tags_json, [])
                if tag and tag not in tags:
                    continue
                material_rows.append({"item": item, "tags": tags})
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/materials", exc)
            material_rows = []
            message = "数据加载失败，请稍后重试"
        return page(request, "materials.html", db, materials=material_rows, q=q, tag=tag, message=message)


@app.post("/materials")
async def add_material(request: Request, csrf: str = Form(), name: str = Form(), asset_type: str = Form("图片"), project_name: str = Form(""), use_scene: str = Form(""), tags: str = Form(""), note: str = Form(""), file: UploadFile | None = File(None)):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user or not can(user, "manage_materials"):
            return redirect("/materials", "没有权限")
        file_name = ""
        file_path = ""
        if file and file.filename:
            suffix = Path(file.filename).suffix.lower()
            stored = settings.storage_dir / "uploads" / f"material-{uuid.uuid4().hex}{suffix}"
            with stored.open("wb") as output:
                shutil.copyfileobj(file.file, output)
            file_name = Path(file.filename).name
            file_path = str(stored)
        asset = MaterialAsset(
            name=name.strip(),
            asset_type=asset_type.strip() or "图片",
            project_name=project_name.strip(),
            use_scene=use_scene.strip(),
            tags_json=json.dumps(list(dict.fromkeys(get_tags(tags) + suggest_material_tags(name, project_name, use_scene, note))), ensure_ascii=False),
            uploader_name=user.display_name or user.username,
            file_name=file_name,
            file_path=file_path,
            note=note.strip(),
        )
        db.add(asset)
        db.commit()
        return redirect("/materials", "素材已添加")


@app.post("/materials/{material_id}/toggle-favorite")
def toggle_material_favorite(material_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return redirect("/materials", "没有权限")
        material = db.get(MaterialAsset, material_id)
        if material and material.deleted_at is None:
            material.is_favorite = not material.is_favorite
            db.commit()
    return redirect("/materials", "收藏状态已更新")


@app.post("/materials/{material_id}/delete")
def delete_material(material_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/materials", "没有权限")
        material = db.get(MaterialAsset, material_id)
        if material and material.deleted_at is None:
            material.deleted_at = datetime.now().replace(microsecond=0)
            material.deleted_by = current_actor(request, db).id if current_actor(request, db) else None
            db.commit()
    return redirect("/materials", "素材已删除")


@app.post("/materials/bulk-delete")
def bulk_delete_materials(request: Request, material_ids: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/materials", "没有权限")
        ids = [int(item) for item in material_ids.split(",") if item.strip().isdigit()]
        materials = db.scalars(select(MaterialAsset).where(MaterialAsset.id.in_(ids), MaterialAsset.deleted_at.is_(None))).all()
        for material in materials:
            material.deleted_at = datetime.now().replace(microsecond=0)
            material.deleted_by = current_actor(request, db).id if current_actor(request, db) else None
        db.commit()
    return redirect("/materials", "素材已批量删除")


@app.get("/materials/{material_id}/download")
def download_material(material_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        material = db.get(MaterialAsset, material_id)
        if not material or not material.file_path or not Path(material.file_path).exists():
            return redirect("/materials", "文件不可用")
        return FileResponse(material.file_path, filename=material.file_name or Path(material.file_path).name)


@app.post("/settings/test-data/cleanup")
def cleanup_test_data(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/settings", "没有权限")
        for model, field in [
            (TopicIdea, TopicIdea.title),
            (VideoBreakdown, VideoBreakdown.title),
            (GeneratedReportDraft, GeneratedReportDraft.title),
            (AiReview, AiReview.prompt_type),
        ]:
            items = db.scalars(select(model).where(field.contains("测试"))).all()
            for item in items:
                db.delete(item)
        test_accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.name.contains("测试"))).all()
        test_account_ids = [account.id for account in test_accounts]
        for account in test_accounts:
            account.deleted_at = datetime.now().replace(microsecond=0)
            account.is_active = False
        if test_account_ids:
            db.query(ContentDailyMetric).filter(ContentDailyMetric.account_id.in_(test_account_ids)).delete(synchronize_session=False)
            db.query(DailyAccountMetric).filter(DailyAccountMetric.account_id.in_(test_account_ids)).delete(synchronize_session=False)
        db.query(ContentDailyMetric).filter(ContentDailyMetric.title.contains("测试")).delete(synchronize_session=False)
        materials = db.scalars(select(MaterialAsset).where(MaterialAsset.deleted_at.is_(None), MaterialAsset.name.contains("测试"))).all()
        for material in materials:
            if material.file_path and Path(material.file_path).exists():
                Path(material.file_path).unlink()
            db.delete(material)
        db.commit()
    return redirect("/settings", "测试数据已清理")


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    with SessionLocal() as db:
        try:
            users = db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at)).all()
            trash = db.scalars(select(User).where(User.deleted_at.is_not(None)).order_by(User.deleted_at.desc())).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/users", exc)
            users = []
            trash = []
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "users.html",
            db,
            users=users,
            trash=trash,
            can_manage_members=can(current_actor(request, db), "manage_members"),
            role_options=[
                (ROLE_SUPERADMIN, role_label(ROLE_SUPERADMIN)),
                (ROLE_MEMBER, role_label(ROLE_MEMBER)),
            ],
            message=message,
        )


@app.post("/users")
def add_user(request: Request, username: str = Form(), display_name: str = Form(), password: str = Form(), role: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_members")
        if not actor:
            return redirect("/", "没有权限")
        if not username.strip() or not display_name.strip():
            return redirect("/users", "用户名和显示名称不能为空")
        if len(password) < 8:
            return redirect("/users", "密码至少需要 8 位")
        if db.scalar(select(User).where(User.username == username.strip())):
            return redirect("/users", "用户名已经存在")
        user = User(username=username.strip(), display_name=display_name.strip(), password_hash=hash_password(password), role=normalize_role(role))
        db.add(user)
        log_operation(db, actor, "新增", "成员", user.username, f"角色：{role_label(user.role)}")
        db.commit()
    return redirect("/users", "成员已添加")


@app.post("/users/{user_id}/edit")
def edit_user(
    user_id: int,
    request: Request,
    username: str = Form(),
    display_name: str = Form(),
    role: str = Form(),
    password: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_members")
        if not actor:
            return redirect("/users", "没有权限")
        user = db.get(User, user_id)
        if not user or user.deleted_at:
            return redirect("/users", "成员不存在")
        if not username.strip() or not display_name.strip():
            return redirect("/users", "用户名和显示名称不能为空")
        duplicate = db.scalar(select(User).where(User.id != user_id, User.username == username.strip()))
        if duplicate:
            return redirect("/users", "用户名已经存在")
        user.username = username.strip()
        user.display_name = display_name.strip()
        user.role = normalize_role(role)
        if password.strip():
            if len(password.strip()) < 8:
                return redirect("/users", "初始密码至少需要 8 位")
            user.password_hash = hash_password(password.strip())
        log_operation(db, actor, "编辑", "成员", user.username, f"角色：{role_label(user.role)}")
        db.commit()
    return redirect("/users", "成员已更新")


@app.post("/users/{user_id}/toggle")
def toggle_user(user_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_members")
        if not actor:
            return redirect("/users", "没有权限")
        user = db.get(User, user_id)
        if not user or user.deleted_at:
            return redirect("/users", "成员不存在")
        if user.id == actor.id:
            return redirect("/users", "不能停用当前登录账号")
        user.is_active = not user.is_active
        log_operation(db, actor, "启用" if user.is_active else "停用", "成员", user.username, f"显示名称：{user.display_name}")
        db.commit()
    return redirect("/users", "成员状态已更新")


@app.post("/users/{user_id}/delete")
def delete_user(user_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "delete_data")
        if not actor:
            return redirect("/users", "没有权限")
        user = db.get(User, user_id)
        if not user or user.deleted_at:
            return redirect("/users", "成员不存在")
        if user.id == actor.id:
            return redirect("/users", "不能删除当前登录账号")
        user.deleted_at = datetime.now().replace(microsecond=0)
        user.deleted_by = actor.id
        user.is_active = False
        log_operation(db, actor, "删除", "成员", user.username, f"显示名称：{user.display_name}")
        db.commit()
    return redirect("/users", "成员已删除")


@app.post("/users/{user_id}/restore")
def restore_user(user_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_members")
        if not actor:
            return redirect("/users", "没有权限")
        user = db.get(User, user_id)
        if user and user.deleted_at:
            user.deleted_at = None
            user.deleted_by = None
            user.is_active = True
            db.commit()
    return redirect("/users", "成员已恢复")


@app.post("/users/{user_id}/purge")
def purge_user(user_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "delete_data")
        if not actor:
            return redirect("/users", "没有权限")
        user = db.get(User, user_id)
        if not user:
            return redirect("/users", "成员不存在")
        if user.id == actor.id:
            return redirect("/users", "不能彻底删除当前登录账号")
        db.delete(user)
        db.commit()
    return redirect("/users", "成员已彻底删除")


@app.post("/users/password")
def change_password(request: Request, current_password: str = Form(), new_password: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user or not verify_password(current_password, user.password_hash):
            return redirect("/settings", "当前密码不正确")
        if len(new_password) < 8:
            return redirect("/settings", "新密码至少需要 8 位")
        user.password_hash = hash_password(new_password)
        db.commit()
    return redirect("/settings", "密码已修改")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with SessionLocal() as db:
        try:
            recipients = db.scalars(select(EmailRecipient).order_by(EmailRecipient.created_at)).all()
            runtime = runtime_settings(db)
            hotspot_history = db.scalars(select(HotspotReport).order_by(HotspotReport.report_date.desc()).limit(10)).all()
            api_tail = runtime.openai_api_key[-4:] if runtime.openai_api_key else ""
            config_status = {
                "openai": bool(runtime.openai_api_key),
                "smtp": bool(runtime.smtp_host and runtime.smtp_username and runtime.smtp_password),
                "secure": settings.cookie_secure,
            }
            appearance = get_json_setting(db, "appearance", {"theme": "light", "font_scale": "100"})
            business_keywords = get_json_setting(
                db,
                "business_keywords",
                ["无人机足球", "青少年科技体育", "科技特长生", "赛事培训", "安全科普"],
            )
            hotspot_sources = get_json_setting(db, "hotspot_sources", ["抖音", "小红书", "微信视频号"])
            report_schedule = {
                "frequency": get_setting(db, "report_frequency", "daily"),
                "weekday": get_setting(db, "report_weekday", "mon"),
                "monthday": get_setting(db, "report_monthday", "1"),
            }
            recipients_view = []
            for recipient in recipients:
                try:
                    tags = json.loads(recipient.tags_json or "[]")
                except json.JSONDecodeError:
                    tags = []
                recipients_view.append({"recipient": recipient, "tags": tags, "tags_text": ", ".join(tags)})
            diagnostics = diagnostics_status(db)
            upload_status = latest_import_status(db)
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/settings", exc)
            runtime = settings
            hotspot_history = []
            api_tail = ""
            config_status = {"openai": False, "smtp": False, "secure": settings.cookie_secure}
            appearance = {"theme": "light", "font_scale": "100"}
            business_keywords = []
            hotspot_sources = []
            report_schedule = {"frequency": "daily", "weekday": "mon", "monthday": "1"}
            recipients_view = []
            diagnostics = {"site": "error", "database": "disconnected", "openai": "missing", "version": APP_VERSION, "last_error": LAST_RUNTIME_ERROR.get("message", ""), "last_error_time": LAST_RUNTIME_ERROR.get("time", ""), "last_error_path": LAST_RUNTIME_ERROR.get("path", "")}
            upload_status = {"file": "暂无记录", "status": "暂无记录", "time": "", "message": ""}
            message = "数据加载失败，请稍后重试"
        return page(
            request,
            "settings.html",
            db,
            recipients=recipients_view,
            config_status=config_status,
            settings=runtime,
            api_key_preview=("已配置" + (f"（尾号 {api_tail}）" if api_tail else "")) if runtime.openai_api_key else "未配置",
            appearance=appearance,
            business_keywords=", ".join(business_keywords),
            hotspot_sources=", ".join(hotspot_sources),
            report_schedule=report_schedule,
            hotspot_history=hotspot_history,
            diagnostics=diagnostics,
            latest_upload=upload_status,
            message=message,
        )


@app.post("/settings/api")
def save_api_settings(
    request: Request,
    openai_api_key: str = Form(""),
    openai_model: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_settings")
        if not actor:
            return redirect("/settings", "没有权限")
        if openai_api_key.strip():
            set_setting(db, "openai_api_key", openai_api_key.strip(), secret=True)
        set_setting(db, "openai_model", openai_model.strip() or settings.openai_model)
        log_operation(db, actor, "编辑", "API设置", "OpenAI API", "更新密钥或模型")
        db.commit()
    return redirect("/settings", "API 设置已保存")


@app.post("/settings/api/delete")
def delete_api_settings(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_settings")
        if not actor:
            return redirect("/settings", "没有权限")
        set_setting(db, "openai_api_key", "")
        log_operation(db, actor, "删除", "API设置", "OpenAI API", "删除 API 密钥")
        db.commit()
    return redirect("/settings", "API 密钥已删除")


@app.post("/settings/general")
def save_general_settings(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form("465"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    mail_from: str = Form(""),
    mail_from_name: str = Form(""),
    report_hour: str = Form("10"),
    report_minute: str = Form("0"),
    hotspot_hour: str = Form("9"),
    hotspot_minute: str = Form("0"),
    report_frequency: str = Form("daily"),
    report_weekday: str = Form("mon"),
    report_monthday: str = Form("1"),
    business_keywords: str = Form(""),
    hotspot_sources: str = Form(""),
    app_timezone: str = Form("Asia/Shanghai"),
    theme: str = Form("light"),
    font_scale: str = Form("100"),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        actor = require_permission(request, db, "manage_settings")
        if not actor:
            return redirect("/settings", "没有权限")
        set_setting(db, "smtp_host", smtp_host.strip())
        set_setting(db, "smtp_port", smtp_port.strip() or "465")
        set_setting(db, "smtp_username", smtp_username.strip())
        if smtp_password.strip():
            set_setting(db, "smtp_password", smtp_password.strip(), secret=True)
        set_setting(db, "mail_from", mail_from.strip())
        set_setting(db, "mail_from_name", mail_from_name.strip() or "自媒体每日复盘")
        set_setting(db, "report_hour", report_hour.strip())
        set_setting(db, "report_minute", report_minute.strip())
        set_setting(db, "hotspot_hour", hotspot_hour.strip())
        set_setting(db, "hotspot_minute", hotspot_minute.strip())
        set_setting(db, "report_frequency", report_frequency.strip() or "daily")
        set_setting(db, "report_weekday", report_weekday.strip() or "mon")
        set_setting(db, "report_monthday", report_monthday.strip() or "1")
        set_json_setting(db, "business_keywords", get_tags(business_keywords))
        set_json_setting(db, "hotspot_sources", get_tags(hotspot_sources))
        set_setting(db, "app_timezone", app_timezone.strip() or "Asia/Shanghai")
        set_json_setting(db, "appearance", {"theme": theme, "font_scale": font_scale})
        db.commit()
        reload_scheduler()
    return redirect("/settings", "系统设置已保存")


@app.post("/settings/recipients")
def add_recipient(request: Request, name: str = Form(""), email: str = Form(), tags: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_recipients"):
            return redirect("/settings", "只有管理员可以修改收件人")
        if db.scalar(select(EmailRecipient).where(EmailRecipient.email == email.strip().lower())):
            return redirect("/settings", "这个邮箱已经存在")
        db.add(EmailRecipient(name=name.strip(), email=email.strip().lower(), tags_json=json.dumps(get_tags(tags), ensure_ascii=False)))
        db.commit()
    return redirect("/settings", "收件邮箱已添加")


@app.post("/settings/recipients/{recipient_id}/edit")
def edit_recipient(
    recipient_id: int,
    request: Request,
    name: str = Form(""),
    email: str = Form(),
    tags: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_recipients"):
            return redirect("/settings", "没有权限")
        recipient = db.get(EmailRecipient, recipient_id)
        if not recipient:
            return redirect("/settings", "联系人不存在")
        duplicate = db.scalar(select(EmailRecipient).where(EmailRecipient.id != recipient_id, EmailRecipient.email == email.strip().lower()))
        if duplicate:
            return redirect("/settings", "这个邮箱已经存在")
        recipient.name = name.strip()
        recipient.email = email.strip().lower()
        recipient.tags_json = json.dumps(get_tags(tags), ensure_ascii=False)
        db.commit()
    return redirect("/settings", "收件人已更新")


@app.post("/settings/recipients/{recipient_id}/toggle")
def toggle_recipient(recipient_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "manage_recipients"):
            return redirect("/settings", "没有权限")
        recipient = db.get(EmailRecipient, recipient_id)
        if recipient:
            recipient.is_active = not recipient.is_active
            db.commit()
    return redirect("/settings", "收件人状态已更新")


@app.post("/settings/recipients/{recipient_id}/delete")
def delete_recipient(recipient_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/settings", "没有权限")
        recipient = db.get(EmailRecipient, recipient_id)
        if recipient:
            db.delete(recipient)
            db.commit()
    return redirect("/settings", "收件人已删除")


@app.post("/settings/recipients/bulk-delete")
def bulk_delete_recipients(request: Request, recipient_ids: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not require_permission(request, db, "delete_data"):
            return redirect("/settings", "没有权限")
        ids = [int(item) for item in recipient_ids.split(",") if item.strip().isdigit()]
        recipients = db.scalars(select(EmailRecipient).where(EmailRecipient.id.in_(ids))).all()
        for recipient in recipients:
            db.delete(recipient)
        db.commit()
    return redirect("/settings", "已批量删除收件人")


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    with SessionLocal() as db:
        return page(request, "help.html", db)


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, operation_type: str = "", object_type: str = ""):
    with SessionLocal() as db:
        try:
            query = select(OperationLog)
            if operation_type:
                query = query.where(OperationLog.operation_type == operation_type)
            if object_type:
                query = query.where(OperationLog.object_type == object_type)
            logs = db.scalars(query.order_by(OperationLog.created_at.desc()).limit(100)).all()
            message = request.query_params.get("message", "")
        except Exception as exc:
            remember_runtime_error("/logs", exc)
            logs = []
            message = "数据加载失败，请稍后重试"
        return page(request, "logs.html", db, logs=logs, operation_type=operation_type, object_type=object_type, message=message)
