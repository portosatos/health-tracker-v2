"""Приём, идемпотентность, слияние устройств, пропуски, сон (DESIGN, разделы 2-7)."""
from __future__ import annotations

from app.summary import merge_hr, merge_steps

from .conftest import (
    daily, day_after, dev, full_day, hr, ndjson, sleep, sql, steps, upload,
)


def _all_tables_snapshot():
    return {
        "steps": sql("SELECT device_id, minute_ts, steps FROM steps_minute ORDER BY 1,2"),
        "hr": sql("SELECT device_id, ts, bpm FROM hr_points ORDER BY 1,2"),
        "sleep": sql("SELECT device_id, start_ts, end_ts, stage FROM sleep_intervals ORDER BY 1,2,3"),
        "daily": sql("SELECT local_date, steps, sleep_minutes, wear_minutes FROM daily_summary ORDER BY 1"),
    }


BASE = [
    dev("watch-01", "watch"), dev("phone-01", "phone"),
    *full_day("watch-01", "2026-03-02", 8000),
    steps("phone-01", "2026-03-02 06:00", "2026-03-02 06:30", 500),
    hr("watch-01", "2026-03-02 06:10", 70),
    sleep("watch-01", "2026-03-01 22:00", "2026-03-02 05:00"),
]


def test_reupload_idempotent(user):
    u1 = upload(user, BASE)
    snap1 = _all_tables_snapshot()
    d1 = daily(user, "2026-03-02")
    u2 = upload(user, BASE)
    assert _all_tables_snapshot() == snap1
    assert daily(user, "2026-03-02")["steps"] == d1["steps"]
    a = user.get(f"/api/uploads/{u1}").json()
    b = user.get(f"/api/uploads/{u2}").json()
    assert a["status"] == "done" and a["counters"]["inserted"] == len(BASE)
    assert b["status"] == "done"
    assert b["counters"] == {"inserted": 0, "updated": 0, "duplicate": len(BASE), "rejected": 0}
    assert b["percent"] == 100


def test_partial_overlap_no_double_count(user):
    # 1 шаг/мин; файл A: 10:00-10:30, файл B: 10:15-10:45 (пересечение 15 минут)
    base = [dev("watch-01")] + full_day("watch-01", "2026-03-02", 0)
    upload(user, base + [steps("watch-01", "2026-03-02 05:00", "2026-03-02 05:30", 30)])
    upload(user, [steps("watch-01", "2026-03-02 05:15", "2026-03-02 05:45", 30)])
    d = daily(user, "2026-03-02")
    assert d["steps"]["value"] == 45  # объединение 05:00-05:45 по 1 шагу в минуту


def test_steps_watch_phone_no_sum(user):
    upload(user, [dev("watch-01", "watch"), dev("phone-01", "phone"),
                  *full_day("watch-01", "2026-03-02", 0),
                  steps("watch-01", "2026-03-02 06:00", "2026-03-02 06:30", 2100),
                  steps("phone-01", "2026-03-02 06:05", "2026-03-02 06:25", 1900)])
    assert daily(user, "2026-03-02")["steps"] == {"value": 2100, "status": "complete"}


def test_steps_watch_off_phone_used(user):
    # часы дали записи только до 06:15, телефон 06:05-06:25 -> на 06:15-06:25 побеждает телефон
    upload(user, [dev("watch-01", "watch"), dev("phone-01", "phone"),
                  *full_day("watch-01", "2026-03-02", 0),
                  steps("watch-01", "2026-03-02 06:00", "2026-03-02 06:15", 1500),
                  steps("phone-01", "2026-03-02 06:05", "2026-03-02 06:25", 1900)])
    assert daily(user, "2026-03-02")["steps"]["value"] == 1500 + 10 * 95


def test_steps_zero_watch_phone_positive(user):
    upload(user, [dev("watch-01", "watch"), dev("phone-01", "phone"),
                  *full_day("watch-01", "2026-03-02", 0),
                  steps("watch-01", "2026-03-02 06:00", "2026-03-02 06:30", 0),
                  steps("phone-01", "2026-03-02 06:00", "2026-03-02 06:30", 1800)])
    assert daily(user, "2026-03-02")["steps"]["value"] == 1800


def test_steps_split_exact_sum(user):
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 0),
                  steps("watch-01", "2026-03-02 05:00", "2026-03-02 05:07", 100)])
    assert sql("SELECT sum(steps) FROM steps_minute WHERE minute_ts >= '2026-03-02 05:00+00' "
               "AND minute_ts < '2026-03-02 05:07+00'")[0][0] == 100
    assert daily(user, "2026-03-02")["steps"]["value"] == 100


