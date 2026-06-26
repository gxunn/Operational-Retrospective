import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import markdown as md
from openai import OpenAI
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.orm import Session
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
from .services.metrics import comparison_groups, summarize_metrics
from .services.reporting import LABELS, METRICS, generate_report, generate_summary_report, report_stats
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


def initialize() -> None:
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())

        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                table.create(conn, checkfirst=True)
        tables = set(inspect(conn).get_table_names())

        def add_column(table: str, name: str, ddl: str) -> None:
            if table not in tables:
                return
            columns = {column["name"] for column in inspect(conn).get_columns(table)}
            if name not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        add_column("users", "deleted_at", "deleted_at DATETIME")
        add_column("users", "deleted_by", "deleted_by INTEGER")
        add_column("users", "updated_at", "updated_at DATETIME")
        add_column("platform_accounts", "last_synced_at", "last_synced_at DATETIME")
        add_column("platform_accounts", "deleted_at", "deleted_at DATETIME")
        add_column("platform_accounts", "deleted_by", "deleted_by INTEGER")
        add_column("platform_accounts", "manager_name", "manager_name VARCHAR(80) DEFAULT ''")
        add_column("platform_accounts", "business_type", "business_type VARCHAR(80) DEFAULT ''")
        add_column("platform_accounts", "positioning", "positioning VARCHAR(255) DEFAULT ''")
        add_column("platform_accounts", "data_source", "data_source VARCHAR(30) DEFAULT 'manual'")
        add_column("platform_accounts", "updated_at", "updated_at DATETIME")
        add_column("import_batches", "deleted_at", "deleted_at DATETIME")
        add_column("import_batches", "deleted_by", "deleted_by INTEGER")
        add_column("import_batches", "updated_at", "updated_at DATETIME")
        add_column("reports", "title", "title VARCHAR(200) DEFAULT ''")
        add_column("reports", "report_type", "report_type VARCHAR(20) DEFAULT 'daily'")
        add_column("reports", "deleted_at", "deleted_at DATETIME")
        add_column("reports", "deleted_by", "deleted_by INTEGER")
        add_column("topic_ideas", "is_favorite", "is_favorite BOOLEAN DEFAULT 0")
        add_column("topic_ideas", "updated_at", "updated_at DATETIME")
        add_column("topic_ideas", "status", "status VARCHAR(20) DEFAULT '待拍摄'")
        add_column("topic_ideas", "owner_name", "owner_name VARCHAR(80) DEFAULT ''")
        add_column("email_recipients", "tags_json", "tags_json TEXT DEFAULT '[]'")
        add_column("content_daily_metrics", "private_messages", "private_messages FLOAT DEFAULT 0")
        add_column("content_daily_metrics", "conversion_note", "conversion_note TEXT DEFAULT ''")
    storage_dir = settings.storage_dir
    for folder in (storage_dir, storage_dir / "uploads", storage_dir / "reports"):
        folder.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.username == settings.admin_username)):
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


def suggest_material_tags(*parts: str) -> list[str]:
    text = " ".join(part for part in parts if part)
    candidates = ["张家界", "无人机足球", "研学", "招生", "证书", "端午", "活动", "海报", "合同", "课程", "案例"]
    tags = [item for item in candidates if item in text]
    if not tags and text:
        tags = [part.strip() for part in text.replace("，", ",").split(",") if part.strip()][:3]
    return list(dict.fromkeys(tags))[:8]



