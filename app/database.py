from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import BASE_DIR, settings


raw_database_url = settings.database_url
if raw_database_url.startswith("postgres://"):
    raw_database_url = "postgresql+psycopg://" + raw_database_url.removeprefix("postgres://")
elif raw_database_url.startswith("postgresql://") and "+psycopg" not in raw_database_url:
    raw_database_url = "postgresql+psycopg://" + raw_database_url.removeprefix("postgresql://")

if raw_database_url.startswith("sqlite:///./"):
    relative = raw_database_url.removeprefix("sqlite:///./")
    database_url = f"sqlite:///{BASE_DIR / relative}"
else:
    database_url = raw_database_url

IS_SQLITE = database_url.startswith("sqlite:///")

if IS_SQLITE:
    Path(database_url.removeprefix("sqlite:///")) .parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    database_url,
    connect_args={"check_same_thread": False, "timeout": 30} if IS_SQLITE else {},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record):
    if IS_SQLITE:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _safe_create_all(bind, **kwargs):
    with bind.begin() as conn:
        existing = set(inspect(conn).get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                table.create(conn, checkfirst=True)


Base.metadata.create_all = _safe_create_all


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
