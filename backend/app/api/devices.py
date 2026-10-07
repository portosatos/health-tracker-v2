"""Устройства."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import jobs
from ..ingest import KINDS
from .deps import ApiError, CurrentUser, current_user, get_db

router = APIRouter(prefix="/devices", tags=["devices"])

_SELECT = """SELECT d.id, d.external_id, d.kind, d.model, d.name, d.priority,
   (SELECT greatest(
        (SELECT max(minute_ts) FROM steps_minute m WHERE m.device_id = d.id),
        (SELECT max(ts) FROM hr_points h WHERE h.device_id = d.id),
        (SELECT max(end_ts) FROM sleep_intervals s WHERE s.device_id = d.id))) AS last_data_at
   FROM devices d WHERE d.user_id = :u"""


def _view(r) -> dict:
    return {"id": r["id"], "external_id": r["external_id"], "kind": r["kind"], "model": r["model"],
            "name": r["name"], "priority": r["priority"],
            "last_data_at": r["last_data_at"].isoformat().replace("+00:00", "Z") if r["last_data_at"] else None}


@router.get("")
def list_devices(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(text(_SELECT + " ORDER BY d.priority DESC, d.id"), {"u": user.id}).mappings().all()
    return [_view(r) for r in rows]


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    kind: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


@router.patch("/{device_id}")
def patch_device(device_id: int, body: DevicePatch, user: CurrentUser = Depends(current_user),
                 db: Session = Depends(get_db)):
    row = db.execute(text("SELECT id, priority FROM devices WHERE id=:id AND user_id=:u"),
                     {"id": device_id, "u": user.id}).first()
    if row is None:
        raise ApiError(404, "not_found", "Устройство не найдено")
    if body.kind is not None and body.kind not in KINDS:
        raise ApiError(422, "validation_error", "kind: watch|phone|other")
    fields = body.model_fields_set
    if "name" in fields:
        db.execute(text("UPDATE devices SET name=:n WHERE id=:id"), {"n": body.name, "id": device_id})
    if body.kind is not None:
        db.execute(text("UPDATE devices SET kind=:k WHERE id=:id"), {"k": body.kind, "id": device_id})
    if body.priority is not None:
        db.execute(text("UPDATE devices SET priority=:p, priority_manual=true WHERE id=:id"),
                   {"p": body.priority, "id": device_id})
        if body.priority != row[1]:
            jobs.enqueue(db, "recompute_days", user.id, {"from": None}, dedupe_key=f"recompute:{user.id}:all")
    db.commit()
    r = db.execute(text(_SELECT + " AND d.id = :id"), {"u": user.id, "id": device_id}).mappings().one()
    return _view(r)
