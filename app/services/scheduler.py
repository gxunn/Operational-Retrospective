from apscheduler.schedulers.background import BackgroundScheduler

from ..config import settings
from ..database import SessionLocal
from .emailer import send_report_email
from .hotspots import generate_hotspots
from .reporting import generate_report


scheduler = BackgroundScheduler(timezone=settings.app_timezone)


def daily_job() -> None:
    with SessionLocal() as db:
        try:
            report = generate_report(db, use_latest=True)
            if report.emailed_at is None:
                send_report_email(db, report)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"日报任务未完成：{type(exc).__name__}")


def hotspot_job() -> None:
    with SessionLocal() as db:
        try:
            generate_hotspots(db)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"热点任务未完成：{type(exc).__name__}")


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(
        daily_job,
        "cron",
        hour=settings.report_hour,
        minute=settings.report_minute,
        id="daily_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        hotspot_job,
        "cron",
        hour=settings.hotspot_hour,
        minute=settings.hotspot_minute,
        id="daily_hotspots",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
