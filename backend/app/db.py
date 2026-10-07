"""Подключение к PostgreSQL (SQLAlchemy 2 + psycopg3), поддержка ±infinity в timestamptz."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

import psycopg
from psycopg.types.datetime import DatetimeDumper, TimestamptzLoader
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

# Сентинелы для '-infinity' / 'infinity' (первая запись tz_periods)
NEG_INF = datetime.min.replace(tzinfo=timezone.utc)
POS_INF = datetime.max.replace(tzinfo=timezone.utc)


class _InfTzLoader(TimestamptzLoader):
    def load(self, data):
        if data == b"-infinity":
            return NEG_INF
        if data == b"infinity":
            return POS_INF
        return super().load(data)


class _InfTzDumper(DatetimeDumper):
    def dump(self, obj):
        if obj == NEG_INF:
            return b"-infinity"
        if obj == POS_INF:
            return b"infinity"
        return super().dump(obj)


def _setup_connection(dbapi_conn: psycopg.Connection) -> None:
    dbapi_conn.adapters.register_loader("timestamptz", _InfTzLoader)
    dbapi_conn.adapters.register_dumper(datetime, _InfTzDumper)


@lru_cache
def get_engine() -> Engine:
    engine = create_engine(
        get_settings().sqlalchemy_url, pool_size=10, max_overflow=20, pool_pre_ping=True, future=True,
        connect_args={"options": "-c timezone=UTC"}
    )
    event.listen(engine, "connect", lambda c, _r: _setup_connection(c))
    return engine


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def new_session() -> Session:
    return get_sessionmaker()()


def raw_psycopg_connect(autocommit: bool = True) -> psycopg.Connection:
    """Отдельное «сырое» соединение (LISTEN/NOTIFY)."""
    conn = psycopg.connect(get_settings().psycopg_url, autocommit=autocommit, options="-c timezone=UTC")
    _setup_connection(conn)
    return conn


def lock_user(session: Session, user_id: int) -> None:
    """Сериализация записей, меняющих агрегаты пользователя (DESIGN, раздел 1)."""
    session.execute(text("SELECT pg_advisory_xact_lock(:u)"), {"u": user_id})
