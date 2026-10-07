"""Цели, прогресс, достижения; уведомления UI."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import jobs
from ..db import lock_user
from ..goals import GOAL_KINDS, evaluate_goals
from ..summary import as_of_date, load_timeline
from .deps import ApiError, CurrentUser, current_user, get_db
from .summary import goals_progress, iso, load_rows, parse_date

router = APIRouter(tags=["goals"])


@router.get("/goals")
def list_goals(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT kind, target, active FROM goals WHERE user_id=:u ORDER BY id"),
                      {"u": user.id}).all()
    return [{"kind": r[0], "target": r[1], "active": r[2]} for r in rows]


class GoalBody(BaseModel):
    target: int = Field(gt=0, le=1_000_000)


@router.put("/goals/{kind}")
def put_goal(kind: str, body: GoalBody, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    if kind not in GOAL_KINDS:
        raise ApiError(404, "not_found", "Неизвестный вид цели")
    if kind == "sleep_minutes_per_day" and body.target > 1440:
        raise ApiError(422, "validation_error", "Цель сна: не более 1440 минут")
    lock_user(db, user.id)
    db.execute(
        text("""INSERT INTO goals (user_id, kind, target, active) VALUES (:u, :k, :t, true)
                ON CONFLICT (user_id, kind) DO UPDATE SET target = EXCLUDED.target, active = true"""),
        {"u": user.id, "k": kind, "t": body.target},
    )
    tl = load_timeline(db, user.id)
    dates = [r[0] for r in db.execute(text("SELECT local_date FROM daily_summary WHERE user_id=:u"),
                                      {"u": user.id}).all()]
    evaluate_goals(db, user.id, dates, as_of_date(db, user.id, tl))
    jobs.enqueue(db, "evaluate_rules", user.id, {}, dedupe_key=f"eval:{user.id}")
    db.commit()
    return {"kind": kind, "target": body.target, "active": True}


@router.get("/goals/progress")
def progress(date: str = Query(...), user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    d = parse_date(date)
    row = load_rows(db, user.id, d, d).get(d)
    return goals_progress(db, user.id, d, row)


@router.get("/goals/achievements")
def achievements(limit: int = 50, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        text("""SELECT g.kind, a.local_date, a.value, a.target_at_time, a.achieved_at, a.revoked_at
                FROM goal_achievements a JOIN goals g ON g.id = a.goal_id
                WHERE a.user_id=:u ORDER BY a.local_date DESC, a.id DESC LIMIT :l"""),
        {"u": user.id, "l": max(1, min(limit, 500))},
    ).all()
    return [{"goal_kind": r[0], "local_date": r[1].isoformat(), "value": r[2], "target_at_time": r[3],
             "achieved_at": iso(r[4]), "revoked": r[5] is not None} for r in rows]


# --------------------------------------------------------------- уведомления

def _note(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "payload": r["payload"], "created_at": iso(r["created_at"]),
            "read_at": iso(r["read_at"])}


@router.get("/notifications")
def list_notifications(unread: int = 0, limit: int = 50, user: CurrentUser = Depends(current_user),
                       db: Session = Depends(get_db)):
    q = "SELECT * FROM notifications WHERE user_id=:u" + (" AND read_at IS NULL" if unread else "")
    rows = db.execute(text(q + " ORDER BY id DESC LIMIT :l"), {"u": user.id, "l": max(1, min(limit, 200))}) \
        .mappings().all()
    return [_note(r) for r in rows]


@router.post("/notifications/read-all", status_code=204)
def read_all(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(text("UPDATE notifications SET read_at = now() WHERE user_id=:u AND read_at IS NULL"), {"u": user.id})
    db.commit()
    return Response(status_code=204)


@router.post("/notifications/{note_id}/read", status_code=204)
def read_one(note_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    r = db.execute(
        text("UPDATE notifications SET read_at = coalesce(read_at, now()) WHERE id=:id AND user_id=:u RETURNING id"),
        {"id": note_id, "u": user.id}).first()
    if r is None:
        raise ApiError(404, "not_found", "Уведомление не найдено")
    db.commit()
    return Response(status_code=204)
