from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_settings
from src.persistence.models import Base


SQLITE_BUSY_TIMEOUT_SEC = 30


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    # SQLite needs check_same_thread=False when shared across threads;
    # PG/MySQL ignore unknown connect_args.
    connect_args: dict = {}
    if settings.DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # 뉴스 수집 job 과 trading tick 이 다른 스레드에서 쓴다 → 잠금 대기를 기본 5초보다 길게
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_SEC
    return create_engine(
        settings.DATABASE_URL,
        connect_args=connect_args,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables. Dev-only; prod uses Alembic migrations (W4+)."""
    Base.metadata.create_all(bind=get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
