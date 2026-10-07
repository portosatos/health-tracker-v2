"""Очередь заданий на таблице jobs (DESIGN, разделы 1, 3, 11)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import clock


def enqueue(
    session: Session,
    kind: str,
    user_id: int | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_at: datetime | None = None,
) -> int | None:
    """Ставит задание. При совпадении dedupe_key с уже ожидающим — схлопывается (None)."""
    row = session.execute(
        text(
            """INSERT INTO jobs (kind, user_id, payload, dedupe_key, run_at)
               VALUES (:k, :u, CAST(:p AS jsonb), :d, coalesce(:r, now()))
               ON CONFLICT (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL DO NOTHING
               RETURNING id"""
        ),
        {"k": kind, "u": user_id, "p": json.dumps(payload or {}), "d": dedupe_key, "r": run_at},
    ).scalar_one_or_none()
    session.execute(text("SELECT pg_notify('ht_jobs', :k)"), {"k": kind})
    return row


def requeue_stuck(session: Session, stale_seconds: int = 120) -> int:
    r = session.execute(
        text(
            """UPDATE jobs SET status='queued', locked_by=NULL
               WHERE status='running' AND heartbeat_at < now() - make_interval(secs => :s)"""
        ),
        {"s": stale_seconds},
    )
    return r.rowcount


def pick_job(session: Session, worker_id: str) -> dict | None:
    """Берёт одно задание (SKIP LOCKED) и помечает running. Файлы одного пользователя — по очереди."""
    session.execute(text("SELECT pg_advisory_xact_lock(777000001)"))  # сериализует только выбор
    row = session.execute(
        text(
            """SELECT j.id FROM jobs j
               WHERE j.status = 'queued' AND j.run_at <= now()
                 AND NOT (j.kind = 'process_upload' AND EXISTS (
                      SELECT 1 FROM jobs r WHERE r.kind = 'process_upload'
                        AND r.user_id = j.user_id AND r.status = 'running'))
               ORDER BY j.run_at, j.id
               FOR UPDATE OF j SKIP LOCKED LIMIT 1"""
        )
    ).first()
    if row is None:
        return None
    job = session.execute(
        text(
            """UPDATE jobs SET status='running', locked_by=:w, locked_at=now(), heartbeat_at=now(),
                      attempts = attempts + 1
               WHERE id = :id
               RETURNING id, kind, user_id, payload, attempts, max_attempts"""
        ),
        {"w": worker_id, "id": row[0]},
    ).mappings().one()
    return dict(job)


def heartbeat(session: Session, job_id: int) -> None:
    session.execute(text("UPDATE jobs SET heartbeat_at = now() WHERE id = :id"), {"id": job_id})


def finish_job(session: Session, job_id: int) -> None:
    session.execute(text("UPDATE jobs SET status='done', last_error=NULL WHERE id=:id"), {"id": job_id})


def fail_job(session: Session, job: dict, error: str) -> None:
    if job["attempts"] >= job["max_attempts"]:
        session.execute(
            text("UPDATE jobs SET status='failed', last_error=:e WHERE id=:id"), {"e": error[:2000], "id": job["id"]}
        )
    else:
        delay = min(2 ** job["attempts"], 60)
        session.execute(
            text(
                """UPDATE jobs SET status='queued', last_error=:e, locked_by=NULL,
                          run_at = now() + make_interval(secs => :d) WHERE id=:id"""
            ),
            {"e": error[:2000], "id": job["id"], "d": delay},
        )


class JobFatal(Exception):
    """Ошибка, после которой повтор задания бессмысленен (задание помечается failed сразу)."""
