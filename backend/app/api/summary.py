"""Сводки: день, диапазон, неделя; сравнение с предыдущим периодом (DESIGN, разделы 6-7)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from datetime import date as _date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock
from ..db import lock_user
from ..summary import get_tz_version, load_timeline, recompute_dates
from ..timeline import Timeline, from_min, to_min
from .deps import ApiError, CurrentUser, current_user, get_db

router = APIRouter(prefix="/summary", tags=["summary"])


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def parse_date(s: str, name: str = "date") -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ApiError(422, "validation_error", f"{name}: ожидается YYYY-MM-DD") from None


def load_rows(db: Session, user_id: int, a: date, b: date) -> dict[date, dict]:
    rows = db.execute(
        text("SELECT * FROM daily_summary WHERE user_id=:u AND local_date BETWEEN :a AND :b"),
        {"u": user_id, "a": a, "b": b},
    ).mappings().all()
    return {r["local_date"]: dict(r) for r in rows}


def refresh_in_progress(db: Session, user_id: int, rows: dict[date, dict], tl: Timeline) -> None:
    """День, который был «в процессе», но уже закончился, пересчитываем при чтении."""
    now_m = to_min(clock.now())
    stale = [d for d, r in rows.items() if r["steps_status"] == "in_progress" and to_min(r["day_end"]) <= now_m]
    if stale:
        lock_user(db, user_id)
        recompute_dates(db, user_id, stale, tl)
        db.commit()
        rows.update(load_rows(db, user_id, min(stale), max(stale)))


def day_view(row: dict | None, d: date, tl: Timeline, tzv: int) -> dict:
    if row is None:
        s, e, ln = tl.day_bounds(d)
        return {
            "date": d.isoformat(), "day_start": iso(from_min(s)), "day_end": iso(from_min(e)), "day_length_min": ln,
            "coverage": "none", "wear_minutes": 0, "stale": False,
            "steps": {"value": None, "status": "no_data"}, "active_minutes": None,
            "sleep": {"minutes": None, "status": "no_data", "start": None, "end": None},
            "hr": {"resting": None, "peak": None, "avg": None, "status": "no_data"},
        }
    return {
        "date": d.isoformat(), "day_start": iso(row["day_start"]), "day_end": iso(row["day_end"]),
        "day_length_min": row["day_length_min"], "coverage": row["coverage"], "wear_minutes": row["wear_minutes"],
        "stale": row["tz_version"] < tzv,
        "steps": {"value": row["steps"], "status": row["steps_status"]},
        "active_minutes": row["active_minutes"],
        "sleep": {"minutes": row["sleep_minutes"], "status": row["sleep_status"],
                  "start": iso(row["sleep_start"]), "end": iso(row["sleep_end"])},
        "hr": {"resting": row["resting_hr"], "peak": row["peak_hr"], "avg": row["avg_hr"], "status": row["hr_status"]},
    }


def make_cmp(cur, prev, min_days: int | None = None, cur_days: int | None = None, prev_days: int | None = None) -> dict:
    out = {"current": cur, "previous": prev, "delta_abs": None, "delta_pct": None, "comparable": False, "reason": None}
    if cur is None:
        out["reason"] = "current_no_data"
    elif prev is None:
        out["reason"] = "previous_no_data"
    elif min_days is not None and ((cur_days or 0) < min_days or (prev_days or 0) < min_days):
        out["reason"] = "insufficient_coverage"
    else:
        out["comparable"] = True
        delta = cur - prev
        out["delta_abs"] = round(delta, 1) if isinstance(delta, float) else delta
        out["delta_pct"] = None if prev == 0 else round(delta / prev * 100, 1)
    return out


def _metric_values(row: dict | None) -> dict:
    if row is None:
        return {"steps": None, "sleep": None, "resting_hr": None, "peak_hr": None}
    return {
        "steps": row["steps"] if row["steps_status"] == "complete" else None,
        "sleep": row["sleep_minutes"] if row["sleep_status"] == "ok" else None,
        "resting_hr": row["resting_hr"],
        "peak_hr": row["peak_hr"] if row["hr_status"] == "ok" else None,
    }


def goals_progress(db: Session, user_id: int, d: date, row: dict | None) -> list[dict]:
    goals = db.execute(text("SELECT id, kind, target FROM goals WHERE user_id=:u AND active ORDER BY id"),
                       {"u": user_id}).all()
    ach = {r[0] for r in db.execute(
        text("SELECT goal_id FROM goal_achievements WHERE user_id=:u AND local_date=:d AND revoked_at IS NULL"),
        {"u": user_id, "d": d}).all()}
    out = []
    for gid, kind, target in goals:
        if kind == "steps_per_day":
            val, status = (row["steps"], row["steps_status"]) if row else (None, "no_data")
        else:
            val, status = (row["sleep_minutes"], row["sleep_status"]) if row else (None, "no_data")
        out.append({"kind": kind, "target": target, "value": val,
                    "percent": None if val is None else round(val / target * 100), "status": status,
                    "achieved": gid in ach})
    return out


def is_processing(db: Session, user_id: int) -> bool:
    return bool(db.execute(
        text("""SELECT EXISTS (SELECT 1 FROM jobs WHERE user_id=:u AND status IN ('queued','running')
                               AND kind IN ('process_upload','recompute_days'))"""), {"u": user_id}).scalar())


@router.get("/daily")
def daily(date: str = Query(...), user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    d = parse_date(date)
    tl = load_timeline(db, user.id)
    tzv = get_tz_version(db, user.id)
    rows = load_rows(db, user.id, d - timedelta(days=1), d)
    refresh_in_progress(db, user.id, rows, tl)
    view = day_view(rows.get(d), d, tl, tzv)
    cur, prev = _metric_values(rows.get(d)), _metric_values(rows.get(d - timedelta(days=1)))
    view["comparison"] = {k: make_cmp(cur[k], prev[k]) for k in cur}
    view["goals"] = goals_progress(db, user.id, d, rows.get(d))
    view["processing"] = is_processing(db, user.id)
    return view


@router.get("/daily/range")
def daily_range(from_: str = Query(alias="from"), to: str = Query(...), user: CurrentUser = Depends(current_user),
                db: Session = Depends(get_db)):
    a, b = parse_date(from_, "from"), parse_date(to, "to")
    if b < a or (b - a).days + 1 > 100:
        raise ApiError(422, "validation_error", "Диапазон: от 1 до 100 дней")
    tl = load_timeline(db, user.id)
    tzv = get_tz_version(db, user.id)
    rows = load_rows(db, user.id, a, b)
    refresh_in_progress(db, user.id, rows, tl)
    return {"days": [day_view(rows.get(a + timedelta(days=i)), a + timedelta(days=i), tl, tzv)
                     for i in range((b - a).days + 1)]}


def week_str(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_aggregate(rows: dict[date, dict], monday: date, today: date) -> dict:
    sunday = monday + timedelta(days=6)
    days = [rows.get(monday + timedelta(days=i)) for i in range(7)]
    steps_ok = [r["steps"] for r in days if r and r["steps_status"] == "complete"]
    steps_any = [r["steps"] for r in days if r and r["steps_status"] in ("complete", "partial") and r["steps"] is not None]
    sleeps = [r["sleep_minutes"] for r in days if r and r["sleep_status"] == "ok"]
    rest = [r["resting_hr"] for r in days if r and r["resting_hr"] is not None]
    peaks = [r["peak_hr"] for r in days if r and r["peak_hr"] is not None]
    n = len(steps_ok)
    if today <= sunday:
        status = "in_progress"
    else:
        status = "complete" if n >= 5 else ("partial" if n >= 1 else "no_data")
    blank = status == "no_data" or (status == "in_progress" and n == 0)
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None  # noqa: E731
    return {
        "week": week_str(monday), "from": monday.isoformat(), "to": sunday.isoformat(), "status": status,
        "valid_days_steps": n,
        "steps_avg": None if blank else avg(steps_ok),
        "steps_total": None if blank else sum(steps_any),
        "sleep_avg": None if blank else avg(sleeps), "sleep_nights": len(sleeps),
        "resting_hr_avg": None if blank else avg(rest), "resting_hr_days": len(rest),
        "peak_hr_max": None if blank else (max(peaks) if peaks else None), "peak_hr_days": len(peaks),
    }


@router.get("/weekly")
def weekly(week: str | None = None, date: str | None = None, user: CurrentUser = Depends(current_user),
           db: Session = Depends(get_db)):
    if week:
        try:
            y, w = week.upper().split("-W")
            monday = _date.fromisocalendar(int(y), int(w), 1)
        except (ValueError, TypeError):
            raise ApiError(422, "validation_error", "week: ожидается YYYY-Www") from None
    elif date:
        d = parse_date(date)
        monday = d - timedelta(days=d.weekday())
    else:
        raise ApiError(422, "validation_error", "Нужен week или date")
    tl = load_timeline(db, user.id)
    tzv = get_tz_version(db, user.id)
    today = tl.local_date(to_min(clock.now()))
    rows = load_rows(db, user.id, monday - timedelta(days=7), monday + timedelta(days=6))
    refresh_in_progress(db, user.id, rows, tl)
    cur = week_aggregate(rows, monday, today)
    prev = week_aggregate(rows, monday - timedelta(days=7), today)
    sunday = monday + timedelta(days=6)
    gd = db.execute(
        text("""SELECT g.kind, count(*) FROM goal_achievements a JOIN goals g ON g.id = a.goal_id
                JOIN daily_summary s ON s.user_id = a.user_id AND s.local_date = a.local_date
                WHERE a.user_id=:u AND a.local_date BETWEEN :a AND :b AND a.revoked_at IS NULL
                  AND ((g.kind='steps_per_day' AND s.steps_status='complete')
                    OR (g.kind='sleep_minutes_per_day' AND s.sleep_status='ok'))
                GROUP BY g.kind"""),
        {"u": user.id, "a": monday, "b": sunday}).all()
    gmap = dict(gd)
    cur["goal_days"] = gmap.get("steps_per_day", 0)
    cur["goal_days_sleep"] = gmap.get("sleep_minutes_per_day", 0)
    cur["days"] = [day_view(rows.get(monday + timedelta(days=i)), monday + timedelta(days=i), tl, tzv)
                   for i in range(7)]
    cur["comparison"] = {
        "steps": make_cmp(cur["steps_avg"], prev["steps_avg"], 3, cur["valid_days_steps"], prev["valid_days_steps"]),
        "sleep": make_cmp(cur["sleep_avg"], prev["sleep_avg"], 3, cur["sleep_nights"], prev["sleep_nights"]),
        "resting_hr": make_cmp(cur["resting_hr_avg"], prev["resting_hr_avg"], 3, cur["resting_hr_days"],
                               prev["resting_hr_days"]),
        "peak_hr": make_cmp(cur["peak_hr_max"], prev["peak_hr_max"], 3, cur["peak_hr_days"], prev["peak_hr_days"]),
    }
    cur["previous_week"] = prev["week"]
    return cur
