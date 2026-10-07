"""Загрузки: приём файла (потоково), статус, ошибки, отмена; /dev/generate."""
from __future__ import annotations

import importlib
import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import jobs
from ..config import get_settings
from ..db import new_session
from ..events import publish_event
from .deps import ApiError, CurrentUser, current_user, get_db

router = APIRouter(tags=["uploads"])


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def upload_view(r: dict, detail: bool = False) -> dict:
    total = r["lines_total"] or 0
    pct = 100 if r["status"] in ("done", "done_with_errors") else (
        min(100, int(r["lines_done"] * 100 / total)) if total else 0)
    out = {
        "id": r["id"], "filename": r["filename"], "size_bytes": r["size_bytes"], "status": r["status"],
        "lines_done": r["lines_done"], "lines_total": r["lines_total"], "percent": pct,
        "counters": {"inserted": r["rows_inserted"], "updated": r["rows_updated"],
                     "duplicate": r["rows_duplicate"], "rejected": r["rows_rejected"]},
        "created_at": _iso(r["created_at"]), "finished_at": _iso(r["finished_at"]),
    }
    if detail:
        out.update({"min_ts": _iso(r["min_ts"]), "max_ts": _iso(r["max_ts"]), "error": r["error"]})
    return out


def _register_upload(user_id: int, filename: str, size: int, tmp_path: Path) -> int:
    """Создаёт запись uploads + job в одной транзакции и переименовывает файл в <id>.ndjson."""
    with new_session() as s:
        uid = s.execute(
            text("""INSERT INTO uploads (user_id, filename, size_bytes, path) VALUES (:u, :f, :s, :p) RETURNING id"""),
            {"u": user_id, "f": filename[:255], "s": size, "p": str(tmp_path)},
        ).scalar_one()
        final = tmp_path.with_name(f"{uid}.ndjson")
        os.replace(tmp_path, final)
        s.execute(text("UPDATE uploads SET path=:p WHERE id=:id"), {"p": str(final), "id": uid})
        jobs.enqueue(s, "process_upload", user_id, {"upload_id": uid})
        publish_event(s, user_id, "upload.progress", {
            "upload_id": uid, "status": "queued", "lines_done": 0, "lines_total": 0, "percent": 0,
            "rows_inserted": 0, "rows_updated": 0, "rows_duplicate": 0, "rows_rejected": 0})
        s.commit()
        return int(uid)


@router.post("/uploads", status_code=202)
async def create_upload(request: Request, user: CurrentUser = Depends(current_user)):
    cfg = get_settings()
    directory = cfg.upload_path
    await run_in_threadpool(directory.mkdir, parents=True, exist_ok=True)
    tmp = directory / f"tmp_{user.id}_{secrets.token_hex(8)}.part"
    filename = unquote(request.headers.get("x-filename") or "upload.ndjson")
    ctype = request.headers.get("content-type", "")
    size = 0
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "file"):
                raise ApiError(422, "validation_error", "Нужно поле file")
            filename = getattr(f, "filename", None) or filename

            def _copy() -> int:
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(f.file, out, 1 << 20)
                return tmp.stat().st_size

            size = await run_in_threadpool(_copy)
            if size > cfg.max_upload_bytes:
                raise ApiError(413, "too_large", "Файл больше 500 МБ")
        else:
            out = await run_in_threadpool(open, tmp, "wb")
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cfg.max_upload_bytes:
                        raise ApiError(413, "too_large", "Файл больше 500 МБ")
                    await run_in_threadpool(out.write, chunk)
            finally:
                await run_in_threadpool(out.close)
        uid = await run_in_threadpool(_register_upload, user.id, filename, size, tmp)
    except BaseException:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return JSONResponse(status_code=202, content={"upload_id": uid, "status": "queued"})


@router.get("/uploads")
def list_uploads(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM uploads WHERE user_id=:u ORDER BY id DESC LIMIT 200"),
                      {"u": user.id}).mappings().all()
    return [upload_view(dict(r)) for r in rows]


def _own_upload(db: Session, upload_id: int, user_id: int) -> dict:
    r = db.execute(text("SELECT * FROM uploads WHERE id=:id AND user_id=:u"),
                   {"id": upload_id, "u": user_id}).mappings().first()
    if r is None:
        raise ApiError(404, "not_found", "Загрузка не найдена")
    return dict(r)


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    return upload_view(_own_upload(db, upload_id, user.id), detail=True)


@router.get("/uploads/{upload_id}/errors")
def upload_errors(upload_id: int, limit: int = 100, user: CurrentUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    _own_upload(db, upload_id, user.id)
    rows = db.execute(
        text("SELECT line_no, code, message FROM upload_errors WHERE upload_id=:id ORDER BY line_no LIMIT :l"),
        {"id": upload_id, "l": max(1, min(limit, 100))},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.post("/uploads/{upload_id}/cancel", status_code=202)
def cancel_upload(upload_id: int, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    up = _own_upload(db, upload_id, user.id)
    if up["status"] not in ("queued", "processing"):
        raise ApiError(409, "not_cancellable", "Загрузка уже завершена")
    db.execute(text("UPDATE uploads SET cancel_requested=true WHERE id=:id"), {"id": upload_id})
    if up["status"] == "queued":
        db.execute(text("UPDATE uploads SET status='cancelled', finished_at=now() WHERE id=:id AND status='queued'"),
                   {"id": upload_id})
        publish_event(db, user.id, "upload.failed", {"upload_id": upload_id, "status": "cancelled"})
    db.commit()
    return {"upload_id": upload_id, "status": "cancelling" if up["status"] == "processing" else "cancelled"}


# ------------------------------------------------------------------ dev: генерация тестового файла

class _Params(dict):
    __getattr__ = dict.get


class GenerateBody(BaseModel):
    model_config = {"populate_by_name": True}
    from_: str = Field(alias="from")
    to: str
    timezone: str = "Europe/Moscow"
    preset: str = "normal"
    gaps: float = Field(default=0.0, ge=0, le=1)
    seed: int | None = None


@router.post("/dev/generate", status_code=202)
async def dev_generate(body: GenerateBody, user: CurrentUser = Depends(current_user)):
    cfg = get_settings()
    if cfg.ht_env not in ("dev", "test"):
        raise ApiError(404, "not_found", "Недоступно")
    try:
        gen = importlib.import_module("app.generator")
    except ModuleNotFoundError:
        raise ApiError(501, "not_implemented", "Генератор данных не установлен") from None
    params = _Params(**{"from": body.from_, "from_": body.from_, "to": body.to, "timezone": body.timezone,
                        "preset": body.preset, "gaps": body.gaps, "seed": body.seed})
    directory = cfg.upload_path
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"tmp_{user.id}_{secrets.token_hex(8)}.part"

    def _gen() -> int:
        n = 0
        with open(tmp, "wb") as out:
            for line in gen.iter_lines(params):
                if isinstance(line, dict):
                    line = json.dumps(line, separators=(",", ":"))
                if isinstance(line, str):
                    line = line.encode()
                out.write(line.rstrip(b"\n") + b"\n")
                n += 1
        return tmp.stat().st_size

    try:
        size = await run_in_threadpool(_gen)
        name = f"generated_{body.preset}_{body.from_}_{body.to}.ndjson"
        uid = await run_in_threadpool(_register_upload, user.id, name, size, tmp)
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        raise ApiError(422, "validation_error", str(e)) from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return {"upload_id": uid, "status": "queued"}
