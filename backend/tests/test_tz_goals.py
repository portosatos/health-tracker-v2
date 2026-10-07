"""Часовые пояса, цели и уведомления (DESIGN, разделы 5 и 8)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import (
    daily, dev, full_day, new_client, register, run_worker, sql, steps, upload,
)


def _trip_lines(off_before: int, off_after: int, switch="2026-03-05 12:00"):
    """1 шаг в минуту, 6-часовые интервалы с 03-04 12:00Z до 03-07 12:00Z; смещение меняется в момент switch."""
    out = [dev("watch-01")]
    t = datetime(2026, 3, 4, 12, tzinfo=timezone.utc)
    end = datetime(2026, 3, 7, 12, tzinfo=timezone.utc)
    sw = datetime.strptime(switch, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    while t < end:
        n = t + timedelta(hours=6)
        out.append(steps("watch-01", t.strftime("%Y-%m-%d %H:%M"), n.strftime("%Y-%m-%d %H:%M"), 360,
                         off_after if t >= sw else off_before))
        t = n
    return out


@pytest.mark.parametrize("before,after,length", [(180, 300, 1320), (180, 120, 1500), (300, 180, 1560)])
def test_tz_travel_day_length_no_loss_no_dup(client, before, after, length):
    tz = {180: "Europe/Moscow", 300: "Asia/Yekaterinburg"}[before]
    register(client, "trip", tz=tz)
    lines = _trip_lines(before, after)
    upload(client, lines)
    days = client.get("/api/summary/daily/range?from=2026-03-04&to=2026-03-07").json()["days"]
    assert [d["day_length_min"] for d in days if d["date"] == "2026-03-05"] == [length]
    # инвариант: сумма по дням == сумме итоговых минут (1 шаг/мин * 3 суток)
    assert sum(d["steps"]["value"] or 0 for d in days) == 3 * 1440
    assert sql("SELECT sum(steps) FROM steps_minute")[0][0] == 3 * 1440
    # повторная загрузка ничего не меняет и не добавляет периодов
    nper = sql("SELECT count(*) FROM tz_periods")[0][0]
    upload(client, lines)
    assert sql("SELECT count(*) FROM tz_periods")[0][0] == nper == 2
    days2 = client.get("/api/summary/daily/range?from=2026-03-04&to=2026-03-07").json()["days"]
    assert days2 == days


def test_tz_device_offset_detection(client):
    register(client, "trip", tz="Europe/Moscow")
    upload(client, _trip_lines(180, 300))
    s = client.get("/api/settings").json()
    assert [p["source"] for p in s["timezone_periods"]] == ["initial", "device"]
    assert s["timezone_periods"][1]["tz"] == "UTC+05:00"
    assert sql("SELECT tz_version FROM user_settings")[0][0] == 1
    assert sql("SELECT count(*) FROM events WHERE type='settings.tz_changed'")[0][0] == 1
    # одиночная запись с другим смещением (меньше 3 подряд) пояс не меняет
    upload(client, [steps("watch-01", "2026-03-10 08:00", "2026-03-10 09:00", 60, off=0)])
    assert sql("SELECT count(*) FROM tz_periods")[0][0] == 2


def test_tz_device_follow_disabled(client):
    register(client, "trip", tz="Europe/Moscow")
    assert client.patch("/api/settings", json={"follow_device_tz": False}).json()["follow_device_tz"] is False
    upload(client, _trip_lines(180, 300))
    assert sql("SELECT count(*) FROM tz_periods")[0][0] == 1


def test_tz_settings_recompute(client):
    register(client, "msk", tz="Europe/Moscow")
    # 20:30Z-21:30Z = 23:30-00:30 МСК (пересекает полночь), но 01:30-02:30 Екб (целиком 03-03)
    upload(client, [dev("watch-01"), steps("watch-01", "2026-03-02 20:30", "2026-03-02 21:30", 60, off=180)])
    before = sql("SELECT local_date, wear_minutes FROM daily_summary ORDER BY 1")
    assert [(str(r[0]), r[1]) for r in before] == [("2026-03-02", 30), ("2026-03-03", 30)]
    r = client.put("/api/settings/timezone", json={"timezone": "Asia/Yekaterinburg", "scope": "all_history"})
    assert r.status_code == 202 and r.json()["timezone"] == "Asia/Yekaterinburg"
    assert sql("SELECT tz_version FROM user_settings")[0][0] == 1
    run_worker()
    after = sql("SELECT local_date, wear_minutes, tz_version FROM daily_summary ORDER BY 1")
    assert [str(r[0]) for r in after] == ["2026-03-03"]  # теперь вся запись целиком в 03-03
    assert after[0][1] == 60 and after[0][2] == 1  # минуты не потеряны и не удвоены
    assert daily(client, "2026-03-03")["stale"] is False
    assert sql("SELECT count(*) FROM events WHERE type='summary.updated'")[0][0] >= 2
    assert client.put("/api/settings/timezone", json={"timezone": "Not/AZone"}).status_code == 422


def test_tz_from_now_keeps_past(client):
    register(client, "msk", tz="Europe/Moscow")
    upload(client, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 1000)])
    before = daily(client, "2026-03-02")
    r = client.put("/api/settings/timezone", json={"timezone": "Asia/Tokyo", "effective_from": "2026-03-20T00:00:00Z"})
    assert r.status_code == 202
    run_worker()
    after = daily(client, "2026-03-02")
    assert after["steps"] == before["steps"] and after["stale"] is False
    assert [p["tz"] for p in client.get("/api/settings").json()["timezone_periods"]] == [
        "Europe/Moscow", "Asia/Tokyo"]


# ---------------------------------------------------------------- цели

def _goal_notes():
    return sql("SELECT id, payload FROM notifications WHERE kind='goal_achieved'")


def test_goal_notification_on_chunk_once(user):
    assert user.put("/api/goals/steps_per_day", json={"target": 5000}).json()["target"] == 5000
    lines = [dev("watch-01"), *full_day("watch-01", "2026-03-02", 8000)]
    upload(user, lines)
    assert len(_goal_notes()) == 1
    assert sql("SELECT count(*) FROM events WHERE type='goal.achieved'")[0][0] == 1
    upload(user, lines)  # повтор — ничего нового
    assert len(_goal_notes()) == 1
    assert sql("SELECT count(*) FROM events WHERE type='goal.achieved'")[0][0] == 1
    assert sql("SELECT count(*) FROM goal_achievements")[0][0] == 1
    notes = user.get("/api/notifications?unread=1").json()
    assert len(notes) == 1 and notes[0]["payload"]["goal_kind"] == "steps_per_day"
    p = user.get("/api/goals/progress?date=2026-03-02").json()[0]
    assert p["achieved"] and p["percent"] == 160 and p["status"] == "complete"
    assert user.post(f"/api/notifications/{notes[0]['id']}/read").status_code == 204
    assert user.get("/api/notifications?unread=1").json() == []


def test_goal_notification_before_upload_done(user, monkeypatch):
    """Уведомление появляется на чанке, до завершения файла."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "chunk_lines", 3)
    user.put("/api/goals/steps_per_day", json={"target": 5000})
    lines = [dev("watch-01"), *full_day("watch-01", "2026-03-02", 8000),
             *[steps("watch-01", f"2026-03-0{d} 06:00", f"2026-03-0{d} 06:10", 10) for d in (3, 4, 5, 6)]]
    upload(user, lines, run_worker=False)
    from app import worker
    assert worker.run_until_idle() >= 1
    ev = sql("SELECT type FROM events ORDER BY id")
    types = [e[0] for e in ev]
    assert "goal.achieved" in types and "upload.done" in types
    assert types.index("goal.achieved") < types.index("upload.done")
    assert types.count("summary.updated") >= 2  # сводка обновлялась по чанкам