def test_merge_functions_pure():
    # шаги: приоритет на минуту; исключение «часы сняты»
    assert merge_steps([(1, 10, 5, 20), (2, 10, 9, 10)]) == {10: 5}
    assert merge_steps([(1, 10, 0, 20), (2, 10, 9, 10)]) == {10: 9}
    assert merge_steps([(1, 10, 0, 20), (2, 10, 0, 10)]) == {10: 0}
    # пульс: только устройство с наивысшим приоритетом; среднее half-up
    out = merge_hr([(1, 600, 70, 20), (1, 610, 71, 20), (2, 620, 150, 10), (2, 660, 80, 10)])
    assert out == {10: 71, 11: 80}  # (70+71)/2=70.5 -> 71; минута 11 только у телефона


def test_hr_minute_priority_device(user):
    pts = [hr("watch-01", "2026-03-02 06:10", 60), hr("phone-01", "2026-03-02 06:10", 120)]
    upload(user, [dev("watch-01", "watch"), dev("phone-01", "phone"), *pts,
                  *[hr("watch-01", f"2026-03-02 07:{m:02d}", 60) for m in range(0, 40)]])
    d = daily(user, "2026-03-02")
    assert d["hr"]["status"] == "ok" and d["hr"]["peak"] == 60  # телефон 120 проигнорирован


def test_gap_is_null_not_zero(user):
    upload(user, BASE)
    d = daily(user, "2026-03-10")  # дня нет вовсе
    assert d["steps"] == {"value": None, "status": "no_data"}
    assert d["sleep"]["minutes"] is None and d["hr"]["resting"] is None
    assert d["coverage"] == "none"


def test_partial_coverage_flag(user):
    upload(user, [dev("watch-01"), steps("watch-01", "2026-03-02 08:00", "2026-03-02 11:00", 300)])
    d = daily(user, "2026-03-02")  # 180 минут
    assert d["coverage"] == "partial" and d["steps"] == {"value": 300, "status": "partial"}
    upload(user, [dev("watch-01"), steps("watch-01", "2026-03-03 08:00", "2026-03-03 08:30", 30)])
    d = daily(user, "2026-03-03")  # < 120 минут -> нет данных
    assert d["coverage"] == "none" and d["steps"]["value"] is None


def test_zero_steps_complete_is_zero(user):
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 0)])
    assert daily(user, "2026-03-02")["steps"] == {"value": 0, "status": "complete"}


def test_sleep_assigned_to_wake_day(user):
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-01", 100), *full_day("watch-01", "2026-03-02", 100),
                  sleep("watch-01", "2026-03-01 22:40", "2026-03-02 05:25")])
    assert daily(user, "2026-03-01")["sleep"]["status"] == "no_data"
    d = daily(user, "2026-03-02")
    assert d["sleep"]["minutes"] == 405 and d["sleep"]["status"] == "ok"
    assert d["sleep"]["start"] == "2026-03-01T22:40:00Z" and d["sleep"]["end"] == "2026-03-02T05:25:00Z"


def test_sleep_across_midnight_utc_not_local(client):
    from .conftest import register
    register(client, "msk", tz="Europe/Moscow")
    # 20:00Z..04:00Z = 23:00..07:00 по Москве; проснулся 03-02 (и по UTC тоже), но
    # 22:30Z..01:30Z (01:30-04:30 МСК 03-02) — сон целиком в локальном дне 03-02, хотя в UTC он начался 03-01
    upload(client, [dev("watch-01"), sleep("watch-01", "2026-03-01 22:30", "2026-03-02 01:30", off=180)])
    assert daily(client, "2026-03-02")["sleep"]["minutes"] == 180
    assert daily(client, "2026-03-01")["sleep"]["status"] == "no_data"


def test_nap_same_day_and_union(user):
    upload(user, [dev("watch-01"), dev("phone-01", "phone"),
                  sleep("watch-01", "2026-03-01 22:00", "2026-03-02 03:00"),
                  sleep("phone-01", "2026-03-02 02:00", "2026-03-02 06:00"),  # пересечение -> union 22:00-06:00
                  sleep("watch-01", "2026-03-02 14:00", "2026-03-02 15:30")])
    assert daily(user, "2026-03-02")["sleep"]["minutes"] == 480 + 90


