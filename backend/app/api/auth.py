"""Аутентификация: регистрация, вход, выход, /auth/me."""
from __future__ import annotations

import secrets
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import NEG_INF
from ..summary import as_of_date, load_timeline
from .deps import COOKIE, ApiError, CurrentUser, current_user, get_db, hash_token

router = APIRouter(prefix="/auth", tags=["auth"])
_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")


class Credentials(BaseModel):
    login: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    timezone: str | None = None


class LoginBody(BaseModel):
    login: str
    password: str


def _issue_session(db: Session, response: Response, user_id: int) -> None:
    cfg = get_settings()
    token = secrets.token_urlsafe(32)
    db.execute(
        text(
            """INSERT INTO auth_sessions (user_id, token_hash, expires_at)
               VALUES (:u, :h, now() + make_interval(days => :d))"""
        ),
        {"u": user_id, "h": hash_token(token), "d": cfg.session_days},
    )
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/", max_age=cfg.session_days * 86400)


@router.post("/register", status_code=201)
def register(body: Credentials, response: Response, db: Session = Depends(get_db)):
    login = body.login.strip()
    if len(login) < 3:
        raise ApiError(422, "validation_error", "Логин должен быть не короче 3 символов")
    tz = body.timezone or get_settings().default_timezone
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        raise ApiError(422, "validation_error", "Неизвестный часовой пояс") from None
    try:
        uid = db.execute(
            text("INSERT INTO users (login, password_hash) VALUES (:l, :p) RETURNING id"),
            {"l": login, "p": _ph.hash(body.password)},
        ).scalar_one()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "login_taken", "Такой логин уже занят") from None
    db.execute(text("INSERT INTO user_settings (user_id) VALUES (:u)"), {"u": uid})
    db.execute(
        text("INSERT INTO tz_periods (user_id, valid_from, tz_name, source) VALUES (:u, :v, :z, 'initial')"),
        {"u": uid, "v": NEG_INF, "z": tz},
    )
    _issue_session(db, response, uid)
    db.commit()
    return {"id": uid, "login": login}


@router.post("/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT id, login, password_hash FROM users WHERE login = :l"), {"l": body.login.strip()}
    ).first()
    try:
        _ph.verify(row[2] if row else _DUMMY_HASH, body.password)
        ok = row is not None
    except VerificationError:
        ok = False
    if not ok:
        raise ApiError(401, "bad_credentials", "Неверный логин или пароль")
    _issue_session(db, response, row[0])
    db.commit()
    return {"id": row[0], "login": row[1]}


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    resp = Response(status_code=204)
    if token:
        db.execute(text("DELETE FROM auth_sessions WHERE token_hash = :h"), {"h": hash_token(token)})
        db.commit()
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/me")
def me(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    tl = load_timeline(db, user.id)
    as_of = as_of_date(db, user.id, tl)
    return {"id": user.id, "login": user.login, "timezone": tl.current_name(),
            "as_of": as_of.isoformat() if as_of else None}
