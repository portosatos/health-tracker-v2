"""Тихие часы (DESIGN, раздел 10): чистые функции + чтение настроек пользователя."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

UTC = timezone.utc


def is_quiet(t: time, start: time, end: time) -> bool:
    """t — локальное время. start == end — тихих часов нет; start > end — интервал через полночь."""
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def _resolve(d, t: time, tz: tzinfo, fold: int) -> datetime:
    """Локальное «стенное» время -> UTC. Несуществующее (весенний переход) сдвигается вперёд."""
    naive = datetime.combine(d, t)
    u = naive.replace(tzinfo=tz, fold=fold).astimezone(UTC)
    if u.astimezone(tz).replace(tzinfo=None) != naive:  # попали в «дыру» перехода на летнее время
        before = (u - timedelta(days=1)).astimezone(tz).utcoffset()  # смещение до перехода
        return naive.replace(tzinfo=timezone(before)).astimezone(UTC)
    return u


def release_time(now: datetime, tz: tzinfo, enabled: bool, start: time, end: time) -> datetime | None:
    """Если now в тихих часах — ближайший момент конца тихих часов (строго после now), иначе None."""
    if not enabled:
        return None
    local = now.astimezone(tz)
    if not is_quiet(local.time(), start, end):
        return None
    d = local.date()
    cands = [_resolve(d + timedelta(days=k), end, tz, fold) for k in (0, 1) for fold in (0, 1)]
    future = [c for c in cands if c > now]
    return min(future) if future else now


def user_tz(session: Session, user_id: int, at: datetime) -> tzinfo:
    """Часовой пояс пользователя на момент at (период таймлайна tz_periods)."""
    row = session.execute(
        text("""SELECT tz_name, fixed_offset_min FROM tz_periods
                WHERE user_id = :u AND valid_from <= :at ORDER BY valid_from DESC LIMIT 1"""),
        {"u": user_id, "at": at},
    ).first()
    if row is None:
        row = session.execute(
            text("SELECT tz_name, fixed_offset_min FROM tz_periods WHERE user_id=:u ORDER BY valid_from LIMIT 1"),
            {"u": user_id},
        ).first()
    if row is None:
        return UTC
    if row[0]:
        return ZoneInfo(row[0])
    return timezone(timedelta(minutes=int(row[1] or 0)))


def user_release(session: Session, user_id: int, now: datetime) -> datetime | None:
    """Конец тихих часов пользователя, если now в них; иначе None."""
    s = session.execute(
        text("SELECT quiet_enabled, quiet_start, quiet_end FROM user_settings WHERE user_id=:u"), {"u": user_id}
    ).first()
    if s is None:
        return None
    return release_time(now, user_tz(session, user_id, now), bool(s[0]), s[1], s[2])
