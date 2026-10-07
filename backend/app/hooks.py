"""Точки подключения модулей, которые пишутся отдельно (rules, mail).

Контракт:
  * app.rules.engine.evaluate(user_id, session)  — оценка правил; вызывается job-ом evaluate_rules
    (ставится после завершения файла, смены пояса/цели/приоритета устройства) под pg_advisory_xact_lock(user_id).
    Необязательно: app.rules.engine.tick(session) — вызывается планировщиком воркера раз в 15 с.
  * app.mail.JOB_HANDLERS: dict[str, Callable[[Session, dict], None]] — обработчики jobs
    (flush_digest, send_outbox, ...); необязательно app.mail.tick(session) раз в 15 с.
Если модуля нет — хук молча пропускается.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Callable

from sqlalchemy.orm import Session


def _try_import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name and (name == e.name or name.startswith(e.name + ".")):
            return None
        raise


def evaluate_rules(user_id: int, session: Session) -> bool:
    mod = _try_import("app.rules.engine")
    if mod is None or not hasattr(mod, "evaluate"):
        return False
    mod.evaluate(user_id, session)
    return True


def extra_job_handlers() -> dict[str, Callable[[Session, dict], None]]:
    mod = _try_import("app.mail")
    return dict(getattr(mod, "JOB_HANDLERS", {})) if mod else {}


def tick_hooks() -> list[Callable[[Session], None]]:
    out = []
    for name in ("app.rules.engine", "app.mail"):
        mod = _try_import(name)
        if mod is not None and callable(getattr(mod, "tick", None)):
            out.append(mod.tick)
    return out
