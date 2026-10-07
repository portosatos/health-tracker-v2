"""Серийные правила по дням: sleep_short_streak и steps_low_streak (DESIGN, раздел 9)."""
from __future__ import annotations

from datetime import date, timedelta

from .base import Ctx, Finding, Result, Rule, Verdict, fmt_date, fmt_minutes, runs

MIN_STREAK = 3
SLEEP_SHORT_MIN = 360


class _DayStreak(Rule):
    """Серия >= 3 плохих дней подряд. День учитывается только при полных данных (_value is not None).

    Серия активна, если её последний день >= as_of-1. S = первая дата серии, ключ `streak:<S>`.
    Закрытие: любой день с данными, начиная с S, оказался «хорошим» (исправление данных или следующая нормальная ночь).
    """

    def _value(self, ctx: Ctx, d: date) -> int | None:
        raise NotImplementedError

    def _threshold(self, ctx: Ctx) -> float | None:
        raise NotImplementedError

    def _texts(self, start: date, end: date, vals: list[int], thr: float) -> tuple[str, str]:
        raise NotImplementedError

    def find(self, ctx: Ctx) -> list[Finding]:
        thr = self._threshold(ctx)
        if thr is None:
            return []

        def bad(d: date) -> bool:
            v = self._value(ctx, d)
            return v is not None and v < thr

        out = []
        for s, e in runs(ctx.window_start, ctx.as_of, bad):
            n = (e - s).days + 1
            if n < MIN_STREAK or e < ctx.as_of - timedelta(days=1):
                continue
            nxt = self._value(ctx, e + timedelta(days=1))
            if nxt is not None and nxt >= thr:
                continue  # серию закончил нормальный день с данными: ситуация устранена (CLEARED)
            vals = [self._value(ctx, s + timedelta(days=i)) for i in range(n)]
            title, body = self._texts(s, e, vals, thr)  # type: ignore[arg-type]
            out.append(Finding(f"streak:{s.isoformat()}", title, body, {
                "start": s.isoformat(), "end": e.isoformat(), "days": n, "values": vals, "threshold": thr,
            }))
        return out

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        thr = self._threshold(ctx)
        if thr is None:
            return Verdict(Result.UNKNOWN)
        s = date.fromisoformat(key.split(":", 1)[1])
        d = s
        while d <= ctx.as_of:
            v = self._value(ctx, d)
            if v is not None and v >= thr:
                return Verdict(Result.CLEARED)
            d += timedelta(days=1)
        return Verdict(Result.UNKNOWN)


class SleepShortStreak(_DayStreak):
    id = "sleep_short_streak"
    title = "Несколько коротких ночей подряд"
    description = ("Срабатывает, если три ночи подряд вы спали меньше 6 часов. "
                   "Ночи без данных серию прерывают и рекомендацию не закрывают.")
    params = {"threshold_minutes": SLEEP_SHORT_MIN, "streak_days": MIN_STREAK, "window_days": 30}

    def _value(self, ctx, d):
        r = ctx.row(d)
        return r["sleep_minutes"] if r and r["sleep_status"] == "ok" and r["sleep_minutes"] is not None else None

    def _threshold(self, ctx):
        return SLEEP_SHORT_MIN

    def _texts(self, s, e, vals, thr):
        n = len(vals)
        return (
            "Вы мало спите уже несколько ночей",
            f"Ночей подряд меньше 6 часов сна: {n} (с {fmt_date(s)} по {fmt_date(e)}), "
            f"в среднем {fmt_minutes(sum(vals) / n)}. Постарайтесь лечь пораньше и дать организму восстановиться.",
        )


class StepsLowStreak(_DayStreak):
    id = "steps_low_streak"
    title = "Мало шагов несколько дней подряд"
    description = ("Срабатывает, если три дня подряд вы проходили меньше половины своей цели по шагам "
                   "(учитываются только дни с полными данными; нужна активная цель по шагам).")
    params = {"fraction_of_goal": 0.5, "streak_days": MIN_STREAK, "window_days": 30}

    def _value(self, ctx, d):
        r = ctx.row(d)
        return r["steps"] if r and r["steps_status"] == "complete" and r["steps"] is not None else None

    def _threshold(self, ctx):
        t = ctx.goals.get("steps_per_day")
        return None if not t else 0.5 * t

    def _texts(self, s, e, vals, thr):
        n = len(vals)
        return (
            "Вы мало двигаетесь последние дни",
            f"{n} дн. подряд (с {fmt_date(s)} по {fmt_date(e)}) шагов меньше половины цели "
            f"({int(thr * 2)}): в среднем {round(sum(vals) / n)} в день. Попробуйте добавить короткую прогулку.",
        )
