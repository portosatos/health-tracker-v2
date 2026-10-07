"""Дайджест: склейка новых рекомендаций в одно письмо (DESIGN, раздел 10)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock, jobs
from ..config import get_settings
from ..db import lock_user
from ..events import publish_event
from . import quiet_hours


def app_url() -> str:
    return os.environ.get("HT_APP_URL", "http://localhost:5173").rstrip("/")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def subject_for(n: int) -> str:
    return f"{n} {_plural(n, 'новая рекомендация', 'новые рекомендации', 'новых рекомендаций')}"


def render(recs: list[dict[str, Any]]) -> tuple[str, str]:
    """(тема, текст письма) по списку рекомендаций {title, body}."""
    link = f"{app_url()}/recommendations"
    lines = [f"Здравствуйте! Новых рекомендаций: {len(recs)}.", ""]
    for i, r in enumerate(recs, 1):
        lines += [f"{i}. {r['title']}", f"   {r['body']}", ""]
    lines += [f"Открыть в приложении: {link}", "", "Трекер здоровья"]
    return subject_for(len(recs)), "\n".join(lines)


def schedule_flush(session: Session, user_id: int, now: datetime | None = None) -> None:
    """Ставит flush_digest через окно склейки; повторные вызовы схлопываются (dedupe_key)."""
    now = now or clock.now()
    run_at = now + timedelta(seconds=get_settings().digest_hold_seconds)
    jobs.enqueue(session, "flush_digest", user_id, {}, dedupe_key=f"digest:{user_id}", run_at=run_at)


def open_recs(session: Session, user_id: int, ids: list[int]) -> list[dict[str, Any]]:
    rows = session.execute(
        text("""SELECT id, title, body FROM recommendations
                WHERE user_id = :u AND id = ANY(:ids) AND status = 'open' ORDER BY opened_at, id"""),
        {"u": user_id, "ids": ids},
    ).mappings().all()
    return [dict(r) for r in rows]


def flush_digest(session: Session, job: dict) -> None:
    """Job flush_digest: все ещё открытые рекомендации из digest_pending -> одно письмо в outbox."""
    uid = job["user_id"]
    lock_user(session, uid)
    now = clock.now()
    pend = [r[0] for r in session.execute(
        text("DELETE FROM digest_pending WHERE user_id = :u RETURNING recommendation_id"), {"u": uid}).all()]
    if not pend:
        return
    # отозванные (не open) отсеиваются; уже вошедшие в какое-либо письмо — не повторяются
    new = session.execute(
        text("""SELECT r.id FROM recommendations r
                WHERE r.user_id = :u AND r.id = ANY(:ids) AND r.status = 'open'
                  AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.user_id = :u AND r.id = ANY(o.recommendation_ids))
                ORDER BY r.opened_at, r.id"""),
        {"u": uid, "ids": pend},
    ).scalars().all()
    if not new:
        return
    st = session.execute(
        text("SELECT email, email_enabled FROM user_settings WHERE user_id = :u"), {"u": uid}).first()
    email, enabled = (st[0], st[1]) if st else (None, False)
    if not email or not enabled:
        oid = session.execute(
            text("""INSERT INTO outbox (user_id, to_addr, subject, body_text, recommendation_ids, status,
                                        send_after, last_error)
                    VALUES (:u, '', :s, '', :ids, 'cancelled', :now, 'no_email') RETURNING id"""),
            {"u": uid, "s": subject_for(len(new)), "ids": list(new), "now": now},
        ).scalar_one()
        publish_event(session, uid, "mail.status", {"outbox_id": oid, "status": "cancelled", "send_after": None})
        return
    existing = session.execute(
        text("SELECT id, recommendation_ids FROM outbox WHERE user_id=:u AND status IN ('pending','failed') "
             "FOR UPDATE"), {"u": uid}).first()
    release = quiet_hours.user_release(session, uid, now)
    send_after = release or now
    reason = "quiet_hours" if release else None
    if existing is not None:
        recs = open_recs(session, uid, list(existing[1]) + list(new))
        subj, body = render(recs)
        session.execute(
            text("""UPDATE outbox SET recommendation_ids = :ids, subject = :s, body_text = :b, to_addr = :to,
                           send_after = CASE WHEN :rel THEN :sa ELSE least(send_after, :sa) END,
                           defer_reason = CASE WHEN :rel THEN 'quiet_hours' ELSE defer_reason END
                    WHERE id = :id"""),
            {"ids": [r["id"] for r in recs], "s": subj, "b": body, "to": email, "rel": release is not None,
             "sa": send_after, "id": existing[0]},
        )
        oid = existing[0]
    else:
        recs = open_recs(session, uid, list(new))
        subj, body = render(recs)
        oid = session.execute(
            text("""INSERT INTO outbox (user_id, to_addr, subject, body_text, recommendation_ids, status,
                                        send_after, defer_reason)
                    VALUES (:u, :to, :s, :b, :ids, 'pending', :sa, :dr) RETURNING id"""),
            {"u": uid, "to": email, "s": subj, "b": body, "ids": [r["id"] for r in recs], "sa": send_after,
             "dr": reason},
        ).scalar_one()
    publish_event(session, uid, "mail.status", {"outbox_id": oid, "status": "pending",
                                                "send_after": send_after.isoformat()})
    jobs.enqueue(session, "send_outbox", uid, {"outbox_id": oid}, dedupe_key=f"send:{oid}")