def test_goal_revoked_then_reached_no_new_notification(user):
    user.put("/api/goals/steps_per_day", json={"target": 5000})
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 8000)])
    assert len(_goal_notes()) == 1
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 2000)])  # исправление вниз
    assert sql("SELECT revoked_at IS NOT NULL FROM goal_achievements")[0][0] is True
    assert user.get("/api/goals/achievements").json()[0]["revoked"] is True
    upload(user, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 9000)])
    assert sql("SELECT revoked_at IS NOT NULL FROM goal_achievements")[0][0] is False
    assert len(_goal_notes()) == 1


def test_goal_sleep_and_validation(user):
    assert user.put("/api/goals/sleep_minutes_per_day", json={"target": 420}).status_code == 200
    assert user.put("/api/goals/sleep_minutes_per_day", json={"target": 0}).status_code == 422
    assert user.put("/api/goals/bogus", json={"target": 5}).status_code == 404
    upload(user, [dev("watch-01"), sleep("watch-01", "2026-03-01 22:00", "2026-03-02 06:00")])
    assert len(_goal_notes()) == 1
    assert {g["kind"] for g in user.get("/api/goals").json()} == {"sleep_minutes_per_day"}


def test_goal_two_clients_one_notification():
    a, b = new_client(), new_client()
    register(a, "pair")
    b.cookies.set("ht_session", a.cookies.get("ht_session"))
    a.put("/api/goals/steps_per_day", json={"target": 1000})
    upload(a, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 3000)])
    assert len(b.get("/api/notifications").json()) == 1
    assert b.get("/api/goals").json()[0]["target"] == 1000


from .conftest import sleep  # noqa: E402
