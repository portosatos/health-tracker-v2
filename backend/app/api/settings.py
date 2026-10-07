"""Настройки: почта, пояс, тихие часы."""
from __future__ import annotations

import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock, jobs
from ..db import NEG_INF, lock_user
from ..events import publish_event
from ..ingest import bump_tz_version, tz_periods_view
from ..summary import load_timeline
from .deps import ApiError, CurrentUser, current_user, get_db

router = APIRouter(prefix="/settings", tags=["settings"])
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def settings_view(db: Session, user_id: int) -> dict:
    r = db.execute(text("SELECT * FROM user_settings WHERE user_id=:u"), {"u": user_id}).mappings().one()
    tl = load_timeline(db, user_id)
    return {
        "email": r["email"], "email_enabled": r["email_enabled"], "timezone": tl.current_name(),
        "timezone_periods": tz_periods_view(db, user_id), "follow_device_tz": r["follow_device_tz"],
        "quiet_hours": {"enabled": r["quiet_enabled"], "start": r["quiet_start"].strftime("%H:%M"),
                        "end": r["quiet_end"].strftime("%H:%M")},
    }


@router.get("")
def get_settings_(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    return settings_view(db, user.id)


class QuietBody(BaseModel):
    enabled: bool | None = None
    start: str | None = None
    end: str | None = None


class SettingsPatch(BaseModel):
    email: str | None = None
    email_enabled: bool | None = None
    follow_device_tz: bool | None = None
    quiet_hours: QuietBody | None = None


def _hhmm(v: str) -> time:
    m = HHMM_RE.match(v)
    if not m:
        raise ApiError(422, "validation_error", "Время: формат HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


@router.patch("")
def patch_settings(body: SettingsPatch, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    f = body.model_fields_set
    sets: dict = {}
    if "email" in f:
        email = (body.email or "").strip()
        if email and (not EMAIL_RE.match(email) or len(email) > 254):
            raise ApiError(422, "validation_error", "Некорректный email")
        sets["email"] = email or None
    if body.email_enabled is not None:
        sets["email_enabled"] = body.email_enabled
    if body.follow_device_tz is not None:
        sets["follow_device_tz"] = body.follow_device_tz
    if body.quiet_hours is not None:
        q = body.quiet_hours
        if q.enabled is not None:
            sets["quiet_enabled"] = q.enabled
        if q.start is not None:
            sets["quiet_start"] = _hhmm(q.start)
        if q.end is not None:
            sets["quiet_end"] = _hhmm(q.end)
    if sets:
        cols = ", ".join(f"{k} = :{k}" for k in sets)
        db.execute(text(f"UPDATE user_settings SET {cols} WHERE user_id = :u"), {**sets, "u": user.id})
        db.commit()
    return settings_view(db, user.id)


class TzBody(BaseModel):
    timezone: str
    effective_from: datetime | None = None
    scope: str = "from_now"


@router.put("/timezone")
def put_timezone(body: TzBody, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    try:
        ZoneInfo(body.timezone)
    except Exception:  # noqa: BLE001
        raise ApiError(422, "validation_error", "Неизвестный часовой пояс") from None
    if body.scope not in ("from_now", "all_history"):
        raise ApiError(422, "validation_error", "scope: from_now|all_history")
    lock_user(db, user.id)
    if body.scope == "all_history":
        db.execute(text("DELETE FROM tz_periods WHERE user_id=:u"), {"u": user.id})
        db.execute(
            text("INSERT INTO tz_periods (user_id, valid_from, tz_name, source) VALUES (:u, :v, :z, 'manual')"),
            {"u": user.id, "v": NEG_INF, "z": body.timezone})
        from_iso = None
    else:
        vf = body.effective_from or clock.now()
        if vf.tzinfo is None:
            vf = vf.replace(tzinfo=timezone.utc)
        vf = vf.astimezone(timezone.utc).replace(second=0, microsecond=0)
        db.execute(
            text("""INSERT INTO tz_periods (user_id, valid_from, tz_name, source) VALUES (:u, :v, :z, 'manual')
                    ON CONFLICT (user_id, valid_from) DO UPDATE
                      SET tz_name = EXCLUDED.tz_name, fixed_offset_min = NULL, source = 'manual'"""),
            {"u": user.id, "v": vf, "z": body.timezone})
        from_iso = vf.isoformat()
    tzv = bump_tz_version(db, user.id)
    jobs.enqueue(db, "recompute_days", user.id, {"from": from_iso}, dedupe_key=f"recompute:{user.id}:{from_iso or 'all'}")
    publish_event(db, user.id, "settings.tz_changed", {
        "timezone_periods": tz_periods_view(db, user.id), "reason": "manual", "tz_version": tzv})
    db.commit()
    return JSONResponse(status_code=202, content=settings_view(db, user.id))
