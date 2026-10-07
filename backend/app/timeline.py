"""Таймлайн часовых поясов и локальные дни (DESIGN, раздел 5).

Внутреннее представление времени — целые «эпох-минуты» (UTC-минуты от 1970-01-01).
Каждая UTC-минута получает ровно одну локальную дату => данные не теряются и не удваиваются.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import NEG_INF

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_ORD = date(1970, 1, 1).toordinal()
NEG_MIN = -(10**12)


def to_min(dt: datetime) -> int:
    """datetime (aware) -> эпох-минуты (пол)."""
    if dt <= NEG_INF:
        return NEG_MIN
    return int((dt - EPOCH) // timedelta(minutes=1))


def from_min(m: int) -> datetime:
    return EPOCH + timedelta(minutes=m)


def date_to_n(d: date) -> int:
    return d.toordinal() - EPOCH_ORD


def n_to_date(n: int) -> date:
    return date.fromordinal(EPOCH_ORD + n)


class Timeline:
    """periods: список (valid_from: datetime, tz_name|None, fixed_offset_min|None)."""

    def __init__(self, periods: list[tuple[datetime, str | None, int | None]]):
        items = sorted(periods, key=lambda p: p[0])
        if not items:
            items = [(NEG_INF, "UTC", None)]
        self.froms = [to_min(p[0]) for p in items]
        self.zones: list[ZoneInfo | None] = [ZoneInfo(p[1]) if p[1] else None for p in items]
        self.fixed = [p[2] for p in items]
        self.names = [p[1] for p in items]

    def _idx(self, t: int) -> int:
        return max(bisect_right(self.froms, t) - 1, 0)

    def offset_at(self, t: int) -> int:
        i = self._idx(t)
        z = self.zones[i]
        if z is None:
            return int(self.fixed[i] or 0)
        off = from_min(t).astimezone(z).utcoffset()
        return int(off.total_seconds() // 60) if off is not None else 0

    def local_date(self, t: int) -> date:
        return n_to_date((t + self.offset_at(t)) // 1440)

    def segments(self, lo: int, hi: int) -> list[tuple[int, int, int]]:
        """Куски [s,e) с постоянным смещением на [lo,hi)."""
        cuts = sorted({lo, hi, *[f for f in self.froms if lo < f < hi]})
        out: list[tuple[int, int, int]] = []
        for s, e in zip(cuts, cuts[1:]):
            i = self._idx(s)
            if self.zones[i] is None:
                out.append((s, e, int(self.fixed[i] or 0)))
                continue
            samples = list(range(s, e, 180)) + [e - 1]
            cur_s, cur_o = s, self.offset_at(s)
            for t0, t1 in zip(samples, samples[1:]):
                o1 = self.offset_at(t1)
                if o1 != cur_o:
                    lo_, hi_ = t0 + 1, t1  # первая минута с offset != cur_o
                    while lo_ < hi_:
                        mid = (lo_ + hi_) // 2
                        if self.offset_at(mid) == cur_o:
                            lo_ = mid + 1
                        else:
                            hi_ = mid
                    out.append((cur_s, lo_, cur_o))
                    cur_s, cur_o = lo_, o1
            out.append((cur_s, e, cur_o))
        # склеить соседей с одинаковым смещением
        merged: list[tuple[int, int, int]] = []
        for s, e, o in out:
            if merged and merged[-1][1] == s and merged[-1][2] == o:
                merged[-1] = (merged[-1][0], e, o)
            else:
                merged.append((s, e, o))
        return merged

    def day_ranges(self, d: date) -> list[tuple[int, int]]:
        """UTC-диапазоны [a,b) эпох-минут, у которых local_date == d."""
        n = date_to_n(d)
        ls, le = n * 1440, (n + 1) * 1440
        out: list[tuple[int, int]] = []
        for s, e, o in self.segments(ls - 840 - 1, le + 720 + 1):
            a, b = max(s, ls - o), min(e, le - o)
            if a < b:
                if out and out[-1][1] == a:
                    out[-1] = (out[-1][0], b)
                else:
                    out.append((a, b))
        return out

    def day_bounds(self, d: date) -> tuple[int, int, int]:
        """(day_start, day_end, day_length_min) в эпох-минутах."""
        r = self.day_ranges(d)
        if not r:  # недостижимо при корректном таймлайне
            n = date_to_n(d) * 1440
            return n, n + 1440, 1440
        return r[0][0], r[-1][1], sum(b - a for a, b in r)

    def current_name(self, t: int | None = None) -> str:
        i = self._idx(t) if t is not None else len(self.froms) - 1
        if self.names[i]:
            return self.names[i]  # type: ignore[return-value]
        o = int(self.fixed[i] or 0)
        sign = "+" if o >= 0 else "-"
        return f"UTC{sign}{abs(o) // 60:02d}:{abs(o) % 60:02d}"
