"""Локальный SMTP-приёмник (aiosmtpd): python -m app.mail.smtp_sink [--port N] [--host H].

Складывает письма в mail_inbox; идемпотентен по Message-ID (дубль отбрасывается, но отвечает 250).
Наружу ничего не пересылает.
"""
from __future__ import annotations

import argparse
import asyncio
import email
import email.policy
import hashlib
import logging
import time
from email.header import decode_header, make_header

from aiosmtpd.controller import Controller
from sqlalchemy import text

from ..config import get_settings
from ..db import new_session

log = logging.getLogger("ht.smtp_sink")


def store_message(rcpts: list[str], raw: bytes) -> bool:
    """Сохраняет письмо. True — новое, False — дубль по Message-ID."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    mid = (msg.get("Message-ID") or "").strip() or f"<sha1-{hashlib.sha1(raw).hexdigest()}@sink>"
    try:
        subject = str(make_header(decode_header(str(msg.get("Subject") or "")))) or None
    except Exception:  # noqa: BLE001
        subject = str(msg.get("Subject") or "") or None
    with new_session() as s:
        n = s.execute(
            text("""INSERT INTO mail_inbox (message_id, rcpt, raw, subject)
                    VALUES (:m, :r, :raw, :s) ON CONFLICT (message_id) DO NOTHING"""),
            {"m": mid, "r": ", ".join(rcpts), "raw": raw.decode("utf-8", errors="replace"), "s": subject},
        ).rowcount
        s.commit()
    return n > 0


class InboxHandler:
    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - API aiosmtpd
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, store_message, list(envelope.rcpt_tos), envelope.original_content or envelope.content)
        except Exception as e:  # noqa: BLE001
            log.error("не удалось сохранить письмо: %s", e)
            return "451 Временная ошибка, повторите позже"
        return "250 OK"


def start_sink(port: int | None = None, host: str = "127.0.0.1") -> Controller:
    """Запускает приёмник в фоновом потоке (для тестов и для main). port=0 не поддерживается — задайте порт."""
    ctrl = Controller(InboxHandler(), hostname=host, port=port or get_settings().smtp_sink_port)
    ctrl.start()
    return ctrl


def main() -> None:
    p = argparse.ArgumentParser(description="SMTP-приёмник Трекера здоровья")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None, help="по умолчанию SMTP_SINK_PORT из конфига")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ctrl = start_sink(a.port, a.host)
    log.info("smtp-sink слушает %s:%s", a.host, ctrl.port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()


if __name__ == "__main__":
    main()
