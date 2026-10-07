"""Движок рекомендаций (DESIGN, разделы 9 и 10).

evaluate(user_id, session) вызывается job-ом evaluate_rules под pg_advisory_xact_lock(user_id); идемпотентен.
Жизненный цикл ситуации (ключ дедупликации = rule_id + dedup_key среди open):
  ACTIVE и нет открытой  -> INSERT open + notifications + digest_pending + job flush_digest + SSE;
  ACTIVE и есть открытая -> обновить evidence/body (только если изменились), письмо не повторяется;
  CLEARED                -> resolved (auto_cleared | expired), убрать из digest_pending, SSE;
  UNKNOWN                -> без изменений.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock, jobs
from ..config import get_settings
from ..db import lock_user
from ..events import publish_event
from ..summary import as_of_date, load_timeline, merge_hr, _load_span
from .definitions import BY_ID, RULES
from .definitions.base import ROWS_BACK_DAYS, Ctx, Finding, Result, Rule

log = logging.getLogger("ht.rules")
HR_HIGH_BPM = 170


# ---------------------------------------------------------------- состояние правил

def enabled_map(session: Session, user_id: int) -> dict[str, bool]:
    """rule_id -> включено (по умолчанию true, если строки в rule_settings нет)."""
    rows = session.execute(text("SELECT rule_id, enabled FROM rule_settings WHERE user_id=:u"), {"u": user_id}).all()
    m = {r.id: True for r in RULES}
    m.update({r[0]: r[1] for r in rows if r[0] in m})
    return m


# ---------------------------------------------------------------- контекст

def _make_hr_high(session: Session, user_id: int, tl):
    cfg = get_settings()
    cache: dict[date, int | None] = {}

    def hr_high(d: date) -> int | None:
        if d in cache:
            return cache[d]
        ranges = tl.day_ranges(d)
        if not ranges:
            cache[d] = None
            return None
        _, hr, _ = _load_span(session, user_id, ranges[0][0], ranges[-1][1])
        hr_m = merge_hr((r[0], int(r[1]), r[2], r[3]) for r in hr)
        total = high = 0
        for a, b in ranges:
            for t in range(a, b):
                v = hr_m.get(t)
                if v is not None:
                    total += 1
                    high += v >= HR_HIGH_BPM
        cache[d] = high if total >= cfg.hr_min_minutes else None
        return cache[d]

    return hr_high


def build_ctx(session: Session, user_id: int) -> Ctx | None:
    tl = load_timeline(session, user_id)
    as_of = as_of_date(session, user_id, tl)
    if as_of is None:
        return None
    rows = session.execute(
        text("SELECT * FROM daily_summary WHERE user_id=:u AND local_date BETWEEN :a AND :b"),
        {"u": user_id, "a": as_of - timedelta(days=ROWS_BACK_DAYS), "b": as_of},
    ).mappings().all()
    goals = {k: t for k, t in session.execute(
        text("SELECT kind, target FROM goals WHERE user_id=:u AND active"), {"u": user_id}).all()}
    return Ctx(user_id=user_id, as_of=as_of, rows={r["local_date"]: dict(r) for r in rows}, goals=goals,
               hr_high=_make_hr_high(session, user_id, tl))


# ---------------------------------------------------------------- операции над рекомендациями

def _open_recs(session: Session, user_id: int, rule_id: str) -> dict[str, dict]:
    rows = session.execute(
        text("SELECT id, dedup_key, title, body, evidence FROM recommendations "
             "WHERE user_id=:u AND rule_id=:r AND status='open'"),
        {"u": user_id, "r": rule_id}).mappings().all()
    return {r["dedup_key"]: dict(r) for r in rows}


def _open_new(session: Session, user_id: int, rule: Rule, f: Finding) -> int | None:
    now = clock.now()
    rid = session.execute(
        text("""INSERT INTO recommendations (user_id, rule_id, dedup_key, status, title, body, evidence,
                                             opened_at, updated_at)
                VALUES (:u, :r, :k, 'open', :t, :b, CAST(:e AS jsonb), :n, :n)
                ON CONFLICT (user_id, rule_id, dedup_key) WHERE status = 'open' DO NOTHING
                RETURNING id"""),
        {"u": user_id, "r": rule.id, "k": f.key, "t": f.title, "b": f.body, "e": json.dumps(f.evidence), "n": now},
    ).scalar_one_or_none()
    if rid is None:
        return None
    note = session.execute(
        text("""INSERT INTO notifications (user_id, kind, payload, dedup_key)
                VALUES (:u, 'recommendation', CAST(:p AS jsonb), :k)
                ON CONFLICT (user_id, dedup_key) DO NOTHING RETURNING id"""),
        {"u": user_id, "k": f"reco:{rid}",
         "p": json.dumps({"recommendation_id": rid, "rule_id": rule.id, "title": f.title})},
    ).scalar_one_or_none()
    # письмо: пометка для дайджеста + отложенный job (несколько вставок схлопываются по dedupe_key)
    session.execute(
        text("INSERT INTO digest_pending (user_id, recommendation_id) VALUES (:u, :r) ON CONFLICT DO NOTHING"),
        {"u": user_id, "r": rid})
    from ..mail.digest import schedule_flush
    schedule_flush(session, user_id, now)
    publish_event(session, user_id, "recommendation.changed",
                  {"id": rid, "rule_id": rule.id, "status": "open", "change": "opened"})
    if note is not None:
        publish_event(session, user_id, "notification.created", {"id": note, "kind": "recommendation"})
    return rid


def _update_open(session: Session, user_id: int, rule: Rule, cur: dict, f: Finding) -> bool:
    """Обновляет открытую рекомендацию, только если что-то изменилось (идемпотентность)."""
    if cur["title"] == f.title and cur["body"] == f.body and cur["evidence"] == json.loads(json.dumps(f.evidence)):
        return False
    session.execute(
        text("UPDATE recommendations SET title=:t, body=:b, evidence=CAST(:e AS jsonb), updated_at=:n WHERE id=:id"),
        {"t": f.title, "b": f.body, "e": json.dumps(f.evidence), "n": clock.now(), "id": cur["id"]})
    publish_event(session, user_id, "recommendation.changed",
                  {"id": cur["id"], "rule_id": rule.id, "status": "open", "change": "updated"})
    return True


def resolve(session: Session, user_id: int, rec_id: int, rule_id: str, reason: str) -> None:
    session.execute(
        text("""UPDATE recommendations SET status='resolved', resolved_at=:n, updated_at=:n, resolution_reason=:r
                WHERE id=:id AND status='open'"""),
        {"n": clock.now(), "r": reason, "id": rec_id})
    # письмо ещё не ушло: рекомендация больше не ждёт дайджеста (неотправленное письмо перегенерируется почтой)
    session.execute(text("DELETE FROM digest_pending WHERE recommendation_id=:id"), {"id": rec_id})
    publish_event(session, user_id, "recommendation.changed",
                  {"id": rec_id, "rule_id": rule_id, "status": "resolved", "change": "resolved",
                   "reason": reason})


def close_open(session: Session, user_id: int, rule_id: str, reason: str = "rule_disabled") -> int:
    ids = [r[0] for r in session.execute(
        text("SELECT id FROM recommendations WHERE user_id=:u AND rule_id=:r AND status='open' ORDER BY id"),
        {"u": user_id, "r": rule_id}).all()]
    for i in ids:
        resolve(session, user_id, i, rule_id, reason)
    return len(ids)


# ---------------------------------------------------------------- оценка

def evaluate(user_id: int, session: Session) -> dict[str, int]:
    """Оценивает все включённые правила пользователя. Возвращает счётчики opened/updated/resolved."""
    lock_user(session, user_id)
    stats = {"opened": 0, "updated": 0, "resolved": 0}
    enabled = enabled_map(session, user_id)
    for rule in RULES:  # отключённое правило не оценивается; его открытые закрываются (страховка к PATCH)
        if not enabled[rule.id]:
            stats["resolved"] += close_open(session, user_id, rule.id, "rule_disabled")
    ctx = build_ctx(session, user_id)
    if ctx is None:
        return stats
    for rule in RULES:
        if not enabled[rule.id]:
            continue
        open_by_key = _open_recs(session, user_id, rule.id)
        active_keys: set[str] = set()
        for f in rule.find(ctx):
            active_keys.add(f.key)
            cur = open_by_key.get(f.key)
            if cur is None:
                stats["opened"] += _open_new(session, user_id, rule, f) is not None
            else:
                stats["updated"] += _update_open(session, user_id, rule, cur, f)
        for key, cur in open_by_key.items():
            if key in active_keys:
                continue
            v = rule.judge(ctx, key, cur["evidence"] or {})
            if v.result == Result.CLEARED:
                resolve(session, user_id, cur["id"], rule.id, v.reason)
                stats["resolved"] += 1
    return stats


def set_rule_enabled(session: Session, user_id: int, rule_id: str, enabled: bool) -> None:
    """Включение/отключение правила (PATCH /rules/{id}). Под lock_user вызывающего или здесь."""
    if rule_id not in BY_ID:
        raise KeyError(rule_id)
    lock_user(session, user_id)
    session.execute(
        text("""INSERT INTO rule_settings (user_id, rule_id, enabled, changed_at) VALUES (:u, :r, :e, :n)
                ON CONFLICT (user_id, rule_id) DO UPDATE SET enabled = EXCLUDED.enabled, changed_at = EXCLUDED.changed_at"""),
        {"u": user_id, "r": rule_id, "e": enabled, "n": clock.now()})
    if not enabled:
        close_open(session, user_id, rule_id, "rule_disabled")
    else:  # включение: оценка заново
        jobs.enqueue(session, "evaluate_rules", user_id, {}, dedupe_key=f"eval:{user_id}")


# ---------------------------------------------------------------- тик (раз в 15 с в воркере)

_seen: dict[int, tuple] = {}


def tick(session: Session) -> None:
    """Ставит evaluate_rules тем, у кого изменился as_of или наступили новые сутки (по локальному времени)."""
    from ..timeline import to_min
    now_min = to_min(clock.now())
    for (uid,) in session.execute(text("SELECT id FROM users")).all():
        tl = load_timeline(session, uid)
        sig = (as_of_date(session, uid, tl), tl.local_date(now_min))
        if sig[0] is None or _seen.get(uid) == sig:
            continue
        _seen[uid] = sig
        jobs.enqueue(session, "evaluate_rules", uid, {}, dedupe_key=f"eval:{uid}")
