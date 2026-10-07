"""События (таблица events + NOTIFY ht_events) и fan-out для SSE (DESIGN, разделы 1 и 11)."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import raw_psycopg_connect


def publish_event(session: Session, user_id: int, type_: str, payload: dict[str, Any]) -> int:
    """Пишет событие в рамках транзакции вызывающего; NOTIFY уходит при коммите."""
    eid = session.execute(
        text("INSERT INTO events (user_id, type, payload) VALUES (:u, :t, CAST(:p AS jsonb)) RETURNING id"),
        {"u": user_id, "t": type_, "p": json.dumps(payload, default=str)},
    ).scalar_one()
    session.execute(text("SELECT pg_notify('ht_events', :id)"), {"id": str(eid)})
    return int(eid)


class EventHub:
    """Одно LISTEN-соединение на процесс api; раздаёт события подписчикам по user_id."""

    def __init__(self) -> None:
        self._subs: dict[int, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_id = 0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="event-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10000)
        with self._lock:
            self._subs.setdefault(user_id, set()).add(q)
        return q

    def unsubscribe(self, user_id: int, q: asyncio.Queue) -> None:
        with self._lock:
            s = self._subs.get(user_id)
            if s:
                s.discard(q)
                if not s:
                    self._subs.pop(user_id, None)

    def _dispatch(self, row: tuple) -> None:
        eid, uid, typ, payload = row
        with self._lock:
            queues = list(self._subs.get(uid, ()))
        for q in queues:
            try:
                q.put_nowait((eid, typ, payload))
            except asyncio.QueueFull:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = raw_psycopg_connect(autocommit=True)
                with conn:
                    conn.execute("LISTEN ht_events")
                    self.last_id = conn.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
                    while not self._stop.is_set():
                        # ждём NOTIFY до 1 с; затем в любом случае подбираем новые строки (страховка)
                        for _ in conn.notifies(timeout=1.0, stop_after=1):
                            pass
                        rows = conn.execute(
                            "SELECT id, user_id, type, payload FROM events WHERE id > %s ORDER BY id",
                            (self.last_id,),
                        ).fetchall()
                        for r in rows:
                            self.last_id = r[0]
                            if self._loop is not None:
                                self._loop.call_soon_threadsafe(self._dispatch, r)
            except Exception:  # noqa: BLE001 - переподключение
                self._stop.wait(1.0)


hub = EventHub()


def sse_format(eid: int, typ: str, payload: Any) -> str:
    return f"id: {eid}\nevent: {typ}\ndata: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"
