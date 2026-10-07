"""Движок рекомендаций и REST правил (DESIGN, разделы 6, 9, 10, 12)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app import jobs
from app.db import new_session
from app.rules import engine
from app.rules.definitions import RULES

from .conftest import dev, full_day, hr, new_client, register, run_worker, sleep, sql, steps, upload

BASE = date(2026, 3, 2)  # понедельник


def D(i: int) -> str:
    return (BASE + timedelta(days=i)).isoformat()


def night(i: int, minutes: int | None, ext="watch-01") -> list[dict]:
    """День i: шаги на 12 ч (complete) и, если minutes задан, сон, закончившийся в 07:00 этого дня."""
    out = full_day(ext, D(i), 8000)
    if minutes:
        end = datetime.combine(BASE + timedelta(days=i), datetime.min.time()) + timedelta(hours=7)
        start = end - timedelta(minutes=minutes)
        out.append(sleep(ext, start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")))
    return out


def sleeps(mins: dict[int, int | None], gaps: tuple[int, ...] = ()) -> list[dict]:
    lines = [dev("watch-01")]
    for i, m in mins.items():
        if i not in gaps:
            lines += night(i, m)
    return lines


def evaluate(uid: int = 1) -> dict:
    with new_session() as s:
        st = engine.evaluate(uid, s)
        s.commit()
    return st


def recs(uid: int = 1, **where):
    q = "SELECT id, rule_id, dedup_key, status, resolution_reason FROM recommendations WHERE user_id=:u ORDER BY id"
    return sql(q, u=uid)


def setup(client, mins, gaps=()):
    register(client)
    upload(client, sleeps(mins, gaps))


def test_catalog_has_six_rules_with_texts():
    ids = {r.id for r in RULES}
    assert ids >= {"sleep_short_streak", "steps_low_streak", "resting_hr_elevated", "steps_week_drop",
                   "sleep_debt_week", "peak_hr_high"}
    for r in RULES:
        assert r.title and r.description and r.params


def test_three_short_nights_open_recommendation(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    rows = recs()
    assert [(r[1], r[2], r[3]) for r in rows] == [("sleep_short_streak", "streak:" + D(1), "open")]
    # уведомление, пометка для письма и отложенный job
    assert sql("SELECT count(*) FROM notifications WHERE kind='recommendation'")[0][0] == 1
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 1
    j = sql("SELECT dedupe_key, status, run_at > now() FROM jobs WHERE kind='flush_digest'")
    assert j == [("digest:1", "queued", True)]
    assert sql("SELECT count(*) FROM events WHERE type='recommendation.changed'")[0][0] == 1
    ev = sql("SELECT evidence FROM recommendations")[0][0]
    assert ev["days"] == 3 and ev["values"] == [300, 320, 330]


def test_two_short_nights_do_nothing(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 480})
    assert recs() == []


def test_gap_mid_streak_does_not_fire(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330, 4: 310}, gaps=(2,))
    assert recs() == []  # ночь 2 без данных разорвала серию: 1 + (3,4) < 3


def test_gap_does_not_close_open(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    assert [r[3] for r in recs()] == ["open"]
    # следующая ночь без данных (день 4 пуст), день 5 — снова данные без сна: ничего не закрывает
    upload(client, [dev("watch-01")] + full_day("watch-01", D(5), 8000))
    assert [r[3] for r in recs()] == ["open"]
    # частичный день (2 часа шагов) — тоже UNKNOWN
    upload(client, [steps("watch-01", D(6) + " 08:00", D(6) + " 10:00", 300)])
    assert [r[3] for r in recs()] == ["open"]


def test_repeated_evaluation_is_idempotent(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    ev0 = sql("SELECT count(*) FROM events")[0][0]
    for _ in range(3):
        assert evaluate() == {"opened": 0, "updated": 0, "resolved": 0}
    assert len(recs()) == 1
    assert sql("SELECT count(*) FROM events")[0][0] == ev0
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 1
    assert sql("SELECT count(*) FROM notifications")[0][0] == 1


def test_streak_extends_updates_without_second_mail(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    upload(client, night(4, 310))
    rows = recs()
    assert len(rows) == 1 and rows[0][3] == "open"
    assert sql("SELECT evidence->>'days' FROM recommendations")[0][0] == "4"
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 1
    assert sql("SELECT count(*) FROM notifications")[0][0] == 1
    assert sql("SELECT count(*) FROM events WHERE type='recommendation.changed' "
               "AND payload->>'change'='updated'")[0][0] == 1


def test_fixing_data_closes_automatically_and_reopen_makes_new_row(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    # исправление: ночь 2 оказалась длиннее (добавили интервал раньше)
    upload(client, [sleep("watch-01", D(2) + " 00:00", D(2) + " 07:00")])
    rows = recs()
    assert [(r[3], r[4]) for r in rows] == [("resolved", "auto_cleared")]
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 0
    assert sql("SELECT count(*) FROM events WHERE payload->>'change'='resolved'")[0][0] == 1
    # ситуация возникает снова (данные вновь короткие): новая строка, история сохранена
    sql("DELETE FROM sleep_intervals WHERE start_ts = :s", s=datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc))
    from app.worker import handle_recompute_days
    handle_recompute_days({"user_id": 1, "payload": {}})
    evaluate()
    assert [r[3] for r in recs()] == ["resolved", "open"]


def test_good_night_after_streak_closes(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    upload(client, night(4, 480))
    assert [(r[3], r[4]) for r in recs()] == [("resolved", "auto_cleared")]


def test_disable_closes_with_reason_keeps_history_and_stops_firing(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    r = client.patch("/api/rules/sleep_short_streak", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["open_count"] == 0
    assert [(x[3], x[4]) for x in recs()] == [("resolved", "rule_disabled")]
    assert sql("SELECT count(*) FROM digest_pending")[0][0] == 0
    hist = client.get("/api/recommendations?status=resolved").json()["items"]
    assert len(hist) == 1 and hist[0]["resolution_reason"] == "rule_disabled"
    # отключённое правило не срабатывает, даже если ситуация новая и повторный прогон
    upload(client, night(4, 300) + night(5, 300))
    evaluate()
    assert len(recs()) == 1
    # включение: оценка заново, ситуация открывается новой строкой
    assert client.patch("/api/rules/sleep_short_streak", json={"enabled": True}).json()["enabled"] is True
    run_worker()
    assert [x[3] for x in recs()] == ["resolved", "open"]


def test_rules_listing_and_unknown_rule(client):
    register(client)
    r = client.get("/api/rules").json()
    assert len(r) >= 6
    assert set(r[0]) == {"id", "title", "description", "params", "enabled", "open_count"}
    assert all(x["enabled"] for x in r)
    assert client.patch("/api/rules/nope", json={"enabled": False}).status_code == 404


def test_feedback_set_and_clear(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    rid = client.get("/api/recommendations").json()["items"][0]["id"]
    r = client.post(f"/api/recommendations/{rid}/feedback", json={"value": "helpful"})
    assert r.status_code == 200 and r.json()["feedback"] == "helpful"
    assert sql("SELECT feedback_at IS NOT NULL FROM recommendations")[0][0] is True
    assert client.post(f"/api/recommendations/{rid}/feedback", json={"value": "not_helpful"}).json()["feedback"] \
        == "not_helpful"
    r = client.post(f"/api/recommendations/{rid}/feedback", json={"value": None})
    assert r.json()["feedback"] is None
    assert sql("SELECT feedback, feedback_at FROM recommendations")[0] == (None, None)
    assert client.post(f"/api/recommendations/{rid}/feedback", json={"value": "bad"}).status_code == 422
    assert client.post("/api/recommendations/999/feedback", json={"value": None}).status_code == 404


def test_recommendations_list_filters_and_paging(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    page = client.get("/api/recommendations?status=open").json()
    assert set(page["items"][0]) == {"id", "rule_id", "title", "body", "evidence", "status", "resolution_reason",
                                     "opened_at", "resolved_at", "feedback"}
    assert page["next_cursor"] is None
    assert client.get("/api/recommendations?status=resolved").json()["items"] == []
    assert client.get("/api/recommendations?status=all&rule_id=peak_hr_high").json()["items"] == []
    assert len(client.get("/api/recommendations?status=all&rule_id=sleep_short_streak").json()["items"]) == 1
    # страницы по 1
    client.patch("/api/rules/sleep_short_streak", json={"enabled": False})
    client.patch("/api/rules/sleep_short_streak", json={"enabled": True})
    run_worker()
    p1 = client.get("/api/recommendations?status=all&limit=1").json()
    assert len(p1["items"]) == 1 and p1["next_cursor"]
    p2 = client.get(f"/api/recommendations?status=all&limit=1&cursor={p1['next_cursor']}").json()
    assert len(p2["items"]) == 1 and p2["next_cursor"] is None
    assert p1["items"][0]["id"] != p2["items"][0]["id"]


def test_user_isolation(client):
    setup(client, {0: 480, 1: 300, 2: 320, 3: 330})
    rid = client.get("/api/recommendations").json()["items"][0]["id"]
    other = new_client()
    register(other, "bob")
    assert other.get("/api/recommendations?status=all").json()["items"] == []
    assert other.post(f"/api/recommendations/{rid}/feedback", json={"value": "helpful"}).status_code == 404
    # данные bob не влияют на alice; отключение правила у bob не трогает alice
    other.patch("/api/rules/sleep_short_streak", json={"enabled": False})
    assert [r["enabled"] for r in client.get("/api/rules").json() if r["id"] == "sleep_short_streak"] == [True]
    assert recs(1)[0][3] == "open"
    assert len(client.get("/api/recommendations").json()["items"]) == 1


def test_steps_low_streak_needs_goal_and_complete_days(client):
    register(client)
    lines = [dev("watch-01")]
    for i in range(5):
        lines += full_day("watch-01", D(i), 2000)
    upload(client, lines)
    assert recs() == []  # цели нет
    client.put("/api/goals/steps_per_day", json={"target": 8000})
    run_worker()
    rows = recs()
    assert [(r[1], r[2]) for r in rows] == [("steps_low_streak", "streak:" + D(0))]
    assert sql("SELECT evidence->>'days' FROM recommendations")[0][0] == "5"


def test_steps_low_streak_partial_day_breaks(client):
    register(client)
    lines = [dev("watch-01")] + full_day("watch-01", D(0), 2000) + full_day("watch-01", D(1), 2000)
    lines += [steps("watch-01", D(2) + " 08:00", D(2) + " 10:00", 200)]  # partial
    lines += full_day("watch-01", D(3), 2000)
    upload(client, lines)
    client.put("/api/goals/steps_per_day", json={"target": 8000})
    run_worker()
    assert recs() == []


def test_resting_hr_elevated(client):
    register(client)
    lines = [dev("watch-01")]
    for i in range(18):
        lines += full_day("watch-01", D(i), 8000)
    upload(client, lines)
    for i in range(18):
        sql("UPDATE daily_summary SET resting_hr = :v WHERE local_date = :d", v=62 if i < 15 else 71, d=BASE + timedelta(days=i))
    evaluate()
    rows = recs()
    assert [(r[1], r[2]) for r in rows] == [("resting_hr_elevated", "streak:" + D(15))]
    assert sql("SELECT evidence->>'baseline' FROM recommendations")[0][0] == "62"
    # день без пульса покоя не закрывает
    sql("UPDATE daily_summary SET resting_hr = NULL WHERE local_date = :d", d=BASE + timedelta(days=17))
    evaluate()
    assert [r[3] for r in recs()] == ["open"]
    # следующий день уже нормальный
    sql("UPDATE daily_summary SET resting_hr = 63 WHERE local_date = :d", d=BASE + timedelta(days=17))
    evaluate()
    assert [(r[3], r[4]) for r in recs()] == [("resolved", "auto_cleared")]


def test_peak_hr_high_open_clear_and_expire(client):
    register(client)
    lines = [dev("watch-01")] + full_day("watch-01", D(0), 8000)
    base = datetime.combine(BASE, datetime.min.time())
    for k in range(60):
        t = base + timedelta(hours=9, minutes=k)
        lines.append(hr("watch-01", t.strftime("%Y-%m-%d %H:%M"), 175 if k < 12 else 120))
    upload(client, lines)
    assert [(r[1], r[2], r[3]) for r in recs()] == [("peak_hr_high", "day:" + D(0), "open")]
    # данные продвинулись на 3 дня вперёд: дата вышла из окна -> expired
    upload(client, full_day("watch-01", D(3), 8000))
    assert [(r[3], r[4]) for r in recs()] == [("resolved", "expired")]


def test_peak_hr_high_correction_clears(client):
    register(client)
    lines = [dev("watch-01")] + full_day("watch-01", D(0), 8000)
    base = datetime.combine(BASE, datetime.min.time())
    for k in range(60):
        t = base + timedelta(hours=9, minutes=k)
        lines.append(hr("watch-01", t.strftime("%Y-%m-%d %H:%M"), 175 if k < 12 else 120))
    upload(client, lines)
    assert recs()[0][3] == "open"
    sql("DELETE FROM hr_points WHERE bpm = 175")
    sql("UPDATE daily_summary SET peak_hr = 120")
    evaluate()
    assert [(r[3], r[4]) for r in recs()] == [("resolved", "auto_cleared")]


def test_steps_week_drop(client):
    register(client)
    lines = [dev("watch-01")]
    for i in range(14):                      # недели Пн-Вс: 8000, потом 4000
        lines += full_day("watch-01", D(i), 8000 if i < 7 else 4000)
    lines += full_day("watch-01", D(14), 4000)   # понедельник следующей недели закрывает вторую
    upload(client, lines)
    rows = recs()
    assert [(r[1], r[2], r[3]) for r in rows] == [("steps_week_drop", "week:2026-W11", "open")]


def test_steps_week_drop_unknown_with_few_days(client):
    register(client)
    lines = [dev("watch-01")]
    for i in range(7):
        lines += full_day("watch-01", D(i), 8000)
    for i in (7, 8, 9):                       # только 3 валидных дня
        lines += full_day("watch-01", D(i), 1000)
    lines += full_day("watch-01", D(14), 1000)
    upload(client, lines)
    assert recs() == []


def test_sleep_debt_week(client):
    register(client)
    client.put("/api/goals/sleep_minutes_per_day", json={"target": 480})
    lines = [dev("watch-01")]
    for i in range(7):
        lines += night(i, 400)                # 400 <= 480-60 ... 7 ч > 6.67 ч?  400 мин = 6 ч 40
    lines += night(7, 450)
    upload(client, lines)
    rows = recs()
    assert [(r[1], r[2], r[3]) for r in rows if r[1] == "sleep_debt_week"] == [("sleep_debt_week", "week:2026-W10", "open")]


def test_sleep_debt_week_needs_five_nights(client):
    register(client)
    client.put("/api/goals/sleep_minutes_per_day", json={"target": 480})
    lines = [dev("watch-01")]
    for i in range(4):
        lines += night(i, 380)
    lines += night(7, 450)
    upload(client, lines)
    assert [r for r in recs() if r[1] == "sleep_debt_week"] == []


def test_evaluate_without_data_is_noop(client):
    register(client)
    assert evaluate() == {"opened": 0, "updated": 0, "resolved": 0}


def test_evaluate_via_worker_hook_after_upload(client):
    register(client)
    upload(client, sleeps({0: 480, 1: 300, 2: 320, 3: 330}), run_worker=False)
    run_worker()
    assert len(recs()) == 1
