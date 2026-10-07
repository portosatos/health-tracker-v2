"""API-процесс (uvicorn app.main:app --port 8000)."""
from __future__ import annotations

import asyncio
import importlib.util
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import auth, deps, devices, events, goals, settings, summary, uploads
from .api.deps import ApiError, error_response
from .events import hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.start(asyncio.get_running_loop())
    yield
    hub.stop()


app = FastAPI(title="Трекер здоровья", lifespan=lifespan)

for r in (auth.router, devices.router, uploads.router, summary.router, goals.router, settings.router,
          events.router):
    app.include_router(r, prefix="/api")

# ---------------------------------------------------------------------------------------
# ПОДКЛЮЧИТЬ rules/mail: эндпоинты рекомендаций/правил/писем (GET /rules, PATCH /rules/{id},
# GET /recommendations, POST /recommendations/{id}/feedback, GET /outbox, GET /dev/mailbox)
# пишутся отдельно. Роутеры подхватываются автоматически, если модуль существует и содержит `router`
# (префикс /api добавляется здесь). Хуки воркера — см. app/hooks.py:
#   * app.rules.engine.evaluate(user_id, session) — после пересчёта (job evaluate_rules);
#   * app.mail.JOB_HANDLERS — обработчики jobs flush_digest/send_outbox.
# ---------------------------------------------------------------------------------------
for _name in ("app.rules.api", "app.rules.router", "app.mail_api", "app.mail_router"):
    try:
        if importlib.util.find_spec(_name) is not None:
            _mod = importlib.import_module(_name)
            if hasattr(_mod, "router"):
                app.include_router(_mod.router, prefix="/api")
    except ModuleNotFoundError:
        pass


class CsrfGuard:
    """Мутирующие запросы к /api требуют X-Requested-With: ht (простая защита от CSRF). Чистый ASGI."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE")                 and scope["path"].startswith("/api/"):
            hdrs = {k.lower(): v for k, v in scope["headers"]}
            if hdrs.get(b"x-requested-with") != b"ht":
                resp = error_response(403, "csrf", "Нужен заголовок X-Requested-With: ht")
                await resp(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app.add_middleware(CsrfGuard)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return error_response(exc.status, exc.code, exc.message)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException):
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
        exc.status_code, "error")
    return error_response(exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
    return error_response(422, "validation_error", msg or "Ошибка валидации")
