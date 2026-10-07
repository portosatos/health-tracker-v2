"""Почта: дайджест, тихие часы, отправка через SMTP-приёмник, повторы (DESIGN, раздел 10)."""
from __future__ import annotations

import socket
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import clock, jobs, mail
from app.db import new_session
from app.mail import quiet_hours, sender
from app.mail.smtp_sink import start_sink

from .conftest import new_client, register, run_worker, sql

UTC = timezone.utc


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.set_fake_now(None)


@pytest.fixture
def sink(monkeypatch):
    port = free_port()
    ctrl = start_sink(port)
    monkeypatch.setenv("HT_SMTP_PORT", str(port))
    yield ctrl
    ctrl.stop()


@pytest.fixture
def dead_smtp(monkeypatch):
    monkeypatch.setenv("HT_SMTP_PORT", str(free_port()))  # никто не слушает


def make_user(tz="Europe/Moscow", email="alice@example.com", quiet=None):
    c = new_client()
    register(c, "alice", tz)
    patch = {}
    if email:
        patch["email"] = email
    if quiet:
        patch["quiet_hours"] = {"enabled": True, "start": quiet[0], "end": quiet[1]}
    if patch:
        assert c.patch("/api/settings", json=patch).status_code == 200
    uid = sql("SELECT id FROM users WHERE login='alice'")[0][0]
    return c, uid


_n = [0]


def add_rec(uid: int, title: str = "Мало сна", status: str = "open", pending: bool = True) -> int:
    _n[0] += 1
    rid = sql(
        """INSERT INTO recommendations (user_id, rule_id, dedup_key, status, title, body, evidence)
           VALUES (:u, 'sleep_short_streak', :k, :s, :t, 'Пояснение: ' || :t, '{}') RETURNING id""",
        u=uid, k=f"k{_n[0]}", s=status, t=f"{title} {_n[0]}")[0][0]
    if pending:
        sql("INSERT INTO digest_pending (user_id, recommendation_id) VALUES (:u, :r)", u=uid, r=rid)
    return rid


def flush(uid: int) -> None:
    with new_session() as s:
        jobs.enqueue(s, "flush_digest", uid, {}, dedupe_key=f"digest:{uid}")
        s.commit()
    run_worker()


def cycle() -> None:
    """Один «тик» планировщика + выполнение созревших заданий."""
    with new_session() as s:
        mail.tick(s)
        s.commit()
    run_worker()


def outbox(uid: int):
    return sql("SELECT id, status, send_after, defer_reason, recommendation_ids, attempts, last_error, "
               "next_attempt_at, subject, body_text FROM outbox WHERE user_id=:u ORDER BY id", u=uid)


def inbox_count() -> int:
    return sql("SELECT count(*) FROM mail_inbox")[0][0]


# ---------------------------------------------------------------- дайджест

