"""Почтовая подсистема (DESIGN, раздел 10): дайджест, тихие часы, отправка, приёмник.

Хуки воркера (см. app/hooks.py): JOB_HANDLERS и tick. Приёмник (smtp_sink) и api подключаются отдельно,
чтобы импорт пакета не требовал aiosmtpd.
"""
from __future__ import annotations

from .digest import flush_digest, schedule_flush
from .sender import send_outbox, tick

JOB_HANDLERS = {
    "flush_digest": flush_digest,
    "send_outbox": send_outbox,
}

__all__ = ["JOB_HANDLERS", "tick", "schedule_flush"]
