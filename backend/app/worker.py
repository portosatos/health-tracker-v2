"""Воркер: python -m app.worker (DESIGN, разделы 1 и 11)."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import traceback
from datetime import datetime

from sqlalchemy import text

from . import hooks, ingest, jobs
from .config import get_settings
from .db import NEG_INF, lock_user, new_session, raw_psycopg_connect
from .events import publish_event
from .goals import evaluate_goals
from .summary import as_of_date, load_timeline, recompute_from

log = logging.getLogger("ht.worker")
_wake = threading.Event()


def handle_recompute_days(job: dict) -> None:
    uid = job["user_id"]
    payload = job["payload"] or {}
    from_ts = payload.get("from")
    from_dt = datetime.fromisoformat(from_ts.replace("Z", "+00:00")) if from_ts else None
    with new_session() as s:
        lock_user(s, uid)
        tl = load_timeline(s, uid)
        dates = recompute_from(s, uid, from_dt, tl)
        if dates:
            evaluate_goals(s, uid, dates, as_of_date(s, uid, tl))
            publish_event(s, uid, "summary.updated", {"dates": [d.isoformat() for d in dates]})
        jobs.enqueue(s, "evaluate_rules", uid, {}, dedupe_key=f"eval:{uid}")
        s.commit()


def handle_evaluate_rules(job: dict) -> None:
    uid = job["user_id"]
    with new_session() as s:
        lock_user(s, uid)
        hooks.evaluate_rules(uid, s)
        s.commit()


BUILTIN = {
    "process_upload": ingest.process_upload,
    "recompute_days": handle_recompute_days,
    "evaluate_rules": handle_evaluate_rules,
}


def run_job(job: dict) -> None:
    fn = BUILTIN.get(job["kind"])
    if fn is not None:
        fn(job)
        return
    extra = hooks.extra_job_handlers().get(job["kind"])
    if extra is None:
        if job["kind"] == "tick":
            return
        raise jobs.JobFatal(f"нет обработчика для {job['kind']}")
    with new_session() as s:
        extra(s, job)
        s.commit()


def run_once(worker_id: str) -> bool:
    """Берёт и выполняет одно задание. True, если задание было."""
    with new_session() as s:
        job = jobs.pick_job(s, worker_id)
        s.commit()
    if job is None:
        return False
    try:
        run_job(job)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        log.error("job %s (%s) failed: %s\n%s", job["id"], job["kind"], err, traceback.format_exc())
        fatal = isinstance(e, jobs.JobFatal)
        if fatal:
            job["attempts"] = job["max_attempts"]
        with new_session() as s:
            jobs.fail_job(s, job, err)
            s.commit()
        if job["kind"] == "process_upload" and job["attempts"] >= job["max_attempts"]:
            ingest.mark_upload_failed(int(job["payload"]["upload_id"]), err)
        return True
    with new_session() as s:
        jobs.finish_job(s, job["id"])
        s.commit()
    return True


def run_until_idle(worker_id: str = "test", limit: int = 10000) -> int:
    n = 0
    while n < limit and run_once(worker_id):
        n += 1
    return n


def tick_once(worker_id: str) -> None:
    with new_session() as s:
        s.execute(
            text("""INSERT INTO worker_heartbeats (worker_id, beat_at) VALUES (:w, now())
                    ON CONFLICT (worker_id) DO UPDATE SET beat_at = now()"""), {"w": worker_id})
        jobs.requeue_stuck(s)
        s.execute(text("DELETE FROM events WHERE created_at < now() - interval '24 hours'"))
        s.commit()
    for hook in hooks.tick_hooks():
        with new_session() as s:
            hook(s)
            s.commit()


def _worker_loop(idx: int, stop: threading.Event) -> None:
    wid = f"{socket.gethostname()}:{os.getpid()}:{idx}"
    while not stop.is_set():
        try:
            if run_once(wid):
                continue
        except Exception:  # noqa: BLE001
            log.error("worker loop error\n%s", traceback.format_exc())
            stop.wait(2)
            continue
        _wake.wait(2.0)
        _wake.clear()


def _tick_loop(stop: threading.Event) -> None:
    wid = f"{socket.gethostname()}:{os.getpid()}:tick"
    while not stop.is_set():
        try:
            tick_once(wid)
        except Exception:  # noqa: BLE001
            log.error("tick error\n%s", traceback.format_exc())
        stop.wait(15)


def _listen_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            conn = raw_psycopg_connect()
            with conn:
                conn.execute("LISTEN ht_jobs")
                while not stop.is_set():
                    for _ in conn.notifies(timeout=2.0, stop_after=1):
                        _wake.set()
        except Exception:  # noqa: BLE001
            stop.wait(2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = get_settings()
    stop = threading.Event()
    threads = [threading.Thread(target=_tick_loop, args=(stop,), daemon=True),
               threading.Thread(target=_listen_loop, args=(stop,), daemon=True)]
    threads += [threading.Thread(target=_worker_loop, args=(i, stop), daemon=True)
                for i in range(cfg.worker_concurrency)]
    for t in threads:
        t.start()
    log.info("worker запущен, потоков обработки: %s", cfg.worker_concurrency)
    try:
        while True:
            stop.wait(1)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
