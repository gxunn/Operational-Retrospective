from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from .emailer import send_report_email
from .hotspots import generate_hotspots
from .reporting import generate_report
from .runtime import get_setting, runtime_settings


scheduler = BackgroundScheduler()


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


def configure_scheduler() -> None:
    with SessionLocal() as db:
        runtime = runtime_settings(db)
        report_frequency = get_setting(db, "report_frequency", "daily")
        report_weekday = get_setting(db, "report_weekday", "mon")
        report_monthday = int(get_setting(db, "report_monthday", "1"))
        scheduler.configure(timezone=runtime.app_timezone)
        scheduler.add_job(
            hotspot_job,
            "cron",
            hour=runtime.hotspot_hour,
            minute=runtime.hotspot_minute,
            id="daily_hotspots",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        trigger_kwargs = {
            "hour": runtime.report_hour,
            "minute": runtime.report_minute,
            "id": "scheduled_report",
            "replace_existing": True,
            "max_instances": 1,
            "coalesce": True,
        }
        if report_frequency == "weekly":
            scheduler.add_job(daily_job, "cron", day_of_week=report_weekday, **trigger_kwargs)
        elif report_frequency == "monthly":
            scheduler.add_job(daily_job, "cron", day=report_monthday, **trigger_kwargs)
        else:
            scheduler.add_job(daily_job, "cron", **trigger_kwargs)


def start_scheduler() -> None:
    if scheduler.running:
        return
    configure_scheduler()
    scheduler.start()


def reload_scheduler() -> None:
    if scheduler.running:
        scheduler.remove_all_jobs()
        configure_scheduler()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
