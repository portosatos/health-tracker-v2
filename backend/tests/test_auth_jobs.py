"""Аутентификация, изоляция пользователей, очередь заданий."""
from __future__ import annotations

import threading

from app import jobs, worker
from app.db import new_session

from .conftest import (
    daily, dev, full_day, new_client, register, run_worker, sql, steps, upload,
)


def test_register_login_logout_me(client):
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/register", json={"login": "Bob", "password": "password123"})
    assert r.status_code == 201 and "ht_session" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    me = client.get("/api/auth/me").json()
    assert me["login"] == "Bob" and me["timezone"] == "Europe/Moscow" and me["as_of"] is None
    assert sql("SELECT password_hash FROM users")[0][0].startswith("$argon2id$")
    assert client.post("/api/auth/register", json={"login": "bob", "password": "password123"}).status_code == 409
    assert client.post("/api/auth/register", json={"login": "x", "password": "password123"}).status_code == 422
    assert client.post("/api/auth/register", json={"login": "carol", "password": "short"}).status_code == 422
    other = new_client()
    assert other.post("/api/auth/login", json={"login": "bob", "password": "wrongpass1"}).status_code == 401
    assert other.post("/api/auth/login", json={"login": "bob", "password": "password123"}).status_code == 200
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert other.get("/api/auth/me").status_code == 200  # другая сессия жива


def test_csrf_header_required(client):
    register(client)
    bare = new_client()
    bare.headers.pop("X-Requested-With")
    bare.cookies.set("ht_session", client.cookies.get("ht_session"))
    r = bare.post("/api/notifications/read-all")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    assert bare.get("/api/goals").status_code == 200


def test_two_tabs_same_cookie_independent_requests():
    a = new_client()
    register(a)
    b = new_client()
    b.cookies.set("ht_session", a.cookies.get("ht_session"))
    results: dict = {}

    def go(name, c):
        results[name] = c.get("/api/summary/daily?date=2026-03-02").status_code

    ts = [threading.Thread(target=go, args=(n, c)) for n, c in (("a", a), ("b", b))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results == {"a": 200, "b": 200}


def test_user_isolation():
    a, b = new_client(), new_client()
    register(a, "alice")
    register(b, "bob")
    a.put("/api/goals/steps_per_day", json={"target": 1000})
    ua = upload(a, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 3000)])
    # у Боба тот же external_id устройства — это другое устройство
    ub = upload(b, [dev("watch-01"), *full_day("watch-01", "2026-03-02", 500)])
    assert daily(a, "2026-03-02")["steps"]["value"] == 3000
    assert daily(b, "2026-03-02")["steps"]["value"] == 500
    assert b.get(f"/api/uploads/{ua}").status_code == 404
    assert b.get(f"/api/uploads/{ua}/errors").status_code == 404
    assert b.post(f"/api/uploads/{ua}/cancel").status_code == 404
    assert [u["id"] for u in b.get("/api/uploads").json()] == [ub]
    da = a.get("/api/devices").json()
    assert b.patch(f"/api/devices/{da[0]['id']}", json={"priority": 1}).status_code == 404
    assert len(a.get("/api/notifications").json()) == 1 and b.get("/api/notifications").json() == []
    assert b.get("/api/goals").json() == []
    assert b.get("/api/summary/weekly?week=2026-W10").json()["steps_total"] is not None
    assert len(a.get("/api/devices").json()) == 1 and len(b.get("/api/devices").json()) == 1


