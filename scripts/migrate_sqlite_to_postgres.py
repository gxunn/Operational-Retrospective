from __future__ import annotations

import os
from collections.abc import Iterable

from sqlalchemy import MetaData, create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine

from app.models import (
    AiReview,
    AppSetting,
    ContentDailyMetric,
    DailyAccountMetric,
    EmailRecipient,
    GeneratedReportDraft,
    HotspotReport,
    ImportBatch,
    MaterialAsset,
    OperationLog,
    PlatformAccount,
    Report,
    SavedView,
    SummaryReport,
    TopicIdea,
    User,
    VideoBreakdown,
)


TABLE_ORDER = [
    User.__table__,
    PlatformAccount.__table__,
    AppSetting.__table__,
    EmailRecipient.__table__,
    SavedView.__table__,
    TopicIdea.__table__,
    MaterialAsset.__table__,
    ImportBatch.__table__,
    DailyAccountMetric.__table__,
    ContentDailyMetric.__table__,
    Report.__table__,
    SummaryReport.__table__,
    HotspotReport.__table__,
    GeneratedReportDraft.__table__,
    AiReview.__table__,
    VideoBreakdown.__table__,
    OperationLog.__table__,
]


def load_urls() -> tuple[str, str]:
    source = os.getenv("SOURCE_DATABASE_URL", "sqlite:///./data/app.db")
    target = os.getenv("TARGET_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    if not target:
        raise SystemExit("请设置 TARGET_DATABASE_URL 或 DATABASE_URL 指向目标 Postgres。")
    if target.startswith("postgres://"):
        target = "postgresql+psycopg://" + target.removeprefix("postgres://")
    elif target.startswith("postgresql://") and "+psycopg" not in target:
        target = "postgresql+psycopg://" + target.removeprefix("postgresql://")
    if target.startswith("sqlite"):
        raise SystemExit("目标数据库必须是 Postgres，当前 TARGET_DATABASE_URL/DATABASE_URL 仍然是 SQLite。")
    return source, target


def reflect_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


def ensure_target_empty(engine: Engine) -> None:
    inspector = inspect(engine)
    for table in TABLE_ORDER:
        if table.name not in inspector.get_table_names():
            continue
        with engine.begin() as conn:
            count = conn.scalar(select(func.count()).select_from(table))
        if count:
            raise SystemExit(f"目标数据库的表 {table.name} 已有 {count} 条数据。为避免覆盖，迁移已停止。")


def copy_rows(source_engine: Engine, target_engine: Engine) -> None:
    source_meta = MetaData()
    source_meta.reflect(bind=source_engine)
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        for table in TABLE_ORDER:
            source_table = source_meta.tables.get(table.name)
            if source_table is None:
                continue
            rows = [dict(row._mapping) for row in source_conn.execute(select(source_table)).fetchall()]
            if not rows:
                continue
            target_conn.execute(table.insert(), rows)


def sync_postgres_sequences(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table in TABLE_ORDER:
            if "id" not in table.c:
                continue
            conn.execute(
                text(
                    """
                    SELECT setval(
                        pg_get_serial_sequence(:table_name, 'id'),
                        COALESCE((SELECT MAX(id) FROM %s), 1),
                        true
                    )
                    """
                    % table.name
                ),
                {"table_name": table.name},
            )


def main() -> None:
    source_url, target_url = load_urls()
    source_engine = reflect_engine(source_url)
    target_engine = reflect_engine(target_url)

    # 目标库表结构由应用模型定义，迁移前先确保表存在。
    from app.database import Base

    Base.metadata.create_all(target_engine)
    ensure_target_empty(target_engine)
    copy_rows(source_engine, target_engine)
    sync_postgres_sequences(target_engine)
    print("SQLite -> Postgres 数据迁移完成")


if __name__ == "__main__":
    main()
