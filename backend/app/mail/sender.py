"""Отправка писем из outbox через SMTP, повторы, пересчёт тихих часов, tick (DESIGN, раздел 10)."""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock, jobs
from ..config import get_settings
from ..db import lock_user
from ..events import publish_event
from . import digest, quiet_hours

log = logging.getLogger("ht.mail")

RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)]
MAX_ATTEMPTS = 5
STALE_SENDING = timedelta(seconds=120)
MSGID_DOMAIN = "healthtracker.local"
FROM_ADDR = "tracker@healthtracker.local"


def smtp_target() -> tuple[str, int]:
    """Хост/порт SMTP: HT_SMTP_HOST / HT_SMTP_PORT, по умолчанию 127.0.0.1 и SMTP_SINK_PORT из конфига."""
    host = os.environ.get("HT_SMTP_HOST", "127.0.0.1")
    port = int(os.environ.get("HT_SMTP_PORT") or get_settings().smtp_sink_port)
    return host, port


def build_message(row: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = row["to_addr"]
    msg["Subject"] = row["subject"]
    msg["Date"] = format_datetime(clock.now())
    msg["Message-ID"] = f"<{row['id']}@{MSGID_DOMAIN}>"
    msg.set_content(row["body_text"], charset="utf-8")
    return msg


def smtp_send(row: dict) -> None:
    host, port = smtp_target()
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.send_message(build_message(row))


def _event(session: Session, user_id: int, oid: int, status: str, send_after: datetime | None) -> None:
    publish_event(session, user_id, "mail.status", {
        "outbox_id": oid, "status": status, "send_after": send_after.isoformat() if send_after else None})


def _cancel(session: Session, row, reason: str) -> None:
    session.execute(text("UPDATE outbox SET status='cancelled', last_error=:e, locked_at=NULL WHERE id=:id"),
                    {"e": reason, "id": row["id"]})
    _event(session, row["user_id"], row["id"], "cancelled", None)


def send_one(session: Session, outbox_id: int) -> str:
    """Пробует отправить одно письмо. Возвращает итоговый статус или 'skipped'/'deferred'."""
    now = clock.now()
    row = session.execute(
        text("SELECT * FROM outbox WHERE id=:id AND status IN ('pending','failed') FOR UPDATE SKIP LOCKED"),
        {"id": outbox_id},
    ).mappings().first()
    if row is None:
        return "skipped"
    row = dict(row)
    uid = row["user_id"]
    lock_user(session, uid)
    if row["send_after"] > now or (row["next_attempt_at"] and row["next_attempt_at"] > now):
        return "skipped"
    # повторная проверка тихих часов в момент отправки
    release = quiet_hours.user_release(session, uid, now)
    if release is not None:
        session.execute(
            text("UPDATE outbox SET send_after=:sa, defer_reason='quiet_hours' WHERE id=:id"),
            {"sa": release, "id": row["id"]})
        _event(session, uid, row["id"], row["status"], release)
        return "deferred"
    st = session.execute(text("SELECT email, email_enabled FROM user_settings WHERE user_id=:u"), {"u": uid}).first()
    if st is None or not st[0] or not st[1]:
        _cancel(session, row, "no_email")
        return "cancelled"
    # в письме остаются только открытые рекомендации
    recs = digest.open_recs(session, uid, list(row["recommendation_ids"]))
    if not recs:
        _cancel(session, row, "recommendations_closed")
        return "cancelled"
    subj, body = digest.render(recs)
    row.update(to_addr=st[0], subject=subj, body_text=body, recommendation_ids=[r["id"] for r in recs])
    session.execute(
        text("""UPDATE outbox SET status='sending', locked_at=:now, to_addr=:to, subject=:s, body_text=:b,
                       recommendation_ids=:ids, defer_reason=NULL WHERE id=:id"""),
        {"now": now, "to": st[0], "s": subj, "b": body, "ids": row["recommendation_ids"], "id": row["id"]})
    _event(session, uid, row["id"], "sending", None)
    session.commit()  # статус sending виден другим; SMTP вызываем без удержания блокировок
    try:
        smtp_send(row)
        err = None
    except Exception as e:  # noqa: BLE001 - любая сетевая/SMTP-ошибка -> повтор
        err = f"{type(e).__name__}: {e}"
        log.warning("outbox %s: отправка не удалась: %s", row["id"], err)
    now = clock.now()
    if err is None:
        session.execute(text("UPDATE outbox SET status='sent', sent_at=:now, locked_at=NULL, last_error=NULL, "
                             "next_attempt_at=NULL WHERE id=:id"), {"now": now, "id": row["id"]})
        _event(session, uid, row["id"], "sent", None)
        return "sent"
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        status, nxt = "dead", None
    else:
        status, nxt = "failed", now + RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]
    session.execute(
        text("""UPDATE outbox SET status=:st, attempts=:a, last_error=:e, next_attempt_at=:n, locked_at=NULL
                WHERE id=:id"""), {"st": status, "a": attempts, "e": err[:1000], "n": nxt, "id": row["id"]})
    _event(session, uid, row["id"], status, nxt)
    return status