@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/assistant/ask")
async def ask_assistant(request: Request):
    with SessionLocal() as db:
        user = current_user(request, db)
        if not user:
            return JSONResponse({"error": "请先登录"}, status_code=401)
        runtime = runtime_settings(db)
        if not runtime.openai_api_key:
            return JSONResponse({"answer": "系统还没有配置 OpenAI API Key，暂时无法使用 AI 助手。"})
        payload = await request.json()
        question = str(payload.get("question", "")).strip()
        if not question:
            return JSONResponse({"answer": "请输入你想问的问题。"}, status_code=400)
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
            return JSONResponse({"answer": response.output_text.strip() or "暂时没有可返回的内容。"})
        except Exception as exc:
            return JSONResponse({"answer": f"AI 助手暂时不可用：{type(exc).__name__}"})


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
        latest = db.scalar(select(func.max(DailyAccountMetric.metric_date)))
        target = latest or (date.today() - timedelta(days=1))
        stats = report_stats(db, target)
        days = [target - timedelta(days=offset) for offset in range(6, -1, -1)]
        trends = [{"date": day.strftime("%-m/%-d"), "views": report_stats(db, day)["current"]["views"]} for day in days]
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
            interactions = (metric.likes + metric.comments + metric.favorites + metric.shares) if metric else 0
            previous_interactions = (
                previous_metric.likes + previous_metric.comments + previous_metric.favorites + previous_metric.shares
            ) if previous_metric else 0
            rate = interactions / metric.views if metric and metric.views else 0
            previous_rate = previous_interactions / previous_metric.views if previous_metric and previous_metric.views else 0
            follower_change = ((metric.followers_new - previous_metric.followers_new) / previous_metric.followers_new * 100) if metric and previous_metric and previous_metric.followers_new else None
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
        views_series = [(row.metric_date, row.views) for row in history_rows]
        followers_series = [(row.metric_date, row.followers_new) for row in history_rows]
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
            anomalies=detect_anomalies(db, target),
            forecast_views=forecast_trend(views_series),
            forecast_followers=forecast_trend(followers_series),
        )


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, q: str = "", platform: str = ""):
    with SessionLocal() as db:
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
        return page(
            request,
            "accounts.html",
            db,
            accounts=accounts,
            platforms=account_platform_choices(db),
            search=q,
            selected_platform=platform,
            can_manage_accounts=can(current_actor(request, db), "manage_accounts"),
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
        accounts = db.scalars(
            select(PlatformAccount).where(PlatformAccount.is_active.is_(True), PlatformAccount.deleted_at.is_(None)).order_by(PlatformAccount.platform)
        ).all()
        return page(request, "upload.html", db, accounts=accounts, max_mb=runtime_settings(db).max_upload_mb)


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
            return JSONResponse({"error": "没有权限"}, status_code=403)
        form = await request.form()
        verify_csrf(request, str(form.get("csrf", "")))
        account_id = int(str(form.get("account_id", "0")) or "0")
        account = db.get(PlatformAccount, account_id)
        if not account or account.deleted_at:
            return JSONResponse({"error": "请选择有效的平台账号"}, status_code=400)
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
                batch.status = "failed"
                batch.error_message = str(exc)
                results.append({"name": file.filename, "status": "failed", "message": str(exc)})
        db.commit()
        return JSONResponse({"results": results, "message": "批量上传已完成"})


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

        accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
        platforms = sorted({account.platform for account in accounts})
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
            anomalies=detect_anomalies(db, compare_target),
            forecast_views=forecast_trend([(row.metric_date, row.views) for row in rows if row.metric_date >= range_end - timedelta(days=14)]),
            saved_views=saved_view_links,
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
        reports = db.scalars(select(Report).where(Report.deleted_at.is_(None)).order_by(Report.report_date.desc())).all()
        summary_reports = db.scalars(select(SummaryReport).where(SummaryReport.deleted_at.is_(None)).order_by(SummaryReport.end_date.desc())).all()
        return page(request, "reports.html", db, reports=reports, summary_reports=summary_reports)


@app.get("/hotspots", response_class=HTMLResponse)
def hotspots_page(request: Request):
    with SessionLocal() as db:
        report = db.scalar(select(HotspotReport).order_by(HotspotReport.report_date.desc()))
        return page(request, "hotspots.html", db, report=report, payload=report_payload(report))


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
    return redirect("/report-builder", "AI分析已合并到生成报告")


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
    return redirect("/report-builder", "AI分析已合并到生成报告")


@app.get("/report-builder", response_class=HTMLResponse)
def report_builder_page(request: Request):
    with SessionLocal() as db:
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.deleted_at.is_(None), PlatformAccount.is_active.is_(True)).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
        drafts = db.scalars(select(GeneratedReportDraft).order_by(GeneratedReportDraft.created_at.desc()).limit(8)).all()
        ai_reviews = db.scalars(select(AiReview).order_by(AiReview.created_at.desc()).limit(8)).all()
        return page(request, "report_builder.html", db, accounts=accounts, drafts=drafts, ai_reviews=ai_reviews, draft=None)


