"""Общие фикстуры: тестовая БД healthtracker_v2_test, чистая схема на каждый тест, помощники данных."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ВАЖНО: окружение выставляется до импорта app.*
TEST_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ht2:ht2_dev_password@localhost:5432/healthtracker_v2_test")
os.environ["DATABASE_URL"] = TEST_URL
os.environ["HT_ENV"] = "test"
_UPLOADS = tempfile.mkdtemp(prefix="ht_uploads_")
os.environ["UPLOAD_DIR"] = _UPLOADS
os.environ.pop("HT_FAKE_NOW", None)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import worker  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.main import app  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
TABLES = ("users, jobs, events, mail_inbox, worker_heartbeats")


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.upgrade(cfg, "head")
    yield
    shutil.rmtree(_UPLOADS, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_db():
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


def new_client() -> TestClient:
    return TestClient(app, headers={"X-Requested-With": "ht"})


@pytest.fixture
def client() -> TestClient:
    return new_client()


def register(c: TestClient, login: str = "alice", tz: str = "UTC") -> dict:
    r = c.post("/api/auth/register", json={"login": login, "password": "password123", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def user(client):
    register(client)
    return client


def ndjson(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(x) for x in lines) + "\n").encode()


def upload(c: TestClient, lines, filename: str = "t.ndjson", run_worker: bool = True) -> int:
    body = lines if isinstance(lines, bytes) else ndjson(lines)
    r = c.post("/api/uploads", content=body,
               headers={"Content-Type": "application/x-ndjson", "X-Filename": filename})
    assert r.status_code == 202, r.text
    uid = r.json()["upload_id"]
    if run_worker:
        worker.run_until_idle()
    return uid


def run_worker() -> int:
    return worker.run_until_idle()


def sql(query: str, **params):
    with get_engine().begin() as c:
        res = c.execute(text(query), params)
        return res.all() if res.returns_rows else None


# ---------- построители записей (время — строки UTC) ----------

def T(s: str) -> str:
    """'2026-03-02 06:00' -> '2026-03-02T06:00:00Z'"""
    s = s.replace(" ", "T")
    return s + (":00Z" if len(s) == 16 else "Z")


def dev(ext="watch-01", kind="watch", **kw) -> dict:
    return {"v": 1, "type": "device", "device_id": ext, "kind": kind, **kw}


def steps(ext, start, end, count, off=0) -> dict:
    return {"v": 1, "type": "steps", "device_id": ext, "start": T(start), "end": T(end), "count": count,
            "offset_min": off}


def sleep(ext, start, end, stage="asleep", off=0) -> dict:
    return {"v": 1, "type": "sleep", "device_id": ext, "start": T(start), "end": T(end), "stage": stage,
            "offset_min": off}


def hr(ext, ts, bpm, off=0) -> dict:
    return {"v": 1, "type": "heart_rate", "device_id": ext, "ts": T(ts) if len(ts) <= 16 else ts,
            "bpm": bpm, "offset_min": off}


def full_day(ext: str, day: str, count_per_12h: int, off: int = 0) -> list[dict]:
    """12 часов покрытия (08:00-20:00 UTC) => день complete (при поясе UTC)."""
    return [steps(ext, f"{day} 08:00", f"{day} 14:00", count_per_12h // 2, off),
            steps(ext, f"{day} 14:00", f"{day} 20:00", count_per_12h - count_per_12h // 2, off)]


def daily(c: TestClient, d: str) -> dict:
    r = c.get(f"/api/summary/daily?date={d}")
    assert r.status_code == 200, r.text
    return r.json()


def day_after(d: str, n: int = 1) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")