def test_sleep_sessions_gap_30min(user):
    # разрыв 40 минут — две сессии; день у обеих один; минуты суммируются без дыры
    upload(user, [dev("watch-01"),
                  sleep("watch-01", "2026-03-02 00:00", "2026-03-02 03:00"),
                  sleep("watch-01", "2026-03-02 03:40", "2026-03-02 06:00")])
    assert daily(user, "2026-03-02")["sleep"]["minutes"] == 180 + 140


def test_parse_bad_lines_counted(user):
    body = ndjson([dev("watch-01")]) + b"not json\n" + ndjson([
        {"v": 2, "type": "steps", "device_id": "w"},
        {"v": 1, "type": "weird", "device_id": "w"},
        {"v": 1, "type": "steps", "device_id": "w", "start": "2026-03-02T06:00:00Z"},
        {"v": 1, "type": "steps", "device_id": "w", "start": "2026-03-02T06:00:30Z", "end": "2026-03-02T06:10:00Z",
         "count": 5},
        {"v": 1, "type": "steps", "device_id": "w", "start": "2026-03-02T07:00:00Z", "end": "2026-03-02T06:10:00Z",
         "count": 5},
        {"v": 1, "type": "heart_rate", "device_id": "w", "ts": "2026-03-02T06:00:00Z", "bpm": 500},
        steps("watch-01", "2026-03-02 06:00", "2026-03-02 06:10", 10)]) + b"\n\n"
    uid = upload(user, body)
    st = user.get(f"/api/uploads/{uid}").json()
    assert st["status"] == "done_with_errors"
    assert st["counters"]["rejected"] == 7 and st["counters"]["inserted"] == 2
    errs = user.get(f"/api/uploads/{uid}/errors").json()
    codes = {e["line_no"]: e["code"] for e in errs}
    assert codes == {2: "bad_json", 3: "unsupported_version", 4: "unknown_type", 5: "missing_field",
                     6: "bad_time", 7: "bad_interval", 8: "bad_value"}


def test_unknown_version_rejected(user):
    uid = upload(user, [{"v": 3, "type": "device", "device_id": "x"}])
    assert user.get(f"/api/uploads/{uid}/errors").json()[0]["code"] == "unsupported_version"


def test_multipart_upload_and_filename(user):
    r = user.post("/api/uploads", files={"file": ("my data.ndjson", ndjson([dev("watch-01")]))})
    assert r.status_code == 202
    from .conftest import run_worker
    run_worker()
    assert user.get(f"/api/uploads/{r.json()['upload_id']}").json()["filename"] == "my data.ndjson"
    r = user.post("/api/uploads", content=ndjson([dev("w2")]),
                  headers={"Content-Type": "application/x-ndjson", "X-Filename": "%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D0%B5.ndjson"})
    assert user.get(f"/api/uploads/{r.json()['upload_id']}").json()["filename"] == "данные.ndjson"


def test_weekly_valid_days_and_compare(user):
    lines = [dev("watch-01")]
    d = "2026-03-02"  # понедельник
    for i in range(7):
        lines += full_day("watch-01", day_after(d, i), 8000 + 100 * i)
    upload(user, lines)
    w = user.get("/api/summary/weekly?week=2026-W10").json()
    assert w["status"] == "complete" and w["valid_days_steps"] == 7
    assert w["steps_avg"] == 8300.0
    assert w["comparison"]["steps"]["reason"] == "previous_no_data"
    assert not w["comparison"]["steps"]["comparable"]
    # предыдущая неделя: 3 валидных дня -> сравнение возможно по средним, а не суммам
    prev = [dev("watch-01")]
    for i in range(3):
        prev += full_day("watch-01", day_after("2026-02-23", i), 4000)
    upload(user, prev)
    w = user.get("/api/summary/weekly?date=2026-03-04").json()
    c = w["comparison"]["steps"]
    assert c["previous"] == 4000.0 and c["comparable"] and c["delta_abs"] == 4300.0
    prevw = user.get("/api/summary/weekly?week=2026-W09").json()
    assert prevw["status"] == "partial" and prevw["valid_days_steps"] == 3


def test_range_endpoint(user):
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 100)])
    r = user.get("/api/summary/daily/range?from=2026-03-01&to=2026-03-03").json()["days"]
    assert [x["steps"]["status"] for x in r] == ["no_data", "complete", "no_data"]
    assert user.get("/api/summary/daily/range?from=2026-01-01&to=2026-06-01").status_code == 422