def test_device_priority_patch_recomputes(user):
    upload(user, [dev("watch-01", "watch"), dev("phone-01", "phone"),
                  *full_day("watch-01", "2026-03-02", 0),
                  steps("watch-01", "2026-03-02 06:00", "2026-03-02 06:30", 2100),
                  steps("phone-01", "2026-03-02 06:00", "2026-03-02 06:30", 900)])
    assert daily(user, "2026-03-02")["steps"]["value"] == 2100
    phone = [d for d in user.get("/api/devices").json() if d["kind"] == "phone"][0]
    assert phone["priority"] == 10 and phone["last_data_at"] is not None
    r = user.patch(f"/api/devices/{phone['id']}", json={"priority": 90})
    assert r.status_code == 200 and r.json()["priority"] == 90
    run_worker()
    assert daily(user, "2026-03-02")["steps"]["value"] == 900
    # приоритет, выставленный вручную, файлом не перезаписывается
    upload(user, [dev("phone-01", "phone", priority=1)])
    assert [d for d in user.get("/api/devices").json() if d["kind"] == "phone"][0]["priority"] == 90


def test_jobs_skip_locked_two_workers_no_double_run():
    with new_session() as s:
        for i in range(30):
            jobs.enqueue(s, "tick", None, {"i": i})
        s.commit()

    def loop(name):
        while worker.run_once(name):
            pass

    ts = [threading.Thread(target=loop, args=(f"w{i}",)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rows = sql("SELECT status, attempts FROM jobs")
    assert len(rows) == 30 and all(r == ("done", 1) for r in rows)


def test_job_dedupe_collapses():
    with new_session() as s:
        a = jobs.enqueue(s, "evaluate_rules", 1, {}, dedupe_key="eval:1")
        b = jobs.enqueue(s, "evaluate_rules", 1, {}, dedupe_key="eval:1")
        s.commit()
    assert a is not None and b is None


def test_job_resume_from_byte_offset(user, monkeypatch):
    from app import ingest
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "chunk_lines", 3)
    lines = [dev("watch-01")] + [steps("watch-01", f"2026-03-02 {h:02d}:00", f"2026-03-02 {h:02d}:30", 30)
                                  for h in range(8, 16)]
    uid = upload(user, lines, run_worker=False)
    real = ingest.read_chunk
    calls = {"n": 0}

    def flaky(path, off, n):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("воркер упал")
        return real(path, off, n)

    monkeypatch.setattr(ingest, "read_chunk", flaky)
    assert worker.run_once("w")
    up = sql("SELECT status, byte_offset, lines_done FROM uploads WHERE id=:i", i=uid)[0]
    assert up[0] == "processing" and up[1] > 0 and up[2] == 6  # два чанка закоммичены
    assert sql("SELECT status, attempts FROM jobs WHERE kind='process_upload'")[0] == ("queued", 1)
    monkeypatch.setattr(ingest, "read_chunk", real)
    sql("UPDATE jobs SET run_at = now()")
    run_worker()
    st = user.get(f"/api/uploads/{uid}").json()
    assert st["status"] == "done" and st["lines_done"] == 9 and st["counters"]["inserted"] == 9
    assert sql("SELECT count(*) FROM steps_minute")[0][0] == 8 * 30


def test_upload_cancel_and_progress(user):
    uid = upload(user, [dev("watch-01")], run_worker=False)
    st = user.get(f"/api/uploads/{uid}").json()
    assert st["status"] == "queued" and st["percent"] == 0
    assert user.post(f"/api/uploads/{uid}/cancel").status_code == 202
    run_worker()
    assert user.get(f"/api/uploads/{uid}").json()["status"] == "cancelled"
    assert user.post(f"/api/uploads/{uid}/cancel").status_code == 409


def test_progress_events_monotonic(user, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "chunk_lines", 2)
    monkeypatch.setattr("app.ingest._last_progress", {})
    import app.ingest as ing
    monkeypatch.setattr(ing.time, "monotonic", lambda: __import__("itertools").count(0, 10).__next__())
    uid = upload(user, [dev("watch-01")] + [steps("watch-01", f"2026-03-02 {h:02d}:00", f"2026-03-02 {h:02d}:10", 5)
                                              for h in range(6, 12)])
    ev = sql("SELECT payload FROM events WHERE type='upload.progress' ORDER BY id")
    done = [e[0]["lines_done"] for e in ev]
    assert done == sorted(done) and ev[-1][0]["percent"] == 100 and ev[-1][0]["upload_id"] == uid
