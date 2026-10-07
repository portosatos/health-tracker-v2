"""Тесты генератора данных (без БД). Запуск: pytest backend/tests/test_generator.py"""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
import tracemalloc
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("ht_generator_t", ROOT / "backend" / "app" / "generator.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ht_generator_t"] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load()
GenParams = gen.GenParams
TZ = "Asia/Almaty"
D0, D1 = date(2026, 9, 1), date(2026, 9, 30)


def P(**kw):
    base = dict(date_from=D0, date_to=D1, tz=TZ, seed=1)
    base.update(kw)
    return GenParams(**base)


def recs(params, **kw):
    return [json.loads(x) for x in gen.iter_lines(params, **kw)]


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ldate(params, s: str) -> date:
    """Локальная дата момента по таймлайну генератора."""
    plan = gen.build_plan(params)
    return plan.timeline.local_date(int(ts(s).timestamp()))


def sleep_by_day(params, rs=None):
    """Минуты сна (union по времени, без awake) по дате пробуждения."""
    rs = rs if rs is not None else recs(params)
    ivs = sorted((ts(r["start"]), ts(r["end"])) for r in rs if r["type"] == "sleep" and r["stage"] != "awake")
    merged = []
    for s, e in ivs:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    plan = gen.build_plan(params)
    res = defaultdict(int)
    for s, e in merged:
        res[plan.timeline.local_date(int(e.timestamp()) - 60)] += int((e - s).total_seconds() // 60)
    return res


def steps_by_day(params, device="watch-01"):
    plan = gen.build_plan(params)
    res = defaultdict(int)
    for r in recs(params):
        if r["type"] == "steps" and r["device_id"] == device:
            res[plan.timeline.local_date(int(ts(r["start"]).timestamp()))] += r["count"]
    return res


# ---------------------------------------------------------------- формат и детерминизм

def test_deterministic_by_seed():
    a, b = list(gen.iter_lines(P())), list(gen.iter_lines(P()))
    assert a == b and len(a) > 1000
    assert list(gen.iter_lines(P(seed=2))) != a


def test_same_date_same_lines_in_any_subperiod():
    """Данные за дату не зависят от границ периода (основа проверки идемпотентности)."""
    full = recs(P(), date_from=date(2026, 9, 10), date_to=date(2026, 9, 12))
    other = list(gen.iter_lines(P(date_from=date(2026, 9, 5), date_to=date(2026, 9, 20))))
    sub = list(gen.iter_lines(P(date_from=date(2026, 9, 5), date_to=date(2026, 9, 20)),
                              date_from=date(2026, 9, 10), date_to=date(2026, 9, 12)))
    assert [json.loads(x) for x in sub] == full
    assert set(sub) <= set(other)


def test_format_valid():
    rs = recs(P(presets=("data-gaps", "timezone-trip")))
    kinds = defaultdict(int)
    for r in rs:
        kinds[r["type"]] += 1
        assert r["v"] == 1 and r["device_id"] in ("watch-01", "phone-01")
        if r["type"] == "device":
            assert r["kind"] in ("watch", "phone")
            continue
        assert -720 <= r["offset_min"] <= 840
        if r["type"] == "heart_rate":
            assert 25 <= r["bpm"] <= 250 and r["ts"].endswith("Z")
        else:
            s, e = ts(r["start"]), ts(r["end"])
            assert s.second == 0 and e.second == 0 and s < e
            if r["type"] == "sleep":
                assert r["stage"] in ("asleep", "light", "deep", "rem", "awake")
                assert (e - s) <= timedelta(hours=16)
            else:
                assert r["count"] >= 0 and 1 <= (e - s).total_seconds() / 60 <= 360
        assert len(json.dumps(r)) < 4096
    assert set(kinds) == {"device", "sleep", "steps", "heart_rate"}


def test_only_watch_has_no_phone_lines():
    rs = recs(P(devices=("watch",)))
    assert {r["device_id"] for r in rs} == {"watch-01"}


def test_bad_params():
    with pytest.raises(ValueError):
        gen.iter_lines(P(date_from=D1, date_to=D0))
    with pytest.raises(ValueError):
        gen.iter_lines(P(presets=("нет-такого",)))
    with pytest.raises(ValueError):
        gen.iter_lines(P(tz="Mars/Olympus"))


# ---------------------------------------------------------------- пересечения часы+телефон

def test_watch_and_phone_overlapping_intervals_with_different_counts():
    rs = recs(P())
    w = [(ts(r["start"]), ts(r["end"]), r["count"]) for r in rs if r["type"] == "steps" and r["device_id"] == "watch-01"]
    ph = [(ts(r["start"]), ts(r["end"]), r["count"]) for r in rs if r["type"] == "steps" and r["device_id"] == "phone-01"]
    assert w and ph
    day = [x for x in w if x[0].date() == date(2026, 9, 10)]
    found = 0
    for ps, pe, pc in ph:
        if ps.date() != date(2026, 9, 10):
            continue
        for ws, we, wc in day:
            if ps < we and ws < pe and pc != wc and wc > 500:
                found += 1
    assert found > 0
    # а на прогулке числа заметно разные, но одного порядка
    big = [(ps, pe, pc) for ps, pe, pc in ph if pc > 800]
    assert big


# ---------------------------------------------------------------- пропуски

def _local_dates(params, rs, rtype):
    plan = gen.build_plan(params)
    out = []
    for r in rs:
        if r["type"] == rtype:
            key = r["ts"] if rtype == "heart_rate" else r["start"]
            out.append((plan.timeline.local_date(int(ts(key).timestamp())), r))
    return out


def test_gap_days_have_no_records_and_no_zeros():
    gaps = (date(2026, 9, 7), date(2026, 9, 8))
    params = P(gap_days=gaps)
    rs = recs(params)
    for d, r in _local_dates(params, rs, "steps"):
        assert d not in gaps
    # от дня пропуска остаётся лишь хвост вечерней ночи (принадлежит следующему дню): пульс только в последние 2 часа
    for d, r in _local_dates(params, rs, "heart_rate"):
        if d in gaps:
            assert (ts(r["ts"]) + timedelta(hours=5)).hour >= 22
    # ночи, заканчивающиеся в день пропуска, отсутствуют
    sl = sleep_by_day(params, rs)
    for g in gaps:
        assert sl.get(g, 0) == 0
    # соседние дни на месте
    assert sl[date(2026, 9, 6)] > 300 and sl[date(2026, 9, 9)] > 300
    # покрытие дня пропуска: меньше 120 минут (хвост вечерней ночи накануне следующего дня)
    plan = gen.build_plan(params)
    minutes = set()
    for r in rs:
        if r["type"] in ("sleep", "steps"):
            a, b = int(ts(r["start"]).timestamp()), int(ts(r["end"]).timestamp())
            for m in range(a // 60, b // 60):
                if plan.timeline.local_date(m * 60) == date(2026, 9, 7):
                    minutes.add(m)
    assert len(minutes) < 120
    # ни одной записи с нулём в пропуске: все шаговые записи вокруг не нулевые для пропущенных дат нет вообще
    baseline = set(gen.iter_lines(P()))
    assert set(gen.iter_lines(params)) <= baseline          # ничего не добавлено, только убрано


def test_random_gap_fraction_removes_days():
    params = P(gap_day_fraction=0.3, seed=5)
    plan = gen.build_plan(params)
    assert 3 <= len(plan.gap_dates) <= 15
    present = {d for d, _ in _local_dates(params, recs(params), "steps")}
    assert not (present & plan.gap_dates)
    assert len(present) == 30 - len(plan.gap_dates)


def test_hour_gaps_inside_day_are_missing_not_zero():
    params = P(hour_gap_prob=1.0, hour_gap_max_hours=3, date_to=date(2026, 9, 6))
    base = set(gen.iter_lines(P(date_to=date(2026, 9, 6))))
    cut = set(gen.iter_lines(params))
    removed = [json.loads(x) for x in base - cut]
    assert cut <= base, "пропуск не должен добавлять строк (в т.ч. нулевых)"
    assert removed
    plan = gen.build_plan(params)
    per_day = defaultdict(list)
    for r in removed:
        s = ts(r.get("start") or r["ts"])
        per_day[plan.timeline.local_date(int(s.timestamp()))].append(s)
    assert len(per_day) == 6
    for d, lst in per_day.items():
        span = (max(lst) - min(lst)).total_seconds()
        assert span <= 3 * 3600 + 30 * 60   # одно окно не длиннее 3 часов (+ интервалы, начавшиеся до окна)
        h = (min(lst) + timedelta(minutes=5 * 60)).hour   # Almaty = UTC+5
        assert 8 <= h <= 22


def test_explicit_hour_gap():
    d = date(2026, 9, 10)
    params = P(hour_gaps=((d, 10, 13),))
    plan = gen.build_plan(params)
    for r in recs(params):
        if r["type"] in ("steps", "heart_rate"):
            s = ts(r.get("start") or r["ts"])
            e = ts(r["end"]) if r["type"] == "steps" else s
            if plan.timeline.local_date(int(s.timestamp())) == d:
                loc = s + timedelta(hours=5)
                loc_e = e + timedelta(hours=5)
                assert not (loc.hour >= 10 and loc.hour < 13) and not (loc_e.hour in (10, 11, 12) and loc_e.minute > 0)


def test_device_absent_days():
    params = P(absent_device="phone", absent_days=5, absent_from=date(2026, 9, 10))
    rs = recs(params)
    plan = gen.build_plan(params)
    absent = {date(2026, 9, 10) + timedelta(days=i) for i in range(5)}
    assert any(r["type"] == "device" and r["device_id"] == "phone-01" for r in rs)
    phone_days = set()
    watch_days = set()
    for r in rs:
        if r["type"] == "steps":
            d = plan.timeline.local_date(int(ts(r["start"]).timestamp()))
            (phone_days if r["device_id"] == "phone-01" else watch_days).add(d)
    assert not (phone_days & absent)
    assert absent <= watch_days
    assert date(2026, 9, 9) in phone_days and date(2026, 9, 15) in phone_days
    # ночь без телефона: сон часов на месте
    sl_phone = [r for r in rs if r["type"] == "sleep" and r["device_id"] == "phone-01"
                and plan.timeline.local_date(int(ts(r["end"]).timestamp()) - 60) in absent]
    assert not sl_phone


def test_absent_watch_leaves_phone_sleep_and_no_hr():
    params = P(absent_device="watch", absent_days=4, absent_from=date(2026, 9, 10))
    plan = gen.build_plan(params)
    absent = {date(2026, 9, 10) + timedelta(days=i) for i in range(4)}
    rs = recs(params)
    sl = sleep_by_day(params, rs)
    for d in absent:
        assert sl[d] > 300                     # сон дал телефон
    for d, r in _local_dates(params, rs, "heart_rate"):
        if d in absent:                        # пульс только с часов; остаётся лишь вечерний хвост ночи следующего дня
            assert (ts(r["ts"]) + timedelta(hours=5)).hour >= 22


# ---------------------------------------------------------------- сон

def test_night_crosses_midnight_and_belongs_to_wake_day():
    params = P()
    plan = gen.build_plan(params)
    rs = recs(params)
    asleep = [r for r in rs if r["type"] == "sleep" and r["device_id"] == "watch-01" and r["stage"] != "awake"]
    crossing = 0
    for r in asleep:
        s = plan.timeline.local_date(int(ts(r["start"]).timestamp()))
        e = plan.timeline.local_date(int(ts(r["end"]).timestamp()) - 60)
        if s < e:
            crossing += 1
    assert crossing >= 25             # по одной стадии на ночь пересекает полночь -> день пробуждения другой
    sl = sleep_by_day(params, rs)
    for d in range(2, 29):
        v = sl[date(2026, 9, d)]
        assert 360 <= v <= 620, (d, v)         # одна ночь на дату, не две


def test_sleep_union_has_overlap_between_watch_and_phone():
    rs = recs(P())
    w = [(ts(r["start"]), ts(r["end"])) for r in rs if r["type"] == "sleep" and r["device_id"] == "watch-01"]
    ph = [(ts(r["start"]), ts(r["end"])) for r in rs if r["type"] == "sleep" and r["device_id"] == "phone-01"]
    assert ph and any(ps < we and ws < pe for ps, pe in ph for ws, we in w)


def test_preset_sleep_over_midnight():
    params = P(presets=("sleep-over-midnight",))
    plan = gen.build_plan(params)
    rs = recs(params)
    same_day = total = 0
    for r in rs:
        if r["type"] == "sleep" and r["device_id"] == "phone-01" and r["stage"] == "asleep":
            total += 1
            a = plan.timeline.local_date(int(ts(r["start"]).timestamp()))
            b = plan.timeline.local_date(int(ts(r["end"]).timestamp()) - 60)
            same_day += (a == b)
    assert total and same_day / total > 0.7        # начало после полуночи -> та же дата
    sl = sleep_by_day(params, rs)
    assert all(360 <= sl[date(2026, 9, d)] <= 620 for d in range(2, 29))


# ---------------------------------------------------------------- поездка

def test_trip_changes_offset_and_day_length():
    params = P(presets=("timezone-trip",))
    rs = recs(params)
    offs = [(ts(r.get("start") or r["ts"]), r["offset_min"]) for r in rs if r["type"] != "device"]
    offs.sort()
    assert {o for _, o in offs} == {300, 360}
    flips = sum(1 for (_, a), (_, b) in zip(offs, offs[1:]) if a != b)
    assert flips == 1                       # смена ровно один раз, посреди периода
    change_at = next(t for (t, a), (_, b) in zip(offs, offs[1:]) if a != b)
    assert datetime(2026, 9, 10, tzinfo=timezone.utc) < change_at < datetime(2026, 9, 25, tzinfo=timezone.utc)
    plan = gen.build_plan(params)
    trip = plan.p.trip_date
    assert gen.day_length_min(params, trip) == 1380          # на восток: 23 часа
    assert gen.day_length_min(params, trip - timedelta(days=1)) == 1440
    assert gen.day_length_min(params, trip + timedelta(days=1)) == 1440
    # ничего не потеряно: каждая UTC-минута ровно одной локальной даты -> сны и шаги в диапазонах
    sl = sleep_by_day(params, rs)
    assert all(sl[date(2026, 9, d)] >= 360 for d in range(2, 29))


def test_trip_west_25_hour_day():
    params = P(presets=("timezone-trip",), tz="Asia/Omsk", trip_tz="Asia/Almaty")
    plan = gen.build_plan(params)
    assert gen.day_length_min(params, plan.p.trip_date) == 1500


# ---------------------------------------------------------------- пресеты правил

def test_preset_sleep_short_streak():
    params = P(presets=("sleep-short-streak",))
    sl = sleep_by_day(params)
    last3 = [date(2026, 9, d) for d in (28, 29, 30)]
    assert all(60 <= sl[d] < 360 for d in last3), [sl[d] for d in last3]
    assert all(sl[date(2026, 9, d)] >= 400 for d in range(2, 28))


def test_preset_steps_low_streak():
    params = P(presets=("steps-low-streak",))
    st = steps_by_day(params)
    assert all(st[date(2026, 9, d)] < 4000 for d in (28, 29, 30))
    normal = [st[date(2026, 9, d)] for d in range(2, 28)]
    assert min(normal) > 4000 and median(normal) > 8000
    # покрытие полное: шаги покрывают 12+ часов
    plan = gen.build_plan(params)
    minutes = set()
    for r in recs(params):
        if r["type"] in ("steps", "sleep"):
            a, b = int(ts(r["start"]).timestamp()), int(ts(r["end"]).timestamp())
            minutes.update(m for m in range(a // 60, b // 60) if plan.timeline.local_date(m * 60) == D1)
    assert len(minutes) >= 720


def _hr_series(params):
    plan = gen.build_plan(params)
    res = defaultdict(list)
    for r in recs(params):
        if r["type"] == "heart_rate":
            res[plan.timeline.local_date(int(ts(r["ts"]).timestamp()))].append((ts(r["ts"]), r["bpm"]))
    return res


def test_preset_resting_hr_elevated():
    params = P(presets=("resting-hr-elevated",))
    hr = _hr_series(params)

    def low(d):
        v = sorted(b for _, b in hr[d])
        return median(v[: max(1, len(v) // 10)])

    base = median(low(date(2026, 9, d)) for d in range(2, 27))
    assert all(low(date(2026, 9, d)) >= base + 7 for d in (28, 29, 30))
    # изредка ночь без часов (watch не надеты) даёт выброс; в остальном нормальные дни около базы
    normal = [low(date(2026, 9, d)) for d in range(2, 27)]
    assert sum(1 for v in normal if v < base + 5) >= 0.9 * len(normal)


def test_preset_peak_hr_high_and_normal_days_stay_below():
    params = P(presets=("peak-hr-high",))
    hr = _hr_series(params)
    hot = sum(1 for _, b in hr[D1] if b >= 170)
    assert hot >= 20          # точки каждые 30 с -> 10+ минут
    for d in range(2, 30):
        assert sum(1 for _, b in hr[date(2026, 9, d)] if b >= 170) == 0


def test_preset_steps_week_drop():
    params = P(presets=("steps-week-drop",), date_from=date(2026, 8, 3), date_to=date(2026, 9, 30))
    st = steps_by_day(params)
    # последняя закрытая неделя к 30.09 (ср): 21..27.09; предыдущая 14..20.09
    w = [st[date(2026, 9, 21) + timedelta(days=i)] for i in range(7)]
    prev = [st[date(2026, 9, 14) + timedelta(days=i)] for i in range(7)]
    assert sum(w) / 7 <= 0.7 * sum(prev) / 7


def test_preset_sleep_debt_week():
    params = P(presets=("sleep-debt-week",), date_from=date(2026, 8, 3), date_to=date(2026, 9, 30))
    sl = sleep_by_day(params)
    week = [sl[date(2026, 9, 21) + timedelta(days=i)] for i in range(7)]
    assert 360 <= min(week) and sum(week) / 7 <= 420
    prev = [sl[date(2026, 9, 14) + timedelta(days=i)] for i in range(7)]
    assert sum(prev) / 7 > 430


def test_preset_names_with_underscores_and_listing():
    assert gen.describe_presets()
    for name in gen.describe_presets():
        gen.iter_lines(P(presets=(name.replace("-", "_"),)))
    # каждый пресет реально генерирует без ошибок на периоде в 6 недель
    for name in gen.describe_presets():
        n = sum(1 for _ in gen.iter_lines(P(presets=(name,), date_from=date(2026, 8, 17), date_to=date(2026, 9, 30))))
        assert n > 1000


# ---------------------------------------------------------------- потоковость и разбиение

def test_streaming_year_is_generator_and_low_memory():
    params = P(date_from=date(2025, 10, 1), date_to=date(2026, 9, 30))
    it = gen.iter_lines(params)
    assert inspect.isgenerator(it)
    assert next(it).startswith('{"v":1,"type":"device"')
    tracemalloc.start()
    n = 1
    for _ in it:                       # не накапливаем
        n += 1
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert n > 100_000                 # год с 15-минутными шагами и пульсом раз в 5 минут
    assert peak < 20 * 1024 * 1024, f"пик памяти {peak / 1e6:.1f} МБ — данные накапливаются?"


def test_split_with_overlap_is_identical_on_shared_dates():
    params = P(presets=("data-gaps",))
    ranges = gen.split_ranges(params, 3, overlap_days=2)
    assert len(ranges) == 3 and ranges[0][0] == D0 and ranges[-1][1] == D1
    assert ranges[1][0] < ranges[0][1] + timedelta(days=1)       # перекрытие
    full = set(gen.iter_lines(params))
    parts = [set(gen.iter_lines(params, a, b)) for a, b in ranges]
    assert set().union(*parts) == full
    a, b = parts[0], parts[1]
    overlap = a & b
    assert len(overlap) > 100           # общие строки совпадают дословно (одна дата -> те же значения)


def test_cli_smoke(tmp_path):
    out = tmp_path / "f.ndjson"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate.py"), "--from", "2026-09-01", "--to", "2026-09-10",
         "--tz", TZ, "--seed", "1", "--preset", "sleep-short-streak", "--out", str(out)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = out.read_bytes().split(b"\n")
    assert lines[-1] == b"" and b"\r" not in out.read_bytes()
    assert [json.loads(x) for x in lines[:-1]] == recs(P(date_to=date(2026, 9, 10), presets=("sleep-short-streak",)))
    # stdout
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py"), "--from", "2026-09-01",
                        "--to", "2026-09-02", "--devices", "watch"], capture_output=True)
    assert r.returncode == 0 and b"phone-01" not in r.stdout and b"\r" not in r.stdout
    # split
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py"), "--from", "2026-09-01", "--to",
                        "2026-09-10", "--split", "2", "--overlap-days", "2", "--out", str(tmp_path / "x.ndjson")],
                       capture_output=True, text=True)
    assert r.returncode == 0 and (tmp_path / "x.part1.ndjson").exists() and (tmp_path / "x.part2.ndjson").exists()
    # ошибка
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate.py"), "--from", "2026-09-10",
                        "--to", "2026-09-01"], capture_output=True, text=True)
    assert r.returncode == 2