def test_multi_recs_one_mail_delivered_via_sink(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    ids = [add_rec(uid, f"Рекомендация {i}") for i in range(3)]
    flush(uid)
    ob = outbox(uid)
    assert len(ob) == 1 and ob[0][1] == "sent" and sorted(ob[0][4]) == sorted(ids)  # flush -> send_outbox
    assert ob[0][8] == "3 новые рекомендации"
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 0
    assert inbox_count() == 1
    box = c.get("/api/dev/mailbox").json()
    assert len(box) == 1 and box[0]["subject"] == "3 новые рекомендации" and box[0]["rcpt"] == "alice@example.com"
    for i in range(3):
        assert f"Рекомендация {i}" in box[0]["body_text"]
    items = c.get("/api/outbox").json()
    assert items[0]["status"] == "sent" and items[0]["sent_at"] and sorted(items[0]["recommendation_ids"]) == sorted(ids)
    assert items[0]["defer_reason"] is None


def test_same_rec_not_mailed_twice(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    rid = add_rec(uid)
    flush(uid)
    assert inbox_count() == 1
    sql("INSERT INTO digest_pending (user_id, recommendation_id) VALUES (:u, :r)", u=uid, r=rid)
    flush(uid)
    cycle()
    assert len(outbox(uid)) == 1 and inbox_count() == 1
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 0
    # новая рекомендация -> второе письмо, но только с ней
    r2 = add_rec(uid, "Другая")
    flush(uid)
    ob = outbox(uid)
    assert len(ob) == 2 and ob[1][4] == [r2] and inbox_count() == 2


def test_closed_before_flush_not_included(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    keep = add_rec(uid, "Остаётся")
    gone = add_rec(uid, "Отозвана")
    sql("UPDATE recommendations SET status='resolved', resolution_reason='rule_disabled' WHERE id=:i", i=gone)
    flush(uid)
    ob = outbox(uid)
    assert len(ob) == 1 and ob[0][4] == [keep] and "Отозвана" not in ob[0][9]
    # все отозваны -> письма нет вовсе
    g2 = add_rec(uid)
    sql("UPDATE recommendations SET status='resolved' WHERE id=:i", i=g2)
    flush(uid)
    assert len(outbox(uid)) == 1


def test_revoked_after_flush_dropped_at_send_time(sink):
    clock.set_fake_now(dt("2026-03-02T19:30:00Z"))  # 22:30 Москва, тихие часы
    c, uid = make_user(quiet=("22:00", "08:00"))
    a, b = add_rec(uid, "Первая"), add_rec(uid, "Вторая")
    flush(uid)
    assert outbox(uid)[0][1] == "pending" and inbox_count() == 0
    sql("UPDATE recommendations SET status='resolved', resolution_reason='auto_cleared' WHERE id=:i", i=b)
    clock.set_fake_now(dt("2026-03-03T05:00:00Z"))
    cycle()
    ob = outbox(uid)[0]
    assert ob[1] == "sent" and ob[4] == [a]
    body = c.get("/api/dev/mailbox").json()[0]
    assert "Первая" in body["body_text"] and "Вторая" not in body["body_text"]
    assert body["subject"] == "1 новая рекомендация"


def test_all_revoked_cancels_mail(sink):
    clock.set_fake_now(dt("2026-03-02T19:30:00Z"))
    c, uid = make_user(quiet=("22:00", "08:00"))
    a = add_rec(uid)
    flush(uid)
    sql("UPDATE recommendations SET status='resolved' WHERE id=:i", i=a)
    clock.set_fake_now(dt("2026-03-03T05:30:00Z"))
    cycle()
    ob = outbox(uid)[0]
    assert ob[1] == "cancelled" and ob[6] == "recommendations_closed"
    assert inbox_count() == 0


def test_no_email_no_mail_visible_in_status(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user(email=None)
    add_rec(uid)
    flush(uid)
    cycle()
    ob = outbox(uid)
    assert len(ob) == 1 and ob[0][1] == "cancelled" and ob[0][6] == "no_email"
    assert inbox_count() == 0
    assert c.get("/api/outbox").json()[0]["last_error"] == "no_email"
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 0


def test_appends_to_pending_letter(sink):
    clock.set_fake_now(dt("2026-03-02T19:30:00Z"))
    c, uid = make_user(quiet=("22:00", "08:00"))
    add_rec(uid, "Раз")
    flush(uid)
    add_rec(uid, "Два")
    add_rec(uid, "Три")
    flush(uid)
    ob = outbox(uid)
    assert len(ob) == 1 and len(ob[0][4]) == 3 and ob[0][8] == "3 новые рекомендации"
    assert ob[0][3] == "quiet_hours"
    clock.set_fake_now(dt("2026-03-03T05:01:00Z"))
    cycle()
    assert inbox_count() == 1 and outbox(uid)[0][1] == "sent"


def test_digest_tick_fallback_flushes_after_hold(sink):
    """Если движок правил не поставил flush_digest, tick делает это сам после окна склейки."""
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    add_rec(uid)
    sql("UPDATE digest_pending SET created_at = :t", t=dt("2026-03-02T09:58:00Z"))
    cycle()
    assert outbox(uid)[0][1] == "sent"


# ---------------------------------------------------------------- тихие часы

def test_quiet_hours_defer_then_send_after_end(sink):
    clock.set_fake_now(dt("2026-03-02T19:30:00Z"))  # 22:30 Москва
    c, uid = make_user(quiet=("22:00", "08:00"))
    add_rec(uid)
    flush(uid)
    cycle()
    ob = outbox(uid)[0]
    assert ob[1] == "pending" and ob[3] == "quiet_hours" and ob[2] == dt("2026-03-03T05:00:00Z")
    assert inbox_count() == 0
    api = c.get("/api/outbox").json()[0]
    assert api["status"] == "pending" and api["defer_reason"] == "quiet_hours"
    assert dt(api["send_after"]) == dt("2026-03-03T05:00:00Z")
    clock.set_fake_now(dt("2026-03-03T04:59:00Z"))
    cycle()
    assert inbox_count() == 0 and outbox(uid)[0][1] == "pending"
    clock.set_fake_now(dt("2026-03-03T05:00:00Z"))
    cycle()
    assert inbox_count() == 1 and outbox(uid)[0][1] == "sent"


def test_after_midnight_same_day_end(sink):
    clock.set_fake_now(dt("2026-03-02T21:00:00Z"))  # 00:00 Москва (3 марта)
    c, uid = make_user(quiet=("22:00", "08:00"))
    add_rec(uid)
    flush(uid)
    assert outbox(uid)[0][2] == dt("2026-03-03T05:00:00Z")  # 08:00 того же локального дня


def test_not_quiet_sends_immediately(sink):
    clock.set_fake_now(dt("2026-03-02T09:00:00Z"))  # 12:00 Москва
    c, uid = make_user(quiet=("22:00", "08:00"))
    add_rec(uid)
    flush(uid)
    assert outbox(uid)[0][1] == "sent" and outbox(uid)[0][3] is None


@pytest.mark.parametrize("hhmm, quiet", [
    ("21:59", False), ("22:00", True), ("23:59", True), ("00:00", True), ("07:59", True), ("08:00", False),
])
def test_quiet_hours_midnight_wrap_boundaries(hhmm, quiet):
    h, m = map(int, hhmm.split(":"))
    assert quiet_hours.is_quiet(time(h, m), time(22, 0), time(8, 0)) is quiet


def test_quiet_hours_plain_interval_and_degenerate():
    assert quiet_hours.is_quiet(time(13, 0), time(13, 0), time(15, 0))
    assert not quiet_hours.is_quiet(time(15, 0), time(13, 0), time(15, 0))
    assert not quiet_hours.is_quiet(time(23, 0), time(13, 0), time(15, 0))
    assert not quiet_hours.is_quiet(time(23, 0), time(8, 0), time(8, 0))
    tz = ZoneInfo("UTC")
    assert quiet_hours.release_time(dt("2026-03-02T14:00:00Z"), tz, True, time(13), time(15)) == dt("2026-03-02T15:00:00Z")
    assert quiet_hours.release_time(dt("2026-03-02T14:00:00Z"), tz, False, time(13), time(15)) is None
    assert quiet_hours.release_time(dt("2026-03-02T14:00:00Z"), tz, True, time(8), time(8)) is None


def test_release_dst_spring_forward_berlin():
    tz = ZoneInfo("Europe/Berlin")
    # 28 марта 22:30 CET; ночью (29-го в 02:00) переход на CEST -> 08:00 CEST = 06:00Z, а не 07:00Z
    r = quiet_hours.release_time(dt("2026-03-28T21:30:00Z"), tz, True, time(22), time(8))
    assert r == dt("2026-03-29T06:00:00Z")
    # конец тихих часов в несуществующее время 02:30 -> сдвиг вперёд, на 03:30 CEST = 01:30Z
    r = quiet_hours.release_time(dt("2026-03-28T23:30:00Z"), tz, True, time(0), time(2, 30))
    assert r == dt("2026-03-29T01:30:00Z")


def test_release_dst_fall_back_berlin():
    tz = ZoneInfo("Europe/Berlin")
    # 25 октября в 03:00 CEST часы идут назад; 02:30 CEST (00:30Z) -> 08:00 CET = 07:00Z
    r = quiet_hours.release_time(dt("2026-10-25T00:30:00Z"), tz, True, time(22), time(8))
    assert r == dt("2026-10-25T07:00:00Z")
    # во второй раз 02:15 (CET, 01:15Z), конец в 02:30: вторая встреча 02:30 = 01:30Z
    r = quiet_hours.release_time(dt("2026-10-25T01:15:00Z"), tz, True, time(0), time(2, 30))
    assert r == dt("2026-10-25T01:30:00Z")


def test_dst_day_in_pipeline(sink):
    clock.set_fake_now(dt("2026-03-28T21:30:00Z"))
    c, uid = make_user(tz="Europe/Berlin", quiet=("22:00", "08:00"))
    add_rec(uid)
    flush(uid)
    assert outbox(uid)[0][2] == dt("2026-03-29T06:00:00Z")
    clock.set_fake_now(dt("2026-03-29T05:59:00Z"))
    cycle()
    assert inbox_count() == 0
    clock.set_fake_now(dt("2026-03-29T06:00:00Z"))
    cycle()
    assert inbox_count() == 1


def test_tz_change_recalculates_send_after(sink):
    clock.set_fake_now(dt("2026-03-02T19:30:00Z"))  # 22:30 Москва
    c, uid = make_user(quiet=("22:00", "08:00"))
    add_rec(uid)
    flush(uid)
    assert outbox(uid)[0][2] == dt("2026-03-03T05:00:00Z")
    # Екатеринбург (+5): сейчас 00:30 локально, конец в 08:00 = 03:00Z
    r = c.put("/api/settings/timezone", json={"timezone": "Asia/Yekaterinburg"})
    assert r.status_code in (200, 202), r.text
    cycle()
    ob = outbox(uid)[0]
    assert ob[1] == "pending" and ob[2] == dt("2026-03-03T03:00:00Z") and ob[3] == "quiet_hours"
    # сдвиг тихих часов: конец 06:00 -> 01:00Z
    assert c.patch("/api/settings", json={"quiet_hours": {"end": "06:00"}}).status_code == 200
    cycle()
    assert outbox(uid)[0][2] == dt("2026-03-03T01:00:00Z")
    assert inbox_count() == 0
    # тихие часы выключены -> письмо уходит сразу
    assert c.patch("/api/settings", json={"quiet_hours": {"enabled": False}}).status_code == 200
    cycle()
    assert outbox(uid)[0][1] == "sent" and inbox_count() == 1


def test_send_time_recheck_defers_again(sink):
    """Настройки изменились между созданием письма и отправкой: перенос заново, без отправки."""
    clock.set_fake_now(dt("2026-03-02T09:00:00Z"))  # 12:00 Москва, не тихо
    c, uid = make_user(quiet=("22:00", "08:00"))
    rid = add_rec(uid, pending=False)
    sql("""INSERT INTO outbox (user_id, to_addr, subject, body_text, recommendation_ids, send_after)
           VALUES (:u, 'alice@example.com', 's', 'b', ARRAY[:r]::bigint[], :t)""", u=uid, r=rid, t=clock.now())
    assert c.patch("/api/settings", json={"quiet_hours": {"start": "11:00", "end": "13:00"}}).status_code == 200
    ob_id = outbox(uid)[0][0]
    with new_session() as s:
        assert sender.send_one(s, ob_id) == "deferred"
        s.commit()
    ob = outbox(uid)[0]
    assert ob[1] == "pending" and ob[2] == dt("2026-03-02T10:00:00Z") and inbox_count() == 0


# ---------------------------------------------------------------- SMTP, повторы

def test_retry_backoff_then_dead_then_manual_retry(dead_smtp, monkeypatch):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    add_rec(uid)
    flush(uid)  # попытка 1 -> failed
    ob = outbox(uid)[0]
    assert ob[1] == "failed" and ob[5] == 1 and ob[7] == dt("2026-03-02T10:01:00Z") and ob[6]
    cycle()  # рано: next_attempt_at ещё не наступил
    assert outbox(uid)[0][5] == 1
    expected = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)]
    for k, delta in enumerate(expected, start=2):
        clock.set_fake_now(outbox(uid)[0][7])
        cycle()
        ob = outbox(uid)[0]
        assert ob[1] == "failed" and ob[5] == k and ob[7] == clock.now() + delta
    clock.set_fake_now(outbox(uid)[0][7])
    cycle()
    ob = outbox(uid)[0]
    assert ob[1] == "dead" and ob[5] == 5 and ob[7] is None
    # ручной повтор после поднятия приёмника
    port = free_port()
    ctrl = start_sink(port)
    monkeypatch.setenv("HT_SMTP_PORT", str(port))
    try:
        r = c.post(f"/api/outbox/{ob[0]}/retry")
        assert r.status_code == 200 and r.json()["status"] == "pending"
        run_worker()
        assert outbox(uid)[0][1] == "sent" and inbox_count() == 1
    finally:
        ctrl.stop()
    assert c.post(f"/api/outbox/{ob[0]}/retry").status_code == 409


def test_failed_then_sent_when_sink_comes_up(dead_smtp, monkeypatch):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    add_rec(uid)
    flush(uid)
    assert outbox(uid)[0][1] == "failed"
    port = free_port()
    ctrl = start_sink(port)
    monkeypatch.setenv("HT_SMTP_PORT", str(port))
    try:
        clock.set_fake_now(dt("2026-03-02T10:01:00Z"))
        cycle()
        assert outbox(uid)[0][1] == "sent" and inbox_count() == 1
    finally:
        ctrl.stop()


def test_idempotent_message_id(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    add_rec(uid)
    flush(uid)
    row = {"id": outbox(uid)[0][0], "to_addr": "alice@example.com", "subject": "s", "body_text": "b"}
    sender.smtp_send(row)
    sender.smtp_send(row)
    assert inbox_count() == 1
    assert sql("SELECT message_id FROM mail_inbox")[0][0] == f"<{row['id']}@healthtracker.local>"


def test_stale_sending_returns_to_pending(sink):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    rid = add_rec(uid, pending=False)
    sql("""INSERT INTO outbox (user_id, to_addr, subject, body_text, recommendation_ids, send_after, status, locked_at)
           VALUES (:u, 'alice@example.com', 's', 'b', ARRAY[:r]::bigint[], :t, 'sending', :l)""",
        u=uid, r=rid, t=clock.now(), l=clock.now() - timedelta(seconds=200))
    cycle()
    assert outbox(uid)[0][1] == "sent"


# ---------------------------------------------------------------- REST

def test_mailbox_only_dev_and_scoped_to_user(sink, monkeypatch):
    clock.set_fake_now(dt("2026-03-02T10:00:00Z"))
    c, uid = make_user()
    add_rec(uid)
    flush(uid)
    sql("""INSERT INTO mail_inbox (message_id, rcpt, raw, subject)
           VALUES ('<x@y>', 'other@example.com', 'Subject: чужое' || chr(10) || chr(10) || 'тело', 'чужое')""")
    box = c.get("/api/dev/mailbox").json()
    assert len(box) == 1 and box[0]["rcpt"] == "alice@example.com"
    import app.mail.api as mail_api

    class Prod:
        ht_env = "prod"

    monkeypatch.setattr(mail_api, "get_settings", lambda: Prod())
    assert c.get("/api/dev/mailbox").status_code == 404


def test_outbox_requires_auth_and_isolated():
    assert new_client().get("/api/outbox").status_code == 401
    c, uid = make_user()
    assert c.get("/api/outbox").json() == []
    assert c.post("/api/outbox/999/retry").status_code == 404
