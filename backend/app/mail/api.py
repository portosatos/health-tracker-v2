"""REST почты: GET /outbox, GET /dev/mailbox, POST /outbox/{id}/retry (префикс /api добавляет main)."""
from __future__ import annotations

import email
import email.policy

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock, jobs
from ..api.deps import ApiError, CurrentUser, current_user, get_db
from ..config import get_settings
from . import quiet_hours
from .sender import _event

router = APIRouter(tags=["mail"])


def _item(r) -> dict:
    return {
        "id": r["id"], "subject": r["subject"], "status": r["status"],
        "send_after": r["send_after"], "defer_reason": r["defer_reason"], "attempts": r["attempts"],
        "sent_at": r["sent_at"], "recommendation_ids": list(r["recommendation_ids"]),
        "last_error": r["last_error"], "body_text": r["body_text"],
        "next_attempt_at": r["next_attempt_at"],
    }


@router.get("/outbox")
def list_outbox(limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT * FROM outbox WHERE user_id=:u ORDER BY id DESC LIMIT :l"), {"u": user.id, "l": limit}
    ).mappings().all()
    return [_item(r) for r in rows]


@router.post("/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT * FROM outbox WHERE id=:id AND user_id=:u FOR UPDATE"), {"id": outbox_id, "u": user.id}
    ).mappings().first()
    if row is None:
        raise ApiError(404, "not_found", "Письмо не найдено")
    if row["status"] not in ("failed", "dead"):
        raise ApiError(409, "conflict", "Повторить можно только письмо со статусом failed или dead")
    if row["status"] == "dead" and db.execute(
        text("SELECT 1 FROM outbox WHERE user_id=:u AND status IN ('pending','failed')"), {"u": user.id}
    ).first():
        raise ApiError(409, "conflict", "У пользователя уже есть неотправленное письмо")
    now = clock.now()
    release = quiet_hours.user_release(db, user.id, now)
    db.execute(
        text("""UPDATE outbox SET status='pending', next_attempt_at=NULL, locked_at=NULL, attempts=0,
                       send_after=:sa, defer_reason=:dr WHERE id=:id"""),
        {"sa": release or now, "dr": "quiet_hours" if release else None, "id": outbox_id})
    _event(db, user.id, outbox_id, "pending", release or now)
    jobs.enqueue(db, "send_outbox", user.id, {"outbox_id": outbox_id}, dedupe_key=f"send:{outbox_id}")
    db.commit()
    row = db.execute(text("SELECT * FROM outbox WHERE id=:id"), {"id": outbox_id}).mappings().one()
    return _item(row)


def _body_text(raw: str) -> str:
    try:
        msg = email.message_from_bytes(raw.encode("utf-8"), policy=email.policy.default)
        part = msg.get_body(preferencelist=("plain", "html"))
        return part.get_content() if part is not None else ""
    except Exception:  # noqa: BLE001
        return raw


@router.get("/dev/mailbox")
def dev_mailbox(limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_db)):
    if get_settings().ht_env not in ("dev", "test"):
        raise ApiError(404, "not_found", "Недоступно в этом окружении")
    em = db.execute(text("SELECT email FROM user_settings WHERE user_id=:u"), {"u": user.id}).scalar()
    if not em:
        return []
    rows = db.execute(
        text("""SELECT id, subject, rcpt, received_at, raw FROM mail_inbox
                WHERE lower(rcpt) = lower(:e) OR lower(rcpt) LIKE '%' || lower(:e) || '%'
                ORDER BY id DESC LIMIT :l"""), {"e": em, "l": limit}).mappings().all()
    return [{"id": r["id"], "subject": r["subject"], "rcpt": r["rcpt"], "received_at": r["received_at"],
             "body_text": _body_text(r["raw"])} for r in rows]
