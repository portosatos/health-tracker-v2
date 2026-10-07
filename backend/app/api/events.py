"""SSE /api/events и /api/health."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import new_session
from ..events import hub, sse_format
from .deps import CurrentUser, current_user, get_db

router = APIRouter(tags=["events"])


def _backlog(user_id: int, after: int) -> list[tuple]:
    with new_session() as s:
        return [tuple(r) for r in s.execute(
            text("SELECT id, type, payload FROM events WHERE user_id=:u AND id > :a ORDER BY id LIMIT 1000"),
            {"u": user_id, "a": after})]


def _head(user_id: int) -> int:
    with new_session() as s:
        return int(s.execute(text("SELECT coalesce(max(id), 0) FROM events")).scalar_one())


@router.get("/events")
async def events(request: Request, user: CurrentUser = Depends(current_user)):
    last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    q = hub.subscribe(user.id)
    try:
        after = int(last) if last and last.isdigit() else await run_in_threadpool(_head, user.id)
        backlog = await run_in_threadpool(_backlog, user.id, after) if last else []
    except BaseException:
        hub.unsubscribe(user.id, q)
        raise

    async def gen():
        sent = after
        try:
            yield "retry: 3000\n: connected\n\n"
            for eid, typ, payload in backlog:
                sent = max(sent, eid)
                yield sse_format(eid, typ, payload)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    eid, typ, payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if eid <= sent:
                    continue
                sent = eid
                yield sse_format(eid, typ, payload)
        finally:
            hub.unsubscribe(user.id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        age = db.execute(text("SELECT extract(epoch FROM now() - max(beat_at)) FROM worker_heartbeats")).scalar()
        return {"db": True, "worker_heartbeat_age_s": None if age is None else round(float(age), 1)}
    except Exception:  # noqa: BLE001
        return {"db": False, "worker_heartbeat_age_s": None}