@app.post("/report-builder/generate")
def generate_report_builder(
    request: Request,
    report_kind: str = Form("weekly"),
    range_type: str = Form("7d"),
    start_date: str = Form(""),
    end_date: str = Form(""),
    platform: str = Form(""),
    account_id: int = Form(0),
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
            },
            report_kind,
        )
        title = f"{_report_kind_label(report_kind)} · {result['start_date']} 至 {result['end_date']}"
        sections = [
            f"# {title}",
            "",
            "## 本周/本月运营概况" if report_kind in {"weekly", "monthly"} else "## 项目/活动概况",
            result["markdown"],
            "",
            "## PPT 大纲",
            "1. 数据概览",
            "2. 核心变化",
            "3. 问题与风险",
            "4. 下阶段计划",
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
            ppt_outline="1. 数据概览\n2. 核心变化\n3. 问题与风险\n4. 下阶段计划",
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
        topics = db.scalars(select(TopicIdea).order_by(TopicIdea.created_at.desc()).limit(40)).all()
        return page(
            request,
            "topic_center.html",
            db,
            topics=topics,
            keyword=keyword,
            business=business,
            topic_status_options=_topic_status_options(),
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


@app.get("/breakdown", response_class=HTMLResponse)
def breakdown_page(request: Request, q: str = "", platform: str = ""):
    with SessionLocal() as db:
        query = select(VideoBreakdown)
        if q:
            needle = q.strip()
            query = query.where(or_(VideoBreakdown.title.contains(needle), VideoBreakdown.platform.contains(needle), VideoBreakdown.cover_description.contains(needle)))
        if platform:
            query = query.where(VideoBreakdown.platform == platform)
        cases = db.scalars(query.order_by(VideoBreakdown.created_at.desc()).limit(50)).all()
        total_cases = len(cases)
        avg_views = sum(case.views for case in cases) / total_cases if total_cases else 0
        progress = min(100, 18 + total_cases * 8)
        analysis_status = "已完成" if total_cases else "等待首条拆解"
        eta = "预计 8 分钟" if total_cases else "预计 1 分钟"
        recent_cases = cases[:5]
        hot_rankings = sorted(cases, key=lambda item: item.views or 0, reverse=True)[:5]
        selected_platform = platform if platform in {"抖音", "小红书", "视频号", "公众号", "其他"} else ""
        report = cases[0] if cases else None
        return page(
            request,
            "breakdown.html",
            db,
            cases=cases,
            recent_cases=recent_cases,
            hot_rankings=hot_rankings,
            report=report,
            report_data=safe_json_loads(report.analysis_json, {}) if report else {},
            q=q,
            selected_platform=selected_platform,
            breakdown_stats={
                "total_cases": total_cases,
                "avg_views": avg_views,
                "progress": progress,
                "analysis_status": analysis_status,
                "eta": eta,
            },
        )


@app.post("/breakdown/generate")
def generate_breakdown(
    request: Request,
    source_url: str = Form(""),
    title: str = Form(""),
    platform: str = Form(""),
    views: float = Form(0),
    likes: float = Form(0),
    comments: float = Form(0),
    duration: str = Form(""),
    cover_description: str = Form(""),
    script_content: str = Form(""),
    csrf: str = Form(),
):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not can(current_actor(request, db), "use_breakdown"):
            return redirect("/breakdown", "没有权限")
        def clean_text(value: str) -> str:
            return "".join(ch for ch in str(value).replace("\x00", "") if ch == "\n" or ch == "\t" or ord(ch) >= 32).strip()

        source_url = clean_text(source_url)
        title = clean_text(title)
        platform = clean_text(platform)
        duration = clean_text(duration)
        cover_description = clean_text(cover_description)
        script_content = clean_text(script_content)
        ratio = ((likes + comments) / views * 100) if views else 0
        analysis = {
            "title_structure": [
                "核心词前置，利益点第一句出现。",
                "标题尽量保留明确的人群或场景。",
            ],
            "hook": [
                "前 3 秒先给结果，再补过程。",
                "画面和口播同时给出强信息。",
            ],
            "rhythm": [
                "前段提速，中段解释，结尾收束。",
                "每 5 到 7 秒推动一次信息变化。",
            ],
            "selling_point": [
                f"当前互动率约 {ratio:.2f}%。",
                "高互动内容通常围绕明确场景和具体结果展开。",
            ],
            "conversion": [
                "评论区和私信入口要给出明确动作。",
                "结尾加入下一步动作，不要只停留在展示。",
            ],
            "template": "痛点 + 场景 + 结果 + 行动指令",
            "extension_topics": [
                "同主题换人群",
                "同场景换结果",
                "同结果换表达方式",
            ],
        }
        markdown = "\n".join(
            [
                f"# {title or '爆款拆解'}",
                "",
                "## 标题结构",
                f"- 当前标题：{title or '未填写'}",
                "- 核心词靠前，利益点前置。",
                "",
                "## 前3秒钩子",
                "- 先抛结果，再给原因。",
                "- 画面与口播同时给到强信息。",
                "",
                "## 情绪节奏",
                "- 开头提速，中段解释，结尾收束。",
                "",
                "## 卖点表达",
                f"- 当前互动率约 {ratio:.2f}%。",
                "",
                "## 转化设计",
                "- 在评论区和私信入口给明确动作。",
                "",
                "## 可复用模板",
                "- 痛点 + 场景 + 结果 + 行动指令",
                "",
                "## 可延伸选题",
                "- 同主题换人群、换场景、换结果。",
            ]
        )
        case = VideoBreakdown(
            source_url=source_url.strip(),
            title=title.strip(),
            platform=platform.strip(),
            views=views,
            likes=likes,
            comments=comments,
            duration=duration.strip(),
            cover_description=cover_description.strip(),
            script_content=script_content.strip(),
            analysis_json=json.dumps({**analysis, "markdown": markdown, "ratio": ratio}, ensure_ascii=False),
        )
        db.add(case)
        db.commit()
        cases = db.scalars(select(VideoBreakdown).order_by(VideoBreakdown.created_at.desc()).limit(50)).all()
        report_data = safe_json_loads(case.analysis_json, {})
        recent_cases = cases[:5]
        hot_rankings = sorted(cases, key=lambda item: item.views or 0, reverse=True)[:5]
        return page(
            request,
            "breakdown.html",
            db,
            cases=cases,
            recent_cases=recent_cases,
            hot_rankings=hot_rankings,
            report=case,
            report_data=report_data,
            breakdown_stats={
                "total_cases": len(cases),
                "avg_views": sum(item.views for item in cases) / len(cases) if cases else 0,
                "progress": min(100, 18 + len(cases) * 8),
                "analysis_status": "已完成",
                "eta": "预计 8 分钟",
            },
            message="爆款拆解已保存到案例库",
        )


@app.get("/materials", response_class=HTMLResponse)
def materials_page(request: Request, q: str = "", tag: str = ""):
    with SessionLocal() as db:
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
        return page(request, "materials.html", db, materials=material_rows, q=q, tag=tag)


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
        users = db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at)).all()
        trash = db.scalars(select(User).where(User.deleted_at.is_not(None)).order_by(User.deleted_at.desc())).all()
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
        recipients = db.scalars(select(EmailRecipient).order_by(EmailRecipient.created_at)).all()
        runtime = runtime_settings(db)
        hotspot_history = db.scalars(select(HotspotReport).order_by(HotspotReport.report_date.desc()).limit(10)).all()
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
        return page(
            request,
            "settings.html",
            db,
            recipients=recipients_view,
            config_status=config_status,
            settings=runtime,
            appearance=appearance,
            business_keywords=", ".join(business_keywords),
            hotspot_sources=", ".join(hotspot_sources),
            report_schedule=report_schedule,
            hotspot_history=hotspot_history,
        )


@app.post("/settings/general")
def save_general_settings(
    request: Request,
    openai_api_key: str = Form(""),
    openai_model: str = Form(""),
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
        if not require_permission(request, db, "manage_settings"):
            return redirect("/settings", "没有权限")
        if openai_api_key.strip():
            set_setting(db, "openai_api_key", openai_api_key.strip(), secret=True)
        set_setting(db, "openai_model", openai_model.strip() or settings.openai_model)
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


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, operation_type: str = "", object_type: str = ""):
    with SessionLocal() as db:
        query = select(OperationLog)
        if operation_type:
            query = query.where(OperationLog.operation_type == operation_type)
        if object_type:
            query = query.where(OperationLog.object_type == object_type)
        logs = db.scalars(query.order_by(OperationLog.created_at.desc()).limit(100)).all()
        return page(request, "logs.html", db, logs=logs, operation_type=operation_type, object_type=object_type)
