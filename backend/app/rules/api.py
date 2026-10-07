"""REST правил и рекомендаций (DESIGN, раздел 12; префикс /api добавляет main.py)."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock
from ..api.deps import ApiError, CurrentUser, current_user, get_db
from ..api.summary import iso
from . import engine
from .definitions import BY_ID, RULES

router = APIRouter(tags=["rules"])


def _rule_dto(db: Session, user_id: int) -> list[dict]:
    en = engine.enabled_map(db, user_id)
    counts = dict(db.execute(
        text("SELECT rule_id, count(*) FROM recommendations WHERE user_id=:u AND status='open' GROUP BY rule_id"),
        {"u": user_id}).all())
    return [{**r.info(), "enabled": en[r.id], "open_count": int(counts.get(r.id, 0))} for r in RULES]


@router.get("/rules")
def list_rules(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    return _rule_dto(db, user.id)


class RulePatch(BaseModel):
    enabled: bool


@router.patch("/rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, user: CurrentUser = Depends(current_user),
               db: Session = Depends(get_db)):
    if rule_id not in BY_ID:
        raise ApiError(404, "not_found", "Правило не найдено")
    engine.set_rule_enabled(db, user.id, rule_id, body.enabled)
    db.commit()
    return next(r for r in _rule_dto(db, user.id) if r["id"] == rule_id)


def _rec_dto(r) -> dict:
    return {"id": r["id"], "rule_id": r["rule_id"], "title": r["title"], "body": r["body"],
            "evidence": r["evidence"], "status": r["status"], "resolution_reason": r["resolution_reason"],
            "opened_at": iso(r["opened_at"]), "resolved_at": iso(r["resolved_at"]),
            "feedback": r["feedback"]}


@router.get("/recommendations")
def list_recommendations(
    status: Literal["open", "resolved", "all"] = "open",
    rule_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    cursor: Optional[str] = None,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Новые сверху (по id); cursor — id последней отданной строки."""
    cur_id = None
    if cursor:
        try:
            cur_id = int(cursor)
        except ValueError:
            raise ApiError(422, "validation_error", "cursor: неверное значение") from None
    rows = db.execute(
        text("""SELECT * FROM recommendations
                WHERE user_id=:u AND (:s = 'all' OR status = :s) AND (CAST(:r AS text) IS NULL OR rule_id = :r)
                  AND (CAST(:c AS bigint) IS NULL OR id < :c)
                ORDER BY id DESC LIMIT :l"""),
        {"u": user.id, "s": status, "r": rule_id, "c": cur_id, "l": limit + 1}).mappings().all()
    items = rows[:limit]
    return {"items": [_rec_dto(r) for r in items], "next_cursor": str(items[-1]["id"]) if len(rows) > limit else None}


class FeedbackBody(BaseModel):
    value: Optional[Literal["helpful", "not_helpful", "useful", "not_useful"]] = None


@router.post("/recommendations/{rec_id}/feedback")
def feedback(rec_id: int, body: FeedbackBody, user: CurrentUser = Depends(current_user),
             db: Session = Depends(get_db)):
    value = {"useful": "helpful", "not_useful": "not_helpful"}.get(body.value or "", body.value)
    row = db.execute(
        text("""UPDATE recommendations SET feedback=:v, feedback_at = CASE WHEN CAST(:v AS text) IS NULL
                                                                          THEN NULL ELSE :n END
                WHERE id=:id AND user_id=:u RETURNING *"""),
        {"v": value, "n": clock.now(), "id": rec_id, "u": user.id}).mappings().first()
    if row is None:
        raise ApiError(404, "not_found", "Рекомендация не найдена")
    db.commit()
    return _rec_dto(row)