def send_outbox(session: Session, job: dict) -> None:
    """Job send_outbox: payload {outbox_id}; без него — все созревшие письма пользователя."""
    oid = (job.get("payload") or {}).get("outbox_id")
    if oid is not None:
        send_one(session, int(oid))
        return
    ids = session.execute(
        text("SELECT id FROM outbox WHERE user_id=:u AND status IN ('pending','failed') ORDER BY id"),
        {"u": job["user_id"]}).scalars().all()
    for i in ids:
        send_one(session, i)


def reschedule_quiet(session: Session, now: datetime | None = None) -> int:
    """Пересчитывает send_after у неотправленных писем под текущие тихие часы и пояс пользователя."""
    now = now or clock.now()
    rows = session.execute(
        text("SELECT id, user_id, send_after, defer_reason FROM outbox WHERE status IN ('pending','failed')")
    ).all()
    changed = 0
    for oid, uid, send_after, reason in rows:
        release = quiet_hours.user_release(session, uid, now)
        if release is not None:
            if reason != "quiet_hours" or send_after != release:
                session.execute(text("UPDATE outbox SET send_after=:sa, defer_reason='quiet_hours' WHERE id=:id"),
                                {"sa": release, "id": oid})
                _event(session, uid, oid, "pending", release)
                changed += 1
        elif reason == "quiet_hours":  # тихие часы кончились/отключены/сдвинулись
            session.execute(text("UPDATE outbox SET send_after=:sa, defer_reason=NULL WHERE id=:id"),
                            {"sa": min(send_after, now), "id": oid})
            _event(session, uid, oid, "pending", min(send_after, now))
            changed += 1
    return changed


def tick(session: Session) -> None:
    """Раз в 15 с: вернуть протухшие sending, пересчитать тихие часы, запустить созревшие дайджесты и письма."""
    now = clock.now()
    session.execute(
        text("UPDATE outbox SET status='pending', locked_at=NULL WHERE status='sending' AND locked_at < :t"),
        {"t": now - STALE_SENDING})
    reschedule_quiet(session, now)
    hold = timedelta(seconds=get_settings().digest_hold_seconds)
    for (uid,) in session.execute(
        text("SELECT user_id FROM digest_pending GROUP BY user_id HAVING min(created_at) <= :t"),
        {"t": now - hold},
    ).all():
        jobs.enqueue(session, "flush_digest", uid, {}, dedupe_key=f"digest:{uid}")
    for oid, uid in session.execute(
        text("""SELECT id, user_id FROM outbox WHERE status IN ('pending','failed') AND send_after <= :t
                AND (next_attempt_at IS NULL OR next_attempt_at <= :t)"""),
        {"t": now},
    ).all():
        jobs.enqueue(session, "send_outbox", uid, {"outbox_id": oid}, dedupe_key=f"send:{oid}")
