"""Недельные правила: steps_week_drop и sleep_debt_week (DESIGN, раздел 9)."""
from __future__ import annotations

from datetime import date, timedelta

from .base import Ctx, Finding, Result, Rule, Verdict, closed_week_mondays, daterange, fmt_date, fmt_minutes, \
    parse_week_key, week_key

MIN_VALID_STEPS_DAYS = 4
DROP_RATIO = 0.7
RECOVER_RATIO = 0.8
MIN_SLEEP_NIGHTS = 5
DEBT_MIN = 60
RECOVER_DEBT_MIN = 30


def _steps_days(ctx: Ctx, days: list[date]) -> list[int]:
    out = []
    for d in days:
        r = ctx.row(d)
        if r and r["steps_status"] == "complete" and r["steps"] is not None:
            out.append(r["steps"])
    return out


def _sleep_nights(ctx: Ctx, days: list[date]) -> list[int]:
    out = []
    for d in days:
        r = ctx.row(d)
        if r and r["sleep_status"] == "ok" and r["sleep_minutes"] is not None:
            out.append(r["sleep_minutes"])
    return out


def _week_days(mon: date) -> list[date]:
    return [mon + timedelta(days=i) for i in range(7)]


def _avg(v: list[int]) -> float:
    return sum(v) / len(v)


def _last7(ctx: Ctx) -> list[date]:
    return daterange(ctx.as_of - timedelta(days=6), ctx.as_of)


class StepsWeekDrop(Rule):
    id = "steps_week_drop"
    title = "Активность за неделю заметно упала"
    description = ("Срабатывает, если среднее число шагов за закрытую неделю не больше 70% от предыдущей недели "
                   "(в каждой неделе нужно не меньше 4 дней с полными данными).")
    params = {"max_ratio": DROP_RATIO, "min_valid_days": MIN_VALID_STEPS_DAYS, "weeks": 5}

    def _pair(self, ctx: Ctx, mon: date):
        cur = _steps_days(ctx, _week_days(mon))
        prev = _steps_days(ctx, _week_days(mon - timedelta(days=7)))
        if len(cur) < MIN_VALID_STEPS_DAYS or len(prev) < MIN_VALID_STEPS_DAYS:
            return None
        return _avg(cur), _avg(prev), len(cur), len(prev)

    def _recovered(self, ctx: Ctx, prev_avg: float) -> bool:
        v = _steps_days(ctx, _last7(ctx))
        return len(v) >= MIN_VALID_STEPS_DAYS and _avg(v) >= RECOVER_RATIO * prev_avg

    def find(self, ctx: Ctx) -> list[Finding]:
        out = []
        for mon in closed_week_mondays(ctx.as_of):
            p = self._pair(ctx, mon)
            if p is None:
                continue
            cur, prev, nc, np_ = p
            if cur <= DROP_RATIO * prev and not self._recovered(ctx, prev):
                pct = round((1 - cur / prev) * 100)
                out.append(Finding(
                    f"week:{week_key(mon)}", "Активность за неделю упала",
                    f"На неделе {fmt_date(mon)} – {fmt_date(mon + timedelta(days=6))} вы проходили в среднем "
                    f"{round(cur)} шагов в день — на {pct}% меньше, чем неделей раньше ({round(prev)}). "
                    f"Попробуйте вернуться к привычному ритму.",
                    {"week": week_key(mon), "steps_avg": round(cur), "prev_steps_avg": round(prev),
                     "drop_pct": pct, "valid_days": nc, "prev_valid_days": np_},
                ))
        return out

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        mon = parse_week_key(key.split(":", 1)[1])
        p = self._pair(ctx, mon)
        if p is None:
            return Verdict(Result.UNKNOWN)
        cur, prev, _, _ = p
        if cur > DROP_RATIO * prev or self._recovered(ctx, prev):
            return Verdict(Result.CLEARED)
        return Verdict(Result.UNKNOWN)


class SleepDebtWeek(Rule):
    id = "sleep_debt_week"
    title = "Недосып за неделю"
    description = ("Срабатывает, если за закрытую неделю (минимум 5 ночей с данными) средний сон хотя бы на час "
                   "меньше вашей цели по сну. Нужна активная цель по сну.")
    params = {"debt_minutes": DEBT_MIN, "min_nights": MIN_SLEEP_NIGHTS, "weeks": 5}

    def _recovered(self, ctx: Ctx, target: float) -> bool:
        v = _sleep_nights(ctx, _last7(ctx))
        return len(v) >= MIN_SLEEP_NIGHTS and _avg(v) >= target - RECOVER_DEBT_MIN

    def find(self, ctx: Ctx) -> list[Finding]:
        target = ctx.goals.get("sleep_minutes_per_day")
        if not target:
            return []
        out = []
        for mon in closed_week_mondays(ctx.as_of):
            v = _sleep_nights(ctx, _week_days(mon))
            if len(v) < MIN_SLEEP_NIGHTS:
                continue
            avg = _avg(v)
            if avg <= target - DEBT_MIN and not self._recovered(ctx, target):
                out.append(Finding(
                    f"week:{week_key(mon)}", "Недосып за неделю",
                    f"На неделе {fmt_date(mon)} – {fmt_date(mon + timedelta(days=6))} вы спали в среднем "
                    f"{fmt_minutes(avg)} при цели {fmt_minutes(target)}. Накопился недосып — "
                    f"постарайтесь в ближайшие дни ложиться пораньше.",
                    {"week": week_key(mon), "sleep_avg": round(avg), "target": target, "nights": len(v)},
                ))
        return out

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        target = ctx.goals.get("sleep_minutes_per_day") or evidence.get("target")
        if not target:
            return Verdict(Result.UNKNOWN)
        mon = parse_week_key(key.split(":", 1)[1])
        v = _sleep_nights(ctx, _week_days(mon))
        if len(v) < MIN_SLEEP_NIGHTS:
            return Verdict(Result.UNKNOWN)
        if _avg(v) > target - DEBT_MIN or self._recovered(ctx, target):
            return Verdict(Result.CLEARED)
        return Verdict(Result.UNKNOWN)
