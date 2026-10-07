"""Правила по пульсу: resting_hr_elevated и peak_hr_high (DESIGN, раздел 9)."""
from __future__ import annotations

from datetime import date, timedelta

from ...summary import half_up_median
from .base import Ctx, Finding, Result, Rule, Verdict, daterange, fmt_date

DELTA_BPM = 7
BASELINE_DAYS = 14
BASELINE_MIN_VALUES = 7
PEAK_BPM = 170
PEAK_MINUTES = 10
PEAK_EXPIRE_DAYS = 3


class RestingHrElevated(Rule):
    id = "resting_hr_elevated"
    title = "Пульс покоя повышен"
    description = ("Срабатывает, если два дня подряд пульс покоя на 7 и более ударов в минуту выше вашей обычной "
                   "нормы (медианы за 14 дней до начала повышения; нужно не меньше 7 измерений). "
                   "Дни без пульса покоя серию прерывают.")
    params = {"delta_bpm": DELTA_BPM, "streak_days": 2, "baseline_days": BASELINE_DAYS,
              "baseline_min_values": BASELINE_MIN_VALUES, "window_days": 30}

    @staticmethod
    def _rhr(ctx: Ctx, d: date) -> int | None:
        r = ctx.row(d)
        return r["resting_hr"] if r and r["resting_hr"] is not None else None

    def _baseline(self, ctx: Ctx, s: date) -> int | None:
        vals = [v for v in (self._rhr(ctx, s - timedelta(days=k)) for k in range(1, BASELINE_DAYS + 1))
                if v is not None]
        return half_up_median(vals) if len(vals) >= BASELINE_MIN_VALUES else None

    def find(self, ctx: Ctx) -> list[Finding]:
        out: list[Finding] = []
        d = ctx.window_start
        while d <= ctx.as_of:
            v = self._rhr(ctx, d)
            base = self._baseline(ctx, d) if v is not None else None
            if base is None or v < base + DELTA_BPM:
                d += timedelta(days=1)
                continue
            e = d
            while e + timedelta(days=1) <= ctx.as_of:
                nv = self._rhr(ctx, e + timedelta(days=1))
                if nv is None or nv < base + DELTA_BPM:
                    break
                e += timedelta(days=1)
            n = (e - d).days + 1
            nxt = self._rhr(ctx, e + timedelta(days=1))
            ended = nxt is not None and nxt < base + DELTA_BPM  # серию закончил нормальный день: CLEARED
            if n >= 2 and e >= ctx.as_of - timedelta(days=1) and not ended:
                vals = [self._rhr(ctx, d + timedelta(days=i)) for i in range(n)]
                out.append(Finding(
                    f"streak:{d.isoformat()}", "Пульс покоя выше обычного",
                    f"Пульс покоя {n} дн. подряд (с {fmt_date(d)} по {fmt_date(e)}) выше вашей нормы "
                    f"({base} уд/мин) на {DELTA_BPM} и более: {', '.join(str(x) for x in vals)}. "
                    f"Такое бывает при усталости, стрессе или начале болезни — понаблюдайте за самочувствием.",
                    {"start": d.isoformat(), "end": e.isoformat(), "days": n, "values": vals,
                     "baseline": base, "threshold": base + DELTA_BPM},
                ))
            d = e + timedelta(days=1)
        return out

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        s = date.fromisoformat(key.split(":", 1)[1])
        base = evidence.get("baseline")
        if base is None:
            return Verdict(Result.UNKNOWN)
        for d in daterange(s, ctx.as_of):
            v = self._rhr(ctx, d)
            if v is not None and v < base + DELTA_BPM:
                return Verdict(Result.CLEARED)
        return Verdict(Result.UNKNOWN)


class PeakHrHigh(Rule):
    id = "peak_hr_high"
    title = "Высокий пиковый пульс"
    description = ("Срабатывает, если за один из последних трёх дней пульс держался на уровне 170 уд/мин и выше "
                   "не меньше 10 минут. Дни без данных о пульсе не учитываются.")
    params = {"bpm": PEAK_BPM, "minutes": PEAK_MINUTES, "window_days": 3}

    def find(self, ctx: Ctx) -> list[Finding]:
        out = []
        for d in daterange(ctx.as_of - timedelta(days=2), ctx.as_of):
            n = ctx.hr_high(d)
            if n is not None and n >= PEAK_MINUTES:
                out.append(Finding(
                    f"day:{d.isoformat()}", "Высокий пульс при нагрузке",
                    f"{fmt_date(d)} пульс был 170 уд/мин и выше в течение {n} мин. "
                    f"Если это была тренировка, дайте себе время на восстановление; если нет — стоит обратить внимание.",
                    {"date": d.isoformat(), "minutes_high": n, "bpm": PEAK_BPM},
                ))
        return out

    def judge(self, ctx: Ctx, key: str, evidence: dict) -> Verdict:
        d = date.fromisoformat(key.split(":", 1)[1])
        n = ctx.hr_high(d)
        if n is not None and n < PEAK_MINUTES:
            return Verdict(Result.CLEARED)  # исправление данных
        if (ctx.as_of - d).days >= PEAK_EXPIRE_DAYS:
            return Verdict(Result.CLEARED, "expired")
        return Verdict(Result.UNKNOWN)
