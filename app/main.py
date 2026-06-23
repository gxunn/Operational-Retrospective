import json
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import BASE_DIR, settings
from .database import Base, SessionLocal, engine
from .models import (
    ContentDailyMetric,
    DailyAccountMetric,
    EmailRecipient,
    HotspotReport,
    ImportBatch,
    PlatformAccount,
    Report,
    User,
)
from .security import csrf_token, current_user, hash_password, verify_csrf, verify_password
from .services.emailer import send_report_email
from .services.importer import FIELDS, file_sha256, import_batch, preview
from .services.hotspots import generate_hotspots, report_payload
from .services.metrics import comparison_groups, summarize_metrics
from .services.reporting import LABELS, METRICS, generate_report, report_stats
from .services.scheduler import start_scheduler, stop_scheduler


def initialize() -> None:
    Base.metadata.create_all(engine)
    for folder in (BASE_DIR / "uploads", BASE_DIR / "reports", BASE_DIR / "data"):
        folder.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.username == settings.admin_username)):
            db.add(
                User(
                    username=settings.admin_username,
                    display_name="管理员",
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
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
    return templates.TemplateResponse(
        request,
        template,
        {"user": user, "csrf_token": csrf_token(request), "message": request.query_params.get("message", ""), **context},
    )


def admin_user(request: Request, db: Session) -> User | None:
    user = current_user(request, db)
    return user if user and user.role == "admin" else None


@app.get("/health")
def health():
    return {"status": "ok"}


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
        return page(
            request,
            "dashboard.html",
            db,
            target=target,
            stats=stats,
            trends=trends,
            account_rows=account_rows,
            contents=contents,
            report_hour=f"{settings.report_hour:02d}:{settings.report_minute:02d}",
            target_weekday="星期" + "一二三四五六日"[target.weekday()],
        )


@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    with SessionLocal() as db:
        accounts = db.scalars(select(PlatformAccount).order_by(PlatformAccount.platform, PlatformAccount.name)).all()
        return page(request, "accounts.html", db, accounts=accounts, platforms=["抖音", "小红书", "视频号", "公众号", "B站", "其他"])


@app.post("/accounts")
def add_account(request: Request, platform: str = Form(), name: str = Form(), external_id: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/accounts", "只有管理员可以新增账号")
        if db.scalar(select(PlatformAccount).where(PlatformAccount.platform == platform, PlatformAccount.name == name.strip())):
            return redirect("/accounts", "这个平台账号已经存在")
        db.add(PlatformAccount(platform=platform, name=name.strip(), external_id=external_id.strip()))
        db.commit()
    return redirect("/accounts", "账号已添加")


@app.post("/accounts/{account_id}/toggle")
def toggle_account(account_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/accounts", "只有管理员可以修改账号")
        account = db.get(PlatformAccount, account_id)
        if account:
            account.is_active = not account.is_active
            db.commit()
    return redirect("/accounts", "账号状态已更新")


@app.get("/imports/upload", response_class=HTMLResponse)
def upload_page(request: Request):
    with SessionLocal() as db:
        accounts = db.scalars(select(PlatformAccount).where(PlatformAccount.is_active.is_(True)).order_by(PlatformAccount.platform)).all()
        return page(request, "upload.html", db, accounts=accounts, max_mb=settings.max_upload_mb)


@app.post("/imports/upload")
async def upload_file(request: Request, account_id: int = Form(), file: UploadFile = File(), csrf: str = Form()):
    verify_csrf(request, csrf)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        return redirect("/imports/upload", "文件格式不支持，请上传 CSV、XLSX 或 XLS")
    with SessionLocal() as db:
        user = current_user(request, db)
        account = db.get(PlatformAccount, account_id)
        if not user or not account:
            return redirect("/imports/upload", "请选择有效的平台账号")
        stored = BASE_DIR / "uploads" / f"{uuid.uuid4().hex}{suffix}"
        with stored.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        if stored.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            stored.unlink(missing_ok=True)
            return redirect("/imports/upload", f"文件不能超过 {settings.max_upload_mb}MB")
        batch = ImportBatch(
            account_id=account.id,
            uploaded_by=user.id,
            original_filename=Path(file.filename or "data").name,
            stored_path=str(stored),
            file_hash=file_sha256(stored),
        )
        db.add(batch)
        db.flush()
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
def imports_page(request: Request):
    with SessionLocal() as db:
        batches = db.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)).all()
        return page(request, "imports.html", db, batches=batches)


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
        )


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    with SessionLocal() as db:
        reports = db.scalars(select(Report).order_by(Report.report_date.desc())).all()
        return page(request, "reports.html", db, reports=reports)


@app.get("/hotspots", response_class=HTMLResponse)
def hotspots_page(request: Request):
    with SessionLocal() as db:
        report = db.scalar(select(HotspotReport).order_by(HotspotReport.report_date.desc()))
        return page(request, "hotspots.html", db, report=report, payload=report_payload(report), settings=settings)


@app.post("/hotspots/refresh")
def refresh_hotspots(request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = generate_hotspots(db)
        db.commit()
        if report.status == "failed":
            return redirect("/hotspots", report.error_message)
        message = "热点已更新" if not report.error_message else report.error_message
        return redirect("/hotspots", message)


@app.post("/reports/generate")
def create_report(request: Request, report_date: str = Form(""), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        try:
            target = date.fromisoformat(report_date) if report_date else None
            report = generate_report(db, target=target, use_latest=not bool(target))
            db.commit()
            return redirect(f"/reports/{report.id}", "日报已生成")
        except Exception as exc:
            db.rollback()
            return redirect("/reports", f"生成失败：{exc}")


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request):
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
        return page(request, "report_detail.html", db, report=report)


@app.get("/reports/{report_id}/markdown")
def download_markdown(report_id: int, request: Request):
    with SessionLocal() as db:
        if not current_user(request, db):
            return redirect("/auth/login")
        report = db.get(Report, report_id)
        if not report:
            return redirect("/reports", "报告不存在")
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
        return FileResponse(report.pdf_path, filename=f"daily-{report.report_date}.pdf", media_type="application/pdf")


@app.post("/reports/{report_id}/email")
def email_report(report_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect(f"/reports/{report_id}", "只有管理员可以发送邮件")
        report = db.get(Report, report_id)
        try:
            count = send_report_email(db, report)
            db.commit()
            return redirect(f"/reports/{report_id}", f"已发送给 {count} 位收件人")
        except Exception as exc:
            db.rollback()
            return redirect(f"/reports/{report_id}", f"发送失败：{exc}")


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/", "只有管理员可以管理团队")
        users = db.scalars(select(User).order_by(User.created_at)).all()
        return page(request, "users.html", db, users=users)


@app.post("/users")
def add_user(request: Request, username: str = Form(), display_name: str = Form(), password: str = Form(), role: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/", "没有权限")
        if len(password) < 8:
            return redirect("/users", "密码至少需要 8 位")
        if db.scalar(select(User).where(User.username == username.strip())):
            return redirect("/users", "用户名已经存在")
        db.add(User(username=username.strip(), display_name=display_name.strip(), password_hash=hash_password(password), role=role))
        db.commit()
    return redirect("/users", "成员已添加")


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
        config_status = {
            "openai": bool(settings.openai_api_key),
            "smtp": bool(settings.smtp_host and settings.smtp_username and settings.smtp_password),
            "secure": settings.cookie_secure,
        }
        return page(request, "settings.html", db, recipients=recipients, config_status=config_status, settings=settings)


@app.post("/settings/recipients")
def add_recipient(request: Request, name: str = Form(""), email: str = Form(), csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/settings", "只有管理员可以修改收件人")
        if db.scalar(select(EmailRecipient).where(EmailRecipient.email == email.strip().lower())):
            return redirect("/settings", "这个邮箱已经存在")
        db.add(EmailRecipient(name=name.strip(), email=email.strip().lower()))
        db.commit()
    return redirect("/settings", "收件邮箱已添加")


@app.post("/settings/recipients/{recipient_id}/toggle")
def toggle_recipient(recipient_id: int, request: Request, csrf: str = Form()):
    verify_csrf(request, csrf)
    with SessionLocal() as db:
        if not admin_user(request, db):
            return redirect("/settings", "没有权限")
        recipient = db.get(EmailRecipient, recipient_id)
        if recipient:
            recipient.is_active = not recipient.is_active
            db.commit()
    return redirect("/settings", "收件人状态已更新")
