"""Слияние устройств (раздел 4), дневные сводки и пропуски (разделы 5-7).

Чистые функции (merge_*, union_intervals, build_sessions, compute_day) не ходят в БД.
recompute_dates() читает сырьё, считает и пишет daily_summary.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import clock
from .config import get_settings
from .db import NEG_INF
from .timeline import Timeline, from_min, to_min

ACTIVE_STEPS_PER_MIN = 100


# ---------------------------------------------------------------- чистые функции

def merge_steps(rows: Iterable[tuple[int, int, int, int]]) -> dict[int, int]:
    """rows: (device_id, minute, steps, priority) -> {minute: steps} (раздел 4.1)."""
    per: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for dev, t, steps, prio in rows:
        per[t].append((-prio, dev, steps))
    out: dict[int, int] = {}
    for t, lst in per.items():
        lst.sort()
        chosen = lst[0][2]
        if chosen == 0 and len(lst) > 1:
            others = max(x[2] for x in lst[1:])
            if others > 0:
                chosen = others
        out[t] = chosen
    return out


def merge_hr(rows: Iterable[tuple[int, int, int, int]]) -> dict[int, int]:
    """rows: (device_id, ts_seconds, bpm, priority) -> {minute: bpm} (раздел 4.3)."""
    per: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    prio: dict[int, int] = {}
    for dev, ts, bpm, p in rows:
        per[ts // 60][dev].append(bpm)
        prio[dev] = p
    out: dict[int, int] = {}
    for t, devs in per.items():
        best = min(devs, key=lambda dv: (-prio[dv], dv))
        vals = devs[best]
        n = len(vals)
        out[t] = (2 * sum(vals) + n) // (2 * n)  # half-up
    return out


def union_intervals(items: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(items):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def build_sessions(union: list[tuple[int, int]], gap: int) -> list[tuple[int, int, int]]:
    """Сессии сна: цепочки интервалов с разрывами <= gap. -> (start, end, sleep_minutes)."""
    out: list[list[int]] = []
    for a, b in union:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = b
            out[-1][2] += b - a
        else:
            out.append([a, b, b - a])
    return [(a, b, m) for a, b, m in out]


class Covered:
    def __init__(self, union: list[tuple[int, int]]):
        self.starts = [a for a, _ in union]
        self.union = union

    def __contains__(self, t: int) -> bool:
        i = bisect_right(self.starts, t) - 1
        return i >= 0 and t < self.union[i][1]


def half_up_median(vals: list[int]) -> int:
    s = sorted(vals)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2] + 1) // 2


def compute_day(
    d: date,
    tl: Timeline,
    steps_m: dict[int, int],
    hr_m: dict[int, int],
    sleep_union: list[tuple[int, int]],
    sessions: list[tuple[int, int, int]],
    now_min: int,
) -> dict:
    cfg = get_settings()
    ranges = tl.day_ranges(d)
    day_start, day_end, day_len = tl.day_bounds(d)

    wear: set[int] = set()
    steps_total = 0
    steps_cnt = 0
    active = 0
    hr_list: list[tuple[int, int]] = []
    for a, b in ranges:
        for t in range(a, b):
            v = steps_m.get(t)
            if v is not None:
                steps_cnt += 1
                steps_total += v
                if v >= ACTIVE_STEPS_PER_MIN:
                    active += 1
                wear.add(t)
            h = hr_m.get(t)
            if h is not None:
                hr_list.append((t, h))
                wear.add(t)
        for sa, sb in sleep_union:
            lo, hi = max(a, sa), min(b, sb)
            if lo < hi:
                wear.update(range(lo, hi))
    wear_n = len(wear)
    coverage = "none" if wear_n < cfg.wear_partial_min else ("partial" if wear_n < cfg.wear_complete_min else "complete")

    # шаги
    if steps_cnt == 0 or coverage == "none":
        steps_val, steps_status, active_val = None, "no_data", None
    else:
        steps_val, active_val = steps_total, active
        if now_min < day_end:
            steps_status = "in_progress"
        else:
            steps_status = coverage  # partial | complete

    # сон
    day_sessions = [s for s in sessions if tl.local_date(s[1] - 1) == d]
    sleep_total = sum(s[2] for s in day_sessions)
    if day_sessions and sleep_total >= cfg.sleep_min_minutes:
        sleep_status, sleep_val = "ok", sleep_total
        night = [s for s in day_sessions if s[1] - s[0] >= 120] or [max(day_sessions, key=lambda s: s[2])]
        sleep_start, sleep_end = from_min(min(s[0] for s in night)), from_min(max(s[1] for s in night))
    else:
        sleep_status, sleep_val, sleep_start, sleep_end = "no_data", None, None, None

    # пульс
    hr_minutes = len(hr_list)
    resting = peak = avg = None
    if hr_minutes >= cfg.hr_min_minutes:
        hr_status = "ok"
        vals = [h for _, h in hr_list]
        peak = max(vals)
        avg = (2 * sum(vals) + len(vals)) // (2 * len(vals))
        day_sleep = Covered(union_intervals((s[0], s[1]) for s in day_sessions))
        # внутри объединённых интервалов сна (не дыр между ними)
        sleep_cov_all = Covered(sleep_union)
        cands: list[int] = []
        for t, h in hr_list:
            if t in day_sleep and t in sleep_cov_all:
                cands.append(h)
            elif t not in sleep_cov_all and all((t - k) in steps_m for k in range(10)) \
                    and sum(steps_m[t - k] for k in range(10)) == 0:
                cands.append(h)
        if len(cands) >= cfg.resting_min_candidates:
            k = math.ceil(0.1 * len(cands))
            resting = half_up_median(sorted(cands)[:k])
    else:
        hr_status = "no_data"

    return {
        "has_data": wear_n > 0,
        "day_start": from_min(day_start), "day_end": from_min(day_end), "day_length_min": day_len,
        "wear_minutes": wear_n, "coverage": coverage,
        "steps": steps_val, "steps_status": steps_status, "active_minutes": active_val,
        "sleep_minutes": sleep_val, "sleep_start": sleep_start, "sleep_end": sleep_end, "sleep_status": sleep_status,
        "resting_hr": resting, "peak_hr": peak, "avg_hr": avg,
        "hr_minutes": hr_minutes if hr_status == "ok" else None, "hr_status": hr_status,
    }


# ---------------------------------------------------------------- работа с БД

def load_timeline(session: Session, user_id: int) -> Timeline:
    rows = session.execute(
        text("SELECT valid_from, tz_name, fixed_offset_min FROM tz_periods WHERE user_id=:u ORDER BY valid_from"),
        {"u": user_id},
    ).all()
    return Timeline([(r[0], r[1], r[2]) for r in rows])


def get_tz_version(session: Session, user_id: int) -> int:
    v = session.execute(text("SELECT tz_version FROM user_settings WHERE user_id=:u"), {"u": user_id}).scalar()
    return int(v or 0)


def as_of_date(session: Session, user_id: int, tl: Timeline) -> date | None:
    ts = session.execute(
        text(
            """SELECT greatest(
                 (SELECT max(minute_ts) FROM steps_minute WHERE user_id=:u),
                 (SELECT max(ts) FROM hr_points WHERE user_id=:u),
                 (SELECT max(end_ts) - interval '1 minute' FROM sleep_intervals WHERE user_id=:u))"""
        ),
        {"u": user_id},
    ).scalar()
    return tl.local_date(to_min(ts)) if ts is not None else None


def first_data_ts(session: Session, user_id: int) -> datetime | None:
    return session.execute(
        text(
            """SELECT least(
                 (SELECT min(minute_ts) FROM steps_minute WHERE user_id=:u),
                 (SELECT min(ts) FROM hr_points WHERE user_id=:u),
                 (SELECT min(start_ts) FROM sleep_intervals WHERE user_id=:u))"""
        ),
        {"u": user_id},
    ).scalar()


_P = "to_timestamp(CAST(:{} AS bigint) * 60)"


def _load_span(session: Session, user_id: int, lo: int, hi: int):
    steps = session.execute(
        text(
            f"""SELECT m.device_id, (extract(epoch FROM m.minute_ts)::bigint / 60) AS t, m.steps, d.priority
                FROM steps_minute m JOIN devices d ON d.id = m.device_id
                WHERE m.user_id = :u AND m.minute_ts >= {_P.format('lo')} AND m.minute_ts < {_P.format('hi')}"""
        ),
        {"u": user_id, "lo": lo - 10, "hi": hi},
    ).all()
    hr = session.execute(
        text(
            f"""SELECT h.device_id, extract(epoch FROM h.ts)::bigint AS s, h.bpm, d.priority
                FROM hr_points h JOIN devices d ON d.id = h.device_id
                WHERE h.user_id = :u AND h.ts >= {_P.format('lo')} AND h.ts < {_P.format('hi')}"""
        ),
        {"u": user_id, "lo": lo, "hi": hi},
    ).all()
    margin = 48 * 60
    sl = session.execute(
        text(
            f"""SELECT extract(epoch FROM start_ts)::bigint / 60, extract(epoch FROM end_ts)::bigint / 60
                FROM sleep_intervals
                WHERE user_id = :u AND stage <> 'awake'
                  AND end_ts > {_P.format('lo')} AND start_ts < {_P.format('hi')}"""
        ),
        {"u": user_id, "lo": lo - margin, "hi": hi + margin},
    ).all()
    return steps, hr, sl


def recompute_dates(session: Session, user_id: int, dates: Iterable[date], tl: Timeline | None = None) -> dict[date, dict]:
    """Пересчитывает daily_summary для дат. Вызывать под lock_user. Возвращает {date: row|None}."""
    cfg = get_settings()
    tl = tl or load_timeline(session, user_id)
    tzv = get_tz_version(session, user_id)
    now_min = to_min(clock.now())
    ds = sorted(set(dates))
    result: dict[date, dict] = {}
    for i in range(0, len(ds), 10):
        batch = ds[i:i + 10]
        rngs = {d: tl.day_ranges(d) for d in batch}
        lo = min(r[0][0] for r in rngs.values() if r)
        hi = max(r[-1][1] for r in rngs.values() if r)
        steps, hr, sl = _load_span(session, user_id, lo, hi)
        steps_m = merge_steps((r[0], int(r[1]), r[2], r[3]) for r in steps)
        hr_m = merge_hr((r[0], int(r[1]), r[2], r[3]) for r in hr)
        union = union_intervals((int(a), int(b)) for a, b in sl)
        sessions = build_sessions(union, cfg.session_gap_min)
        for d in batch:
            row = compute_day(d, tl, steps_m, hr_m, union, sessions, now_min)
            if not row["has_data"]:
                session.execute(
                    text("DELETE FROM daily_summary WHERE user_id=:u AND local_date=:d"), {"u": user_id, "d": d}
                )
                result[d] = None  # type: ignore[assignment]
                continue
            row = {k: v for k, v in row.items() if k != "has_data"}
            session.execute(
                text(
                    """INSERT INTO daily_summary (user_id, local_date, day_start, day_end, day_length_min,
                         wear_minutes, coverage, steps, steps_status, active_minutes, sleep_minutes, sleep_start,
                         sleep_end, sleep_status, resting_hr, peak_hr, avg_hr, hr_minutes, hr_status, tz_version,
                         computed_at)
                       VALUES (:user_id, :local_date, :day_start, :day_end, :day_length_min, :wear_minutes,
                         :coverage, :steps, :steps_status, :active_minutes, :sleep_minutes, :sleep_start,
                         :sleep_end, :sleep_status, :resting_hr, :peak_hr, :avg_hr, :hr_minutes, :hr_status,
                         :tz_version, now())
                       ON CONFLICT (user_id, local_date) DO UPDATE SET
                         day_start=EXCLUDED.day_start, day_end=EXCLUDED.day_end,
                         day_length_min=EXCLUDED.day_length_min, wear_minutes=EXCLUDED.wear_minutes,
                         coverage=EXCLUDED.coverage, steps=EXCLUDED.steps, steps_status=EXCLUDED.steps_status,
                         active_minutes=EXCLUDED.active_minutes, sleep_minutes=EXCLUDED.sleep_minutes,
                         sleep_start=EXCLUDED.sleep_start, sleep_end=EXCLUDED.sleep_end,
                         sleep_status=EXCLUDED.sleep_status, resting_hr=EXCLUDED.resting_hr,
                         peak_hr=EXCLUDED.peak_hr, avg_hr=EXCLUDED.avg_hr, hr_minutes=EXCLUDED.hr_minutes,
                         hr_status=EXCLUDED.hr_status, tz_version=EXCLUDED.tz_version, computed_at=now()"""
                ),
                {**row, "user_id": user_id, "local_date": d, "tz_version": tzv},
            )
            result[d] = {**row, "local_date": d}
    return result


def dates_of_minutes(tl: Timeline, minutes: Iterable[int], spread_days: int = 0) -> set[date]:
    out = {tl.local_date(t) for t in minutes}
    if spread_days:
        from datetime import timedelta
        out |= {d + timedelta(days=k) for d in list(out) for k in range(-spread_days, spread_days + 1)}
    return out


def dates_between(a: date, b: date) -> list[date]:
    from datetime import timedelta
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def recompute_from(session: Session, user_id: int, from_ts: datetime | None, tl: Timeline | None = None) -> list[date]:
    """Пересчёт от даты (from_ts-1 день) до as_of; None / NEG_INF = вся история. Под lock_user."""
    from datetime import timedelta
    tl = tl or load_timeline(session, user_id)
    first = first_data_ts(session, user_id)
    last = as_of_date(session, user_id, tl)
    if first is None or last is None:
        return []
    start = tl.local_date(to_min(first))
    if from_ts is not None and from_ts > NEG_INF:
        start = max(start, tl.local_date(to_min(from_ts)) - timedelta(days=1))
    # ранее посчитанные даты тоже обновляем (могли сместиться при смене пояса)
    prev = session.execute(
        text("SELECT min(local_date) FROM daily_summary WHERE user_id=:u"), {"u": user_id}
    ).scalar()
    if prev is not None and (from_ts is None or from_ts <= NEG_INF):
        start = min(start, prev)
    dates = dates_between(start, last + timedelta(days=1))
    recompute_dates(session, user_id, dates, tl)
    # неизменившиеся более ранние дни — отметить актуальными по tz_version
    tzv = get_tz_version(session, user_id)
    session.execute(
        text("UPDATE daily_summary SET tz_version=:v WHERE user_id=:u AND local_date < :d AND tz_version < :v"),
        {"v": tzv, "u": user_id, "d": start},
    )
    return dates


def json_dumps(o) -> str:
    return json.dumps(o, default=str)
