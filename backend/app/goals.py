"""Цели и достижения (DESIGN, раздел 8)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from .events import publish_event

GOAL_KINDS = ("steps_per_day", "sleep_minutes_per_day")


def goal_value(kind: str, row: dict | None) -> int | None:
    """Значение метрики дня для цели; None = нет данных."""
    if row is None:
        return None
    if kind == "steps_per_day":
        return row.get("steps")
    if row.get("sleep_status") == "ok":
        return row.get("sleep_minutes")
    return None


def evaluate_goals(
    session: Session, user_id: int, dates: Iterable[date], as_of: date | None, rows: dict[date, dict | None] | None = None
) -> list[dict]:
    """Проверяет достижения для дат. Возвращает созданные уведомления. Вызывать под lock_user."""
    dates = sorted(set(dates))
    if not dates:
        return []
    goals = session.execute(
        text("SELECT id, kind, target FROM goals WHERE user_id=:u AND active"), {"u": user_id}
    ).all()
    if not goals:
        return []
    if rows is None:
        found = session.execute(
            text(
                """SELECT local_date, steps, sleep_minutes, sleep_status FROM daily_summary
                   WHERE user_id=:u AND local_date = ANY(:ds)"""
            ),
            {"u": user_id, "ds": dates},
        ).mappings().all()
        rows = {r["local_date"]: dict(r) for r in found}
    created: list[dict] = []
    for goal_id, kind, target in goals:
        for d in dates:
            value = goal_value(kind, rows.get(d))
            reached = value is not None and value >= target
            if reached:
                ach = session.execute(
                    text(
                        """INSERT INTO goal_achievements (user_id, goal_id, local_date, target_at_time, value)
                           VALUES (:u, :g, :d, :t, :v) ON CONFLICT (goal_id, local_date) DO NOTHING RETURNING id"""
                    ),
                    {"u": user_id, "g": goal_id, "d": d, "t": target, "v": value},
                ).scalar_one_or_none()
                if ach is None:
                    # уже есть: снять отзыв (повторное достижение без нового уведомления), обновить значение
                    session.execute(
                        text(
                            """UPDATE goal_achievements SET revoked_at = NULL, value = :v
                               WHERE goal_id=:g AND local_date=:d AND (revoked_at IS NOT NULL OR value <> :v)"""
                        ),
                        {"v": value, "g": goal_id, "d": d},
                    )
                    continue
                if as_of is not None and d >= as_of - timedelta(days=1):
                    note = session.execute(
                        text(
                            """INSERT INTO notifications (user_id, kind, payload, dedup_key)
                               VALUES (:u, 'goal_achieved', CAST(:p AS jsonb), :k)
                               ON CONFLICT (user_id, dedup_key) DO NOTHING RETURNING id"""
                        ),
                        {
                            "u": user_id, "k": f"goal:{goal_id}:{d.isoformat()}",
                            "p": _json({"goal_kind": kind, "date": d.isoformat(), "value": value, "target": target}),
                        },
                    ).scalar_one_or_none()
                    if note is not None:
                        publish_event(session, user_id, "goal.achieved", {
                            "notification_id": note, "goal_kind": kind, "date": d.isoformat(),
                            "value": value, "target": target,
                        })
                        publish_event(session, user_id, "notification.created", {"id": note, "kind": "goal_achieved"})
                        created.append({"id": note, "goal_kind": kind, "date": d})
            else:
                session.execute(
                    text(
                        """UPDATE goal_achievements SET revoked_at = now()
                           WHERE goal_id=:g AND local_date=:d AND revoked_at IS NULL
                             AND (CAST(:v AS int) IS NULL OR CAST(:v AS int) < target_at_time)"""
                    ),
                    {"g": goal_id, "d": d, "v": value},
                )
    return created


def _json(o) -> str:
    import json
    return json.dumps(o)
