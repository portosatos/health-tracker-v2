"""Зависимости API: ошибки, сессия БД, текущий пользователь (cookie ht_session)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import new_session

COOKIE = "ht_session"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def get_db() -> Iterator[Session]:
    s = new_session()
    try:
        yield s
    finally:
        s.close()


@dataclass
class CurrentUser:
    id: int
    login: str


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    token = request.cookies.get(COOKIE)
    if not token:
        raise ApiError(401, "unauthorized", "Требуется вход")
    row = db.execute(
        text(
            """SELECT u.id, u.login, s.id, s.expires_at < now() + make_interval(days => :keep) AS needs_slide
               FROM auth_sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = :h AND s.expires_at > now()"""
        ),
        {"h": hash_token(token), "keep": get_settings().session_days - 1},
    ).first()
    if row is None:
        raise ApiError(401, "unauthorized", "Сессия недействительна или истекла")
    if row[3]:  # скользящие 14 дней; обновляем не чаще раза в сутки
        db.execute(
            text("UPDATE auth_sessions SET expires_at = now() + make_interval(days => :d) WHERE id = :id"),
            {"d": get_settings().session_days, "id": row[2]},
        )
        db.commit()
    return CurrentUser(id=row[0], login=row[1])
