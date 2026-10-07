"""Приём NDJSON: разбор и валидация строк (раздел 2), запись сырья (раздел 3/4), обработка файла чанками (раздел 11)."""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import jobs
from .config import get_settings
from .db import lock_user, new_session
from .events import publish_event
from .goals import evaluate_goals
from .summary import (
    as_of_date, dates_between, dates_of_minutes, get_tz_version, load_timeline, recompute_dates, recompute_from,
)
from .timeline import Timeline, from_min, to_min

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
KINDS = ("watch", "phone", "other")
DEFAULT_PRIORITY = {"watch": 20, "phone": 10, "other": 5}
STAGES = ("asleep", "light", "deep", "rem", "awake")
TZ_DETECT_MIN_DIFF = 30
TZ_DETECT_MIN_RUN = 3
MAX_STORED_ERRORS = 100


class LineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Rec:
    type: str
    ext: str
    start: int = 0       # эпох-минуты (steps/sleep) или секунды (hr)
    end: int = 0
    value: int = 0       # count / bpm
    stage: str = "asleep"
    offset: int | None = None
    kind: str | None = None
    model: str | None = None
    priority: int | None = None
    status: str = "duplicate"
    line_no: int = 0


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_time(obj: dict, name: str, aligned: bool) -> int:
    v = obj.get(name)
    if v is None:
        raise LineError("missing_field", f"нет поля {name}")
    if not isinstance(v, str) or not TIME_RE.match(v):
        raise LineError("bad_time", f"{name}: ожидается YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise LineError("bad_time", f"{name}: несуществующая дата/время") from None
    if aligned and dt.second != 0:
        raise LineError("bad_time", f"{name}: секунды должны быть 00")
    return calendar.timegm(dt.timetuple())


def _offset(obj: dict) -> int | None:
    o = obj.get("offset_min")
    if o is None:
        return None
    if not _is_int(o) or not -720 <= o <= 840:
        raise LineError("bad_value", "offset_min: целое -720..840")
    return o


def parse_line(raw: bytes) -> Rec | None:
    """None — пустая строка. Ошибка -> LineError."""
    if len(raw) > get_settings().max_line_bytes:
        raise LineError("bad_value", "строка длиннее 4 КБ")
    try:
        s = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise LineError("bad_json", "не UTF-8") from None
    if not s:
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        raise LineError("bad_json", "невалидный JSON") from None
    if not isinstance(obj, dict):
        raise LineError("bad_json", "ожидается объект")
    if "v" not in obj:
        raise LineError("missing_field", "нет поля v")
    if obj["v"] != 1 or isinstance(obj["v"], bool):
        raise LineError("unsupported_version", f"версия {obj['v']!r} не поддерживается")
    typ = obj.get("type")
    if typ is None:
        raise LineError("missing_field", "нет поля type")
    if typ not in ("device", "sleep", "steps", "heart_rate"):
        raise LineError("unknown_type", f"неизвестный type {typ!r}")
    ext = obj.get("device_id")
    if ext is None:
        raise LineError("missing_field", "нет поля device_id")
    if not isinstance(ext, str) or not DEVICE_ID_RE.match(ext):
        raise LineError("bad_value", "device_id: 1..64 символов [A-Za-z0-9._:-]")

    if typ == "device":
        kind = obj.get("kind")
        if kind is not None and kind not in KINDS:
            raise LineError("bad_value", "kind: watch|phone|other")
        model = obj.get("model")
        if model is not None and (not isinstance(model, str) or len(model) > 64):
            raise LineError("bad_value", "model: строка до 64 символов")
        prio = obj.get("priority")
        if prio is not None and (not _is_int(prio) or not 0 <= prio <= 100):
            raise LineError("bad_value", "priority: целое 0..100")
        return Rec("device", ext, kind=kind, model=model, priority=prio)

    off = _offset(obj)
    if typ == "heart_rate":
        ts = _parse_time(obj, "ts", aligned=False)
        bpm = obj.get("bpm")
        if bpm is None:
            raise LineError("missing_field", "нет поля bpm")
        if not _is_int(bpm) or not 25 <= bpm <= 250:
            raise LineError("bad_value", "bpm: целое 25..250")
        return Rec("heart_rate", ext, start=ts, end=ts, value=bpm, offset=off)

    start = _parse_time(obj, "start", aligned=True) // 60
    end = _parse_time(obj, "end", aligned=True) // 60
    if start >= end:
        raise LineError("bad_interval", "start должен быть < end")
    if typ == "sleep":
        stage = obj.get("stage", "asleep")
        if stage not in STAGES:
            raise LineError("bad_value", "stage: asleep|light|deep|rem|awake")
        if end - start > 16 * 60:
            raise LineError("bad_interval", "сон длиннее 16 ч")
        return Rec("sleep", ext, start=start, end=end, stage=stage, offset=off)
    count = obj.get("count")
    if count is None:
        raise LineError("missing_field", "нет поля count")
    if not _is_int(count) or count < 0:
        raise LineError("bad_value", "count: целое >= 0")
    if end - start > 360:
        raise LineError("bad_interval", "интервал шагов длиннее 360 мин")
    return Rec("steps", ext, start=start, end=end, value=count, offset=off)


# ---------------------------------------------------------------- запись сырья

def _resolve_devices(session: Session, user_id: int, recs: list[Rec]) -> tuple[dict[str, tuple[int, int]], bool]:
    """-> ({ext: (device_id, priority)}, priority_changed)."""
    prio_changed = False
    for r in [x for x in recs if x.type == "device"]:
        row = session.execute(
            text("SELECT id, kind, model, priority, priority_manual FROM devices WHERE user_id=:u AND external_id=:e"),
            {"u": user_id, "e": r.ext},
        ).first()
        if row is None:
            kind = r.kind or "other"
            prio = r.priority if r.priority is not None else DEFAULT_PRIORITY[kind]
            session.execute(
                text(
                    """INSERT INTO devices (user_id, external_id, kind, model, priority)
                       VALUES (:u, :e, :k, :m, :p) ON CONFLICT (user_id, external_id) DO NOTHING"""
                ),
                {"u": user_id, "e": r.ext, "k": kind, "m": r.model, "p": prio},
            )
            r.status = "inserted"
            continue
        did, old_kind, old_model, old_prio, manual = row
        kind = r.kind or old_kind
        model = r.model if r.model is not None else old_model
        prio = old_prio
        if not manual:
            if r.priority is not None:
                prio = r.priority
            elif r.kind and old_prio == DEFAULT_PRIORITY.get(old_kind):
                prio = DEFAULT_PRIORITY[kind]
        if (kind, model, prio) != (old_kind, old_model, old_prio):
            session.execute(
                text("UPDATE devices SET kind=:k, model=:m, priority=:p WHERE id=:id"),
                {"k": kind, "m": model, "p": prio, "id": did},
            )
            r.status = "updated"
            if prio != old_prio:
                prio_changed = True
    exts = {r.ext for r in recs}
    for e in exts:
        session.execute(
            text(
                """INSERT INTO devices (user_id, external_id, kind, priority) VALUES (:u, :e, 'other', 5)
                   ON CONFLICT (user_id, external_id) DO NOTHING"""
            ),
            {"u": user_id, "e": e},
        )
    rows = session.execute(
        text("SELECT external_id, id, priority FROM devices WHERE user_id=:u AND external_id = ANY(:e)"),
        {"u": user_id, "e": list(exts)},
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}, prio_changed


_STEPS_SQL = text(
    """INSERT INTO steps_minute (user_id, device_id, minute_ts, steps, offset_min, upload_id)
       SELECT :u, t.d, to_timestamp(t.m * 60), t.s, t.o, :up
       FROM unnest(CAST(:d AS bigint[]), CAST(:m AS bigint[]), CAST(:s AS int[]), CAST(:o AS int[])) AS t(d, m, s, o)
       ON CONFLICT (device_id, minute_ts) DO UPDATE
         SET steps = EXCLUDED.steps, offset_min = EXCLUDED.offset_min, upload_id = EXCLUDED.upload_id
         WHERE steps_minute.steps IS DISTINCT FROM EXCLUDED.steps
       RETURNING device_id, (extract(epoch FROM minute_ts)::bigint / 60), (xmax = 0)"""
)
_HR_SQL = text(
    """INSERT INTO hr_points (user_id, device_id, ts, bpm, offset_min, upload_id)
       SELECT :u, t.d, to_timestamp(t.m), t.s, t.o, :up
       FROM unnest(CAST(:d AS bigint[]), CAST(:m AS bigint[]), CAST(:s AS int[]), CAST(:o AS int[])) AS t(d, m, s, o)
       ON CONFLICT (device_id, ts) DO UPDATE
         SET bpm = EXCLUDED.bpm, offset_min = EXCLUDED.offset_min, upload_id = EXCLUDED.upload_id
         WHERE hr_points.bpm IS DISTINCT FROM EXCLUDED.bpm
       RETURNING device_id, extract(epoch FROM ts)::bigint, (xmax = 0)"""
)
_SLEEP_SQL = text(
    """INSERT INTO sleep_intervals (user_id, device_id, start_ts, end_ts, stage, offset_min, upload_id)
       SELECT :u, t.d, to_timestamp(t.s * 60), to_timestamp(t.e * 60), t.st, t.o, :up
       FROM unnest(CAST(:d AS bigint[]), CAST(:s AS bigint[]), CAST(:e AS bigint[]), CAST(:st AS text[]),
                   CAST(:o AS int[])) AS t(d, s, e, st, o)
       ON CONFLICT (device_id, start_ts, end_ts) DO UPDATE
         SET stage = EXCLUDED.stage, offset_min = EXCLUDED.offset_min, upload_id = EXCLUDED.upload_id
         WHERE sleep_intervals.stage IS DISTINCT FROM EXCLUDED.stage
       RETURNING device_id, (extract(epoch FROM start_ts)::bigint / 60), (extract(epoch FROM end_ts)::bigint / 60),
                 (xmax = 0)"""
)
_BATCH = 50000


def _status_from(flags: list[bool | None]) -> str:
    if any(f is True for f in flags):
        return "inserted"
    if any(f is False for f in flags):
        return "updated"
    return "duplicate"


def _write_steps(session: Session, user_id: int, upload_id: int, recs: list[Rec], dev: dict[str, tuple[int, int]]) -> None:
    cells: dict[tuple[int, int], tuple[int, int | None, Rec]] = {}
    for r in recs:
        did = dev[r.ext][0]
        n = r.end - r.start
        for i in range(n):
            v = ((i + 1) * r.value) // n - (i * r.value) // n
            cells[(did, r.start + i)] = (v, r.offset, r)
    keys = list(cells)
    changed: dict[tuple[int, int], bool] = {}
    for i in range(0, len(keys), _BATCH):
        part = keys[i:i + _BATCH]
        res = session.execute(_STEPS_SQL, {
            "u": user_id, "up": upload_id,
            "d": [k[0] for k in part], "m": [k[1] for k in part],
            "s": [cells[k][0] for k in part], "o": [cells[k][1] for k in part],
        })
        for d, m, ins in res:
            changed[(d, int(m))] = bool(ins)
    flags: dict[int, list[bool | None]] = {}
    for k, (_, _, r) in cells.items():
        flags.setdefault(id(r), []).append(changed.get(k))
    for r in recs:
        r.status = _status_from(flags.get(id(r), []))


def _write_hr(session: Session, user_id: int, upload_id: int, recs: list[Rec], dev: dict[str, tuple[int, int]]) -> None:
    cells = {(dev[r.ext][0], r.start): r for r in recs}
    keys = list(cells)
    changed: dict[tuple[int, int], bool] = {}
    for i in range(0, len(keys), _BATCH):
        part = keys[i:i + _BATCH]
        res = session.execute(_HR_SQL, {
            "u": user_id, "up": upload_id, "d": [k[0] for k in part], "m": [k[1] for k in part],
            "s": [cells[k].value for k in part], "o": [cells[k].offset for k in part],
        })
        for d, m, ins in res:
            changed[(d, int(m))] = bool(ins)
    owner = {id(r): k for k, r in cells.items()}
    for r in recs:
        r.status = _status_from([changed.get(owner[id(r)])]) if id(r) in owner else "duplicate"


def _write_sleep(session: Session, user_id: int, upload_id: int, recs: list[Rec], dev: dict[str, tuple[int, int]]) -> None:
    cells = {(dev[r.ext][0], r.start, r.end): r for r in recs}
    keys = list(cells)
    changed: dict[tuple[int, int, int], bool] = {}
    for i in range(0, len(keys), _BATCH):
        part = keys[i:i + _BATCH]
        res = session.execute(_SLEEP_SQL, {
            "u": user_id, "up": upload_id, "d": [k[0] for k in part], "s": [k[1] for k in part],
            "e": [k[2] for k in part], "st": [cells[k].stage for k in part], "o": [cells[k].offset for k in part],
        })
        for d, s, e, ins in res:
            changed[(d, int(s), int(e))] = bool(ins)
    owner = {id(r): k for k, r in cells.items()}
    for r in recs:
        r.status = _status_from([changed.get(owner[id(r)])]) if id(r) in owner else "duplicate"


# ---------------------------------------------------------------- смена пояса по устройству (5.4.2)

def detect_device_tz(
    session: Session, user_id: int, recs: list[Rec], dev: dict[str, tuple[int, int]], tl: Timeline
) -> tuple[Timeline, list[datetime]]:
    """Добавляет периоды source='device'. Возвращает (новый таймлайн, список valid_from добавленных)."""
    follow = session.execute(
        text("SELECT follow_device_tz FROM user_settings WHERE user_id=:u"), {"u": user_id}
    ).scalar()
    if not follow:
        return tl, []
    withoff = [r for r in recs if r.type != "device" and r.offset is not None]
    if not withoff:
        return tl, []
    top = min({r.ext for r in withoff}, key=lambda e: (-dev[e][1], dev[e][0]))
    series = sorted(
        ((r.start // 60 if r.type == "heart_rate" else r.start), r.offset) for r in withoff if r.ext == top
    )
    added: list[datetime] = []
    i = 0
    while i < len(series):
        t, o = series[i]
        cur = tl.offset_at(t)
        if abs(o - cur) >= TZ_DETECT_MIN_DIFF and i + TZ_DETECT_MIN_RUN <= len(series) \
                and all(series[j][1] == o for j in range(i, i + TZ_DETECT_MIN_RUN)):
            vf = from_min(t)
            ins = session.execute(
                text(
                    """INSERT INTO tz_periods (user_id, valid_from, fixed_offset_min, source)
                       VALUES (:u, :v, :o, 'device') ON CONFLICT (user_id, valid_from) DO NOTHING RETURNING id"""
                ),
                {"u": user_id, "v": vf, "o": o},
            ).scalar_one_or_none()
            if ins is not None:
                added.append(vf)
                tl = load_timeline(session, user_id)
        i += 1
    return tl, added


def bump_tz_version(session: Session, user_id: int) -> int:
    return int(session.execute(
        text("UPDATE user_settings SET tz_version = tz_version + 1 WHERE user_id=:u RETURNING tz_version"),
        {"u": user_id},
    ).scalar_one())


def tz_periods_view(session: Session, user_id: int) -> list[dict]:
    rows = session.execute(
        text("SELECT valid_from, tz_name, fixed_offset_min, source FROM tz_periods WHERE user_id=:u ORDER BY valid_from"),
        {"u": user_id},
    ).all()
    from .db import NEG_INF
    out = []
    for vf, name, fixed, src in rows:
        tz = name if name else f"UTC{'+' if fixed >= 0 else '-'}{abs(fixed) // 60:02d}:{abs(fixed) % 60:02d}"
        out.append({"valid_from": None if vf <= NEG_INF else vf.isoformat().replace("+00:00", "Z"), "tz": tz,
                    "source": src})
    return out


# ---------------------------------------------------------------- чанк

@dataclass
class ChunkResult:
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    rejected: int = 0
    min_ts: int | None = None   # эпох-минуты
    max_ts: int | None = None
    dates: list = field(default_factory=list)
    tz_changed: bool = False


def process_records(
    session: Session, user_id: int, upload_id: int, recs: list[Rec]
) -> ChunkResult:
    """Запись одной пачки записей и пересчёт. Вызывать под lock_user, в транзакции чанка."""
    res = ChunkResult()
    if not recs:
        return res
    dev, prio_changed = _resolve_devices(session, user_id, recs)
    steps = [r for r in recs if r.type == "steps"]
    hr = [r for r in recs if r.type == "heart_rate"]
    sleep = [r for r in recs if r.type == "sleep"]
    if steps:
        _write_steps(session, user_id, upload_id, steps, dev)
    if hr:
        _write_hr(session, user_id, upload_id, hr, dev)
    if sleep:
        _write_sleep(session, user_id, upload_id, sleep, dev)
    for r in recs:
        if r.status == "inserted":
            res.inserted += 1
        elif r.status == "updated":
            res.updated += 1
        else:
            res.duplicate += 1

    data = steps + hr + sleep
    mins: list[int] = []
    for r in steps:
        mins += [r.start, r.end - 1]
    for r in sleep:
        mins += [r.start, r.end - 1]
    for r in hr:
        mins.append(r.start // 60)
    if mins:
        res.min_ts, res.max_ts = min(mins), max(mins)

    tl = load_timeline(session, user_id)
    tl, added = detect_device_tz(session, user_id, data, dev, tl)
    changed_data = any(r.status in ("inserted", "updated") for r in data)

    if prio_changed:
        jobs.enqueue(session, "recompute_days", user_id, {"from": None}, dedupe_key=f"recompute:{user_id}:all")

    if added:
        res.tz_changed = True
        tzv = bump_tz_version(session, user_id)
        recompute_from(session, user_id, min(added), tl)
        publish_event(session, user_id, "settings.tz_changed", {
            "timezone_periods": tz_periods_view(session, user_id), "reason": "device_offset", "tz_version": tzv,
        })
        recompute_dates_all = True
    else:
        recompute_dates_all = False

    if changed_data or added:
        sleep_mins = [m for r in sleep for m in (r.start, r.end - 1)]
        dset = dates_of_minutes(tl, mins)
        dset |= dates_of_minutes(tl, sleep_mins, spread_days=1)
        rows = recompute_dates(session, user_id, dset, tl)
        as_of = as_of_date(session, user_id, tl)
        goal_dates = set(dset)
        if recompute_dates_all:
            goal_dates |= {d for d in dates_between(min(dset), max(dset))}
        evaluate_goals(session, user_id, goal_dates, as_of)
        res.dates = sorted(d.isoformat() for d in dset)
    return res


# ---------------------------------------------------------------- файл

def count_lines(path: str) -> int:
    n = 0
    last = b"\n"
    with open(path, "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            n += buf.count(b"\n")
            last = buf[-1:]
    if last != b"\n":
        n += 1
    return n


def read_chunk(path: str, offset: int, max_lines: int) -> tuple[list[bytes], int]:
    """-> (строки без перевода строки, новый byte_offset). Строка > лимита возвращается усечённой + флагом."""
    lim = get_settings().max_line_bytes
    lines: list[bytes] = []
    with open(path, "rb") as f:
        f.seek(offset)
        while len(lines) < max_lines:
            ln = f.readline(lim + 2)
            if not ln:
                break
            if len(ln) >= lim + 2 and not ln.endswith(b"\n"):
                # слишком длинная строка — дочитываем и отбрасываем, оставляя маркер
                while True:
                    rest = f.readline(65536)
                    if not rest or rest.endswith(b"\n"):
                        break
                lines.append(b"x" * (lim + 1))
                continue
            lines.append(ln.rstrip(b"\r\n"))
        return lines, f.tell()


_last_progress: dict[int, float] = {}


def _counters(up: Any) -> dict:
    return {"inserted": up["rows_inserted"], "updated": up["rows_updated"],
            "duplicate": up["rows_duplicate"], "rejected": up["rows_rejected"]}


def _progress_payload(up: dict) -> dict:
    total = up["lines_total"] or 0
    pct = 100 if up["status"] in ("done", "done_with_errors") else (
        min(100, int(up["lines_done"] * 100 / total)) if total else 0)
    return {"upload_id": up["id"], "status": up["status"], "lines_done": up["lines_done"], "lines_total": total,
            "percent": pct, "rows_inserted": up["rows_inserted"], "rows_updated": up["rows_updated"],
            "rows_duplicate": up["rows_duplicate"], "rows_rejected": up["rows_rejected"]}


def _get_upload(session: Session, upload_id: int) -> dict | None:
    r = session.execute(text("SELECT * FROM uploads WHERE id=:id"), {"id": upload_id}).mappings().first()
    return dict(r) if r else None


def _finish(session: Session, up: dict, status: str, error: str | None = None) -> None:
    session.execute(
        text("UPDATE uploads SET status=:s, finished_at=now(), error=:e WHERE id=:id"),
        {"s": status, "e": error, "id": up["id"]},
    )
    up = _get_upload(session, up["id"]) or up
    if status in ("done", "done_with_errors"):
        publish_event(session, up["user_id"], "upload.progress", _progress_payload(up))
        publish_event(session, up["user_id"], "upload.done", {
            "upload_id": up["id"], "status": status, "counters": _counters(up)})
        jobs.enqueue(session, "evaluate_rules", up["user_id"], {"upload_id": up["id"]},
                     dedupe_key=f"eval:{up['user_id']}")
    else:
        publish_event(session, up["user_id"], "upload.failed", {
            "upload_id": up["id"], "status": status, "counters": _counters(up), "error": error})


def mark_upload_failed(upload_id: int, error: str) -> None:
    with new_session() as s:
        up = _get_upload(s, upload_id)
        if up and up["status"] in ("queued", "processing"):
            _finish(s, up, "failed", error)
        s.commit()


def process_upload(job: dict) -> None:
    """Обработчик задания process_upload (раздел 11)."""
    cfg = get_settings()
    upload_id = int(job["payload"]["upload_id"])
    with new_session() as s:
        up = _get_upload(s, upload_id)
        if up is None or up["status"] not in ("queued", "processing"):
            return
        if up["cancel_requested"]:
            _finish(s, up, "cancelled")
            s.commit()
            return
        path = up["path"]
        if not os.path.exists(path):
            raise jobs.JobFatal("файл загрузки не найден на диске")
        if up["lines_total"] is None:
            total = count_lines(path)
            s.execute(text("UPDATE uploads SET lines_total=:t WHERE id=:id"), {"t": total, "id": upload_id})
        s.execute(text("UPDATE uploads SET status='processing', started_at=coalesce(started_at, now()) WHERE id=:id"),
                  {"id": upload_id})
        s.commit()

    while True:
        with new_session() as s:
            up = _get_upload(s, upload_id)
            if up is None:
                return
            if up["cancel_requested"]:
                _finish(s, up, "cancelled")
                s.commit()
                return
            try:
                lines, new_off = read_chunk(up["path"], up["byte_offset"], cfg.chunk_lines)
            except OSError as e:
                raise jobs.JobFatal(f"не удалось прочитать файл: {e}") from e
            if not lines and new_off == up["byte_offset"]:
                _finish(s, up, "done_with_errors" if up["rows_rejected"] > 0 else "done")
                jobs.heartbeat(s, job["id"])
                s.commit()
                return

            recs: list[Rec] = []
            errors: list[tuple[int, str, str]] = []
            for i, raw in enumerate(lines):
                line_no = up["lines_done"] + i + 1
                try:
                    r = parse_line(raw)
                except LineError as e:
                    errors.append((line_no, e.code, e.message))
                    continue
                if r is not None:
                    r.line_no = line_no
                    recs.append(r)

            lock_user(s, up["user_id"])
            res = process_records(s, up["user_id"], upload_id, recs)
            res.rejected = len(errors)
            if errors:
                stored = s.execute(text("SELECT count(*) FROM upload_errors WHERE upload_id=:id"),
                                   {"id": upload_id}).scalar_one()
                for ln, code, msg in errors[: max(0, MAX_STORED_ERRORS - stored)]:
                    s.execute(
                        text("""INSERT INTO upload_errors (upload_id, line_no, code, message)
                                VALUES (:id, :l, :c, :m) ON CONFLICT DO NOTHING"""),
                        {"id": upload_id, "l": ln, "c": code, "m": msg},
                    )
            s.execute(
                text(
                    """UPDATE uploads SET byte_offset=:off, lines_done = lines_done + :n,
                         rows_inserted = rows_inserted + :i, rows_updated = rows_updated + :u,
                         rows_duplicate = rows_duplicate + :d, rows_rejected = rows_rejected + :r,
                         min_ts = least(min_ts, CASE WHEN CAST(:mn AS bigint) IS NULL THEN NULL ELSE to_timestamp(CAST(:mn AS bigint) * 60) END),
                         max_ts = greatest(max_ts, CASE WHEN CAST(:mx AS bigint) IS NULL THEN NULL ELSE to_timestamp(CAST(:mx AS bigint) * 60) END)
                       WHERE id=:id"""
                ),
                {"off": new_off, "n": len(lines), "i": res.inserted, "u": res.updated, "d": res.duplicate,
                 "r": res.rejected, "mn": res.min_ts, "mx": res.max_ts, "id": upload_id},
            )
            up2 = _get_upload(s, upload_id)
            if res.dates:
                publish_event(s, up["user_id"], "summary.updated", {"dates": res.dates, "upload_id": upload_id})
            now_t = time.monotonic()
            if now_t - _last_progress.get(upload_id, 0) >= 0.5:
                _last_progress[upload_id] = now_t
                publish_event(s, up["user_id"], "upload.progress", _progress_payload(up2))
            jobs.heartbeat(s, job["id"])
            s.commit()
