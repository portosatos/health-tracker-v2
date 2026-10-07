"""Базовые типы правил: результат оценки, контекст, помощники для серий и недель (DESIGN, раздел 9)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any, Callable

WINDOW_DAYS = 30          # окно оценки [as_of-29, as_of]
ROWS_BACK_DAYS = 62       # сколько дней сводок грузим (для недельных правил нужны 6 недель)


class Result(str, Enum):
    ACTIVE = "ACTIVE"      # условие выполняется по полным данным
    CLEARED = "CLEARED"    # данные однозначно показывают, что ситуация устранена
    UNKNOWN = "UNKNOWN"    # данных для суждения нет: открытая остаётся открытой, новая не создаётся


@dataclass
class Finding:
    """Активная ситуация: ключ дедупликации, тексты и доказательства (evidence — только JSON-типы)."""
    key: str
    title: str
    body: str
    evidence: dict[str, Any]


@dataclass
class Verdict:
    result: Result
    reason: str = "auto_cleared"   # причина закрытия при CLEARED: auto_cleared | expired


@dataclass
class Ctx:
    user_id: int
    as_of: date
    rows: dict[date, dict]                      # daily_summary по датам (нет строки = нет данных)
    goals: dict[str, int] = field(default_factory=dict)   # активные цели: kind -> target
    hr_high: Callable[[date], int | None] = lambda d: None  # минут пульса >= 170 в дне (None = нет пульса)

    @property
    def window_start(self) -> date:
        return self.as_of - timedelta(days=WINDOW_DAYS - 1)

    def row(self, d: date) -> dict | None:
        return self.rows.get(d)


class Rule:
    id: str = ""
    title: str = ""
    description: str = ""     # видно пользователю
    params: dict[str, Any] = {}

    def find(self, ctx: Ctx) -> list[Finding]:
        """Все ситуации, которые сейчас ACTIVE."""
        raise NotImplementedError

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        """Оценка ОТКРЫТОЙ рекомендации, которой нет среди find(): CLEARED или UNKNOWN."""
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "description": self.description, "params": dict(self.params)}


# ---------------------------------------------------------------- помощники

def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fmt_minutes(m: float) -> str:
    m = int(round(m))
    h, mm = divmod(m, 60)
    return f"{h} ч {mm:02d} мин" if mm else f"{h} ч"


def daterange(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def runs(lo: date, hi: date, bad: Callable[[date], bool]) -> list[tuple[date, date]]:
    """Максимальные отрезки подряд идущих дат, где bad(d) истинно. Пропуск (bad=False) разрывает серию."""
    out: list[tuple[date, date]] = []
    start: date | None = None
    prev: date | None = None
    for d in daterange(lo, hi):
        if bad(d):
            if start is None:
                start = d
            prev = d
        elif start is not None:
            out.append((start, prev))  # type: ignore[arg-type]
            start = None
    if start is not None:
        out.append((start, prev))  # type: ignore[arg-type]
    return out


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_key(mon: date) -> str:
    y, w, _ = mon.isocalendar()
    return f"{y}-W{w:02d}"


def parse_week_key(key: str) -> date:
    y, w = key.split("-W")
    return date.fromisocalendar(int(y), int(w), 1)


def closed_week_mondays(as_of: date, n: int = 5) -> list[date]:
    """Понедельники n последних закрытых ISO-недель (неделя закрыта, когда as_of уже в следующей)."""
    cur = monday(as_of)
    return [cur - timedelta(days=7 * k) for k in range(1, n + 1)]
