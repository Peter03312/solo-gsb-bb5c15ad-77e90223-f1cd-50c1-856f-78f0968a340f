"""Database engine / session management."""
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://exp:exp@localhost:5432/experiments",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """Create tables and indexes, retrying while Postgres is still starting."""
    from app import models  # noqa: F401  (register models on the metadata)

    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # noqa: BLE001 - any transient DB error
            last_exc = exc
            time.sleep(delay)
    raise RuntimeError(f"database not ready after {retries} attempts: {last_exc}")
