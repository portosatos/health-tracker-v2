"""Генератор тестовых данных «Трекера здоровья» (формат NDJSON v1, docs/DESIGN.md, раздел 2).

Чистая библиотека: только stdlib, без БД и FastAPI. Основная точка входа:

    iter_lines(GenParams(date_from=..., date_to=..., tz="Asia/Almaty", seed=1)) -> Iterator[str]

Строки выдаются потоково, по одному локальному дню за раз (память не растёт с длиной периода).

Принципы
--------
* Детерминизм: каждый день генерируется из независимого генератора случайных чисел,
  ключ которого (seed, дата, тег). Поэтому одна и та же дата даёт ровно те же строки
  при любых границах периода (важно для проверки идемпотентности загрузки перекрывающихся файлов).
* Время «на стене» (будильник, прогулки, отбой) отсчитывается от начала локального дня
  в минутах, прошедших с локальной полуночи. В день смены пояса/перехода на летнее время
  это «прошедшие минуты», а не показания часов (день на 1 час короче/длиннее).
* Пропуски НЕ пишутся вообще. Единственные строки с нулём — `count=0` у часов, когда они
  «лежат на столе», а телефон при этом считает шаги (это данные, раздел 4.1 DESIGN.md);
  они появляются только если выбраны оба устройства.
* Ночь «вечер -> утро» относится к дате пробуждения (раздел 5.2), поэтому ночь,
  заканчивающаяся в первый день периода, начинается вечером накануне периода.
* «День без данных» D: нет ни ночи, закончившейся в D, ни дневной активности D. Ночь, начавшаяся
  вечером D (до 105 минут носки в D), остаётся: она принадлежит следующему дню, покрытие D остаётся `none`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WATCH_ID = "watch-01"
PHONE_ID = "phone-01"
DEVICE_IDS = {"watch": WATCH_ID, "phone": PHONE_ID}

# Имена пресетов (канонически через дефис; подчёркивание тоже принимается).
# Соответствуют идентификаторам правил из DESIGN.md раздел 9 + служебные сценарии.
PRESETS = {
    "sleep-short-streak": "три последние ночи сна меньше 6 часов (правило sleep_short_streak)",
    "steps-low-streak": "три последних дня меньше 50% цели шагов, дни с полным покрытием (steps_low_streak)",
    "resting-hr-elevated": "пульс покоя на 10 уд/мин выше обычного три последних дня (resting_hr_elevated)",
    "steps-week-drop": "последняя закрытая ISO-неделя с шагами ~45% от предыдущей (steps_week_drop)",
    "sleep-debt-week": "последняя закрытая ISO-неделя со сном ~6,5 ч в среднем (sleep_debt_week)",
    "peak-hr-high": "в последний день тяжёлая тренировка: 20+ минут с пульсом выше 170 (peak_hr_high)",
    "timezone-trip": "поездка: посреди периода пояс меняется на +1 час, день переезда 23 часа",
    "sleep-over-midnight": "почти все ночи начинаются после полуночи (сон целиком в одной календарной дате)",
    "data-gaps": "набор пропусков: ~10% дней без данных, пропуски часов внутри дня, телефон отсутствует 3 дня",
}


def describe_presets() -> dict[str, str]:
    """Имя пресета -> описание (для справки CLI и кнопки в UI)."""
    return dict(PRESETS)


def _norm_preset(name: str) -> str:
    n = name.strip().lower().replace("_", "-")
    if n not in PRESETS:
        raise ValueError(f"неизвестный пресет {name!r}; доступны: {', '.join(PRESETS)}")
    return n


@dataclass
class GenParams:
    """Параметры генерации."""

    date_from: date
    date_to: date
    tz: str = "UTC"                       # IANA-зона пользователя
    seed: int = 0
    devices: tuple[str, ...] = ("watch", "phone")   # подмножество {"watch","phone"}
    presets: tuple[str, ...] = ()

    # --- пропуски ---
    gap_days: tuple[date, ...] = ()       # даты без данных (устройство не носили целый день)
    gap_day_fraction: float = 0.0         # доля случайных дней без данных (0..1)
    hour_gap_prob: float = 0.0            # вероятность, что в дне выпал кусок часов (все устройства)
    hour_gap_max_hours: int = 3           # длина такого куска: 1..N часов
    hour_gaps: tuple[tuple[date, int, int], ...] = ()   # явно: (дата, час_от, час_до) локального времени
    absent_device: Optional[str] = None   # "watch" | "phone": устройство отсутствует
    absent_days: int = 0                  # сколько дней подряд
    absent_from: Optional[date] = None    # с какой даты (по умолчанию — середина периода)

    # --- сценарии (обычно задаются пресетами; 0 = выключено) ---
    short_sleep_nights: int = 0           # N последних ночей по 4,8..5,7 ч
    low_steps_days: int = 0               # N последних дней с малым числом шагов
    hr_elevated_days: int = 0             # N последних дней с повышенным пульсом покоя (+10)
    hard_workout_last_day: bool = False   # тяжёлая тренировка в последний день
    week_drop: bool = False               # последняя закрытая ISO-неделя: шагов ~45%
    sleep_debt_week: bool = False         # последняя закрытая ISO-неделя: сон ~6,5 ч
    over_midnight: bool = False           # ночи начинаются после полуночи
    trip: bool = False                    # смена пояса посреди периода
    trip_tz: Optional[str] = None         # зона назначения (по умолчанию — на +1 час к востоку)
    trip_date: Optional[date] = None      # локальная дата переезда (по умолчанию — середина периода)

    # --- детализация ---
    steps_interval_min: int = 15          # длина интервала шагов часов (1..60); телефон — чуть длиннее и сдвинут
    hr_interval_s: int = 300              # шаг точек пульса (10..3600)

    @staticmethod
    def from_dict(d: dict) -> "GenParams":
        """Собрать из JSON-подобного словаря (даты — строками YYYY-MM-DD, списки — списками)."""
        d = dict(d)

        def _d(v):
            return v if v is None or isinstance(v, date) else date.fromisoformat(v)

        d["date_from"], d["date_to"] = _d(d["date_from"]), _d(d["date_to"])
        for k in ("absent_from", "trip_date"):
            if k in d:
                d[k] = _d(d[k])
        if "gap_days" in d:
            d["gap_days"] = tuple(_d(x) for x in d["gap_days"])
        if "hour_gaps" in d:
            d["hour_gaps"] = tuple((_d(a), int(b), int(c)) for a, b, c in d["hour_gaps"])
        for k in ("devices", "presets"):
            if k in d:
                d[k] = tuple(d[k])
        return GenParams(**d)


# ---------------------------------------------------------------------------
# Таймлайн поясов
# ---------------------------------------------------------------------------

_ORD0 = 719163  # date.toordinal() для 1970-01-01


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(
            f"неизвестная часовая зона {name!r} (на Windows нужен пакет tzdata: pip install tzdata)"
        ) from e


class Timeline:
    """Таймлайн поясов: базовая зона и необязательная одна смена зоны в момент `switch_ts` (epoch, сек)."""

    def __init__(self, base: tzinfo, trip: Optional[tuple[int, tzinfo]] = None):
        self._base = base
        self._trip = trip
        self._off: dict[int, int] = {}
        self._ds: dict[date, int] = {}

    def offset_min(self, ts: int) -> int:
        k = ts // 900
        v = self._off.get(k)
        if v is None:
            tsb = k * 900
            tz = self._trip[1] if (self._trip and tsb >= self._trip[0]) else self._base
            v = int(datetime.fromtimestamp(tsb, tz).utcoffset().total_seconds() // 60)
            if len(self._off) > 100_000:
                self._off.clear()
            self._off[k] = v
        return v

    def local_ordinal(self, ts: int) -> int:
        return (ts + self.offset_min(ts) * 60) // 86400 + _ORD0

    def local_date(self, ts: int) -> date:
        return date.fromordinal(self.local_ordinal(ts))

    def day_start(self, d: date) -> int:
        """Первый момент (epoch, сек) с локальной датой d."""
        v = self._ds.get(d)
        if v is not None:
            return v
        target = d.toordinal()
        mid = (target - _ORD0) * 86400
        lo, hi = (mid - 15 * 3600) // 60, (mid + 13 * 3600) // 60   # pred(lo)=False, pred(hi)=True
        while hi - lo > 1:
            m = (lo + hi) // 2
            if self.local_ordinal(m * 60) >= target:
                hi = m
            else:
                lo = m
        v = hi * 60
        if len(self._ds) > 5000:
            self._ds.clear()
        self._ds[d] = v
        return v

    def day_length_min(self, d: date) -> int:
        return (self.day_start(d + timedelta(days=1)) - self.day_start(d)) // 60


# ---------------------------------------------------------------------------
# План: параметры -> конкретные даты сценариев
# ---------------------------------------------------------------------------

def _rnd(seed: int, d: Optional[date], tag: str) -> random.Random:
    return random.Random(f"{seed}|{d.isoformat() if d else '-'}|{tag}")


def _last_days(d_to: date, d_from: date, n: int) -> set[date]:
    return {d_to - timedelta(days=i) for i in range(n) if d_to - timedelta(days=i) >= d_from}


@dataclass
class _Night:
    start: int       # epoch
    end: int         # epoch
    kind: str        # normal | short | debt


@dataclass
class Plan:
    p: GenParams
    timeline: Timeline
    devices: tuple[str, ...]
    short_dates: set = field(default_factory=set)
    low_dates: set = field(default_factory=set)
    elevated_dates: set = field(default_factory=set)
    hard_dates: set = field(default_factory=set)
    drop_dates: set = field(default_factory=set)
    debt_dates: set = field(default_factory=set)
    protected: set = field(default_factory=set)
    gap_dates: set = field(default_factory=set)
    absent_dates: set = field(default_factory=set)
    over_midnight: bool = False
    rest_base: int = 58
    _nights: dict = field(default_factory=dict)

    def night(self, d: date) -> _Night:
        """Ночь, закончившаяся (проснулся) в локальную дату d."""
        n = self._nights.get(d)
        if n is not None:
            return n
        rng = _rnd(self.p.seed, d, "night")
        ds = self.timeline.day_start(d)
        kind = "short" if d in self.short_dates else "debt" if d in self.debt_dates else "normal"
        over = self.over_midnight and rng.random() < 0.85
        x = rng.randint(15, 100)            # отбой за x минут до полуночи
        y = rng.randint(15, 90)             # или через y минут после неё
        if kind == "short":
            length = rng.randint(290, 340)
        elif kind == "debt":
            length = rng.randint(370, 405)
        else:
            length = rng.randint(440, 500) if over else rng.randint(440, 520)
        start = ds + y * 60 if over else ds - x * 60
        n = _Night(start, start + length * 60, kind)
        if len(self._nights) > 400:
            self._nights.clear()
        self._nights[d] = n
        return n


def _find_closed_week(d_from: date, d_to: date) -> tuple[date, date]:
    """Последняя ISO-неделя, закрытая к d_to (воскресенье <= d_to - 1) и целиком внутри периода."""
    sunday = d_to - timedelta(days=1)
    while sunday.isoweekday() != 7:
        sunday -= timedelta(days=1)
    monday = sunday - timedelta(days=6)
    if monday < d_from:
        raise ValueError("для пресета с «закрытой неделей» в периоде нет полной ISO-недели, закрытой к дате «до»")
    return monday, sunday


def build_plan(p: GenParams) -> Plan:
    """Проверить параметры и превратить пресеты в конкретные даты."""
    if p.date_from > p.date_to:
        raise ValueError("дата «с» позже даты «по»")
    if (p.date_to - p.date_from).days > 800:
        raise ValueError("слишком длинный период (максимум ~800 дней)")
    devs = tuple(dict.fromkeys(p.devices))
    if not devs or any(x not in DEVICE_IDS for x in devs):
        raise ValueError("devices: подмножество {'watch','phone'}, не пусто")
    if p.absent_device is not None and p.absent_device not in DEVICE_IDS:
        raise ValueError("absent_device: 'watch' или 'phone'")
    if not (1 <= p.steps_interval_min <= 60):
        raise ValueError("steps_interval_min: 1..60")
    if not (10 <= p.hr_interval_s <= 3600):
        raise ValueError("hr_interval_s: 10..3600")
    for name, v in (("gap_day_fraction", p.gap_day_fraction), ("hour_gap_prob", p.hour_gap_prob)):
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"{name}: 0..1")
    base = _load_tz(p.tz)

    # Копия параметров с применёнными пресетами (исходный объект не меняем).
    q = GenParams(**{**p.__dict__})
    for raw in p.presets:
        name = _norm_preset(raw)
        if name == "sleep-short-streak":
            q.short_sleep_nights = q.short_sleep_nights or 3
        elif name == "steps-low-streak":
            q.low_steps_days = q.low_steps_days or 3
        elif name == "resting-hr-elevated":
            q.hr_elevated_days = q.hr_elevated_days or 3
        elif name == "steps-week-drop":
            q.week_drop = True
        elif name == "sleep-debt-week":
            q.sleep_debt_week = True
        elif name == "peak-hr-high":
            q.hard_workout_last_day = True
        elif name == "timezone-trip":
            q.trip = True
        elif name == "sleep-over-midnight":
            q.over_midnight = True
        elif name == "data-gaps":
            q.gap_day_fraction = q.gap_day_fraction or 0.1
            q.hour_gap_prob = q.hour_gap_prob or 0.25
            if q.absent_device is None and len(devs) > 1:
                q.absent_device, q.absent_days = "phone", 3

    plan = Plan(p=q, timeline=Timeline(base), devices=devs, over_midnight=q.over_midnight)
    plan.rest_base = 54 + int(_rnd(q.seed, None, "user").uniform(0, 8))

    d0, d1 = q.date_from, q.date_to
    plan.short_dates = _last_days(d1, d0, q.short_sleep_nights)
    plan.low_dates = _last_days(d1, d0, q.low_steps_days)
    plan.elevated_dates = _last_days(d1, d0, q.hr_elevated_days)
    if q.hard_workout_last_day:
        plan.hard_dates = {d1}
    if q.week_drop or q.sleep_debt_week:
        monday, sunday = _find_closed_week(d0, d1)
        week = {monday + timedelta(days=i) for i in range(7)}
        if q.week_drop:
            plan.drop_dates = week
        if q.sleep_debt_week:
            # ночи недели W: даты пробуждения Пн..Вс
            plan.debt_dates = week
    plan.protected = (plan.short_dates | plan.low_dates | plan.elevated_dates | plan.hard_dates
                      | plan.drop_dates | plan.debt_dates)

    # Пропуски
    gaps = set(q.gap_days)
    if q.gap_day_fraction > 0:
        d = d0
        while d <= d1:
            if d not in plan.protected and _rnd(q.seed, d, "gap").random() < q.gap_day_fraction:
                gaps.add(d)
            d += timedelta(days=1)
    plan.gap_dates = {g for g in gaps if d0 <= g <= d1}
    if q.absent_device and q.absent_days > 0:
        n_days = (d1 - d0).days + 1
        start = q.absent_from or d0 + timedelta(days=max(0, (n_days - q.absent_days) // 2))
        plan.absent_dates = {start + timedelta(days=i) for i in range(q.absent_days)}

    # Поездка
    if q.trip:
        n_days = (d1 - d0).days + 1
        if n_days < 4:
            raise ValueError("для поездки нужно минимум 4 дня")
        trip_date = q.trip_date or d0 + timedelta(days=n_days // 2)
        if not (d0 < trip_date < d1):
            raise ValueError("дата поездки должна быть внутри периода, не первым и не последним днём")
        switch = Timeline(base).day_start(trip_date) + 14 * 3600     # перелёт в 14:00 по старому поясу
        plan.timeline = Timeline(base, (switch, _pick_trip_tz(q.trip_tz, base, switch)))
        plan.p.trip_date = trip_date
    return plan


def _pick_trip_tz(name: Optional[str], base: tzinfo, switch: int) -> tzinfo:
    if name:
        return _load_tz(name)
    base_off = int(datetime.fromtimestamp(switch, base).utcoffset().total_seconds() // 60)
    for cand in ("Asia/Almaty", "Asia/Omsk", "Asia/Novosibirsk", "Asia/Yekaterinburg", "Europe/Samara",
                 "Europe/Moscow", "Europe/Kaliningrad", "Europe/Berlin", "Europe/London", "Asia/Tashkent",
                 "Asia/Dubai", "Asia/Bangkok", "Asia/Tokyo"):
        try:
            tz = ZoneInfo(cand)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
        if int(datetime.fromtimestamp(switch, tz).utcoffset().total_seconds() // 60) == base_off + 60:
            return tz
    return timezone(timedelta(minutes=base_off + 60))


# ---------------------------------------------------------------------------
# Генерация одного дня
# ---------------------------------------------------------------------------

def _line_sleep(dev: str, s: int, e: int, stage: str, off: int) -> str:
    return (f'{{"v":1,"type":"sleep","device_id":"{dev}","start":"{_iso(s)}","end":"{_iso(e)}",'
            f'"stage":"{stage}","offset_min":{off}}}')


def _line_steps(dev: str, s: int, e: int, count: int, off: int) -> str:
    return (f'{{"v":1,"type":"steps","device_id":"{dev}","start":"{_iso(s)}","end":"{_iso(e)}",'
            f'"count":{count},"offset_min":{off}}}')


def _line_hr(dev: str, ts: int, bpm: int, off: int) -> str:
    return f'{{"v":1,"type":"heart_rate","device_id":"{dev}","ts":"{_iso(ts)}","bpm":{bpm},"offset_min":{off}}}'


def _stages(rng: random.Random, length: int, awake_budget: int) -> list[tuple[str, int, int]]:
    """Разбить ночь длиной length минут на стадии (минуты от начала ночи)."""
    seq: list[tuple[str, int, int]] = []
    cycle = ("light", "deep", "light", "rem")
    t = i = 0
    while t < length:
        st = cycle[i % 4]
        dur = {"light": rng.randint(20, 45), "deep": rng.randint(25, 55), "rem": rng.randint(10, 30)}[st]
        dur = min(dur, length - t)
        seq.append((st, t, t + dur))
        t += dur
        i += 1
        if i % 4 == 0 and t < length and awake_budget >= 2 and rng.random() < 0.6:
            a = min(rng.randint(2, 8), awake_budget, length - t)
            seq.append(("awake", t, t + a))
            t += a
            awake_budget -= a
    return seq


def _day_lines(plan: Plan, d: date) -> list[str]:
    p = plan.p
    if d in plan.gap_dates:
        return []
    tl = plan.timeline
    ds, de = tl.day_start(d), tl.day_start(d + timedelta(days=1))
    day_len = (de - ds) // 60
    absent = d in plan.absent_dates
    watch_on = "watch" in plan.devices and not (absent and p.absent_device == "watch")
    phone_on = "phone" in plan.devices and not (absent and p.absent_device == "phone")
    if not (watch_on or phone_on):
        return []

    out: list[tuple[int, str]] = []
    off = tl.offset_min

    rest = plan.rest_base + _rnd(p.seed, d, "rest").uniform(-1.5, 1.5) + (10 if d in plan.elevated_dates else 0)

    # ---------------- ночь, закончившаяся в d ----------------
    night = plan.night(d)
    nrng = _rnd(p.seed, d, "nightdev")
    watch_night = watch_on and nrng.random() >= 0.05
    phone_night = phone_on and (not watch_night or nrng.random() < 0.7)
    n_len = (night.end - night.start) // 60
    if watch_night:
        srng = _rnd(p.seed, d, "stages")
        budget = 0 if night.kind == "debt" else 16
        for st, a, b in _stages(srng, n_len, budget):
            s, e = night.start + a * 60, night.start + b * 60
            out.append((s, _line_sleep(WATCH_ID, s, e, st, off(s))))
        hrng = _rnd(p.seed, d, "hrnight")
        t = night.start + hrng.randint(0, p.hr_interval_s)
        while t < night.end:
            bpm = int(round(rest + hrng.uniform(-3, 3)))
            out.append((t, _line_hr(WATCH_ID, t, max(25, min(250, bpm)), off(t))))
            t += max(10, p.hr_interval_s + hrng.randint(-p.hr_interval_s // 5, p.hr_interval_s // 5))
    if phone_night:
        prng = _rnd(p.seed, d, "phonesleep")
        s = night.start + prng.randint(0, 15) * 60
        e = night.end - prng.randint(0, 10) * 60
        out.append((s, _line_sleep(PHONE_ID, s, e, "asleep", off(s))))

    # ---------------- дневная активность ----------------
    wake = (night.end - ds) // 60
    nxt = plan.night(d + timedelta(days=1))
    end = min((nxt.start - ds) // 60, day_len)
    rng = _rnd(p.seed, d, "day")
    low = d in plan.low_dates
    drop = d in plan.drop_dates
    hard = d in plan.hard_dates
    f = rng.uniform(0.8, 1.15) * (0.45 if drop else 1.0)
    bgf = 0.4 if low else (0.5 if drop else 1.0)

    walks: list[tuple[int, int, float]] = []      # (старт-минута, длительность, шагов в минуту)
    s_c, d_c, n_c = wake + rng.randint(45, 90), rng.randint(20, 30), rng.randint(1800, 2800)
    s_l, d_l, n_l = rng.randint(750, 795), rng.randint(15, 20), rng.randint(900, 1500)
    work_roll, eve_roll = rng.random(), rng.random()
    s_w = rng.randint(1080, 1200)
    d_w = 45 if hard else rng.randint(35, 50)
    n_w = d_w * rng.randint(110, 150)
    s_e, d_e, n_e = rng.randint(1050, 1170), rng.randint(30, 60), rng.randint(3000, 5500)
    peak_normal = rng.randint(148, 165)
    peak_hard = rng.randint(178, 188)
    nap_roll = rng.random()
    s_n, d_n = rng.randint(840, 930), rng.randint(30, 80)
    off_roll = rng.random()
    s_o, d_o = rng.randint(540, 900), rng.randint(60, 120)

    workout: Optional[tuple[int, int, int]] = None   # (старт, длительность, пик)
    if low:
        walks.append((s_l, d_l, n_l * 0.5 / d_l))
    else:
        walks.append((s_c, d_c, n_c * f / d_c))
        walks.append((s_l, d_l, n_l * f / d_l))
        if hard or work_roll < 0.35:
            walks.append((s_w, d_w, n_w * f / d_w))
            workout = (s_w, d_w, peak_hard if hard else peak_normal)
        elif eve_roll < 0.8:
            walks.append((s_e, d_e, n_e * f / d_e))
    nap = None
    if nap_roll < 0.15 and d not in plan.short_dates and d not in plan.debt_dates and not low:
        nap = (s_n, d_n)
    offwrist = None
    if off_roll < 0.10 and watch_on and phone_on:
        offwrist = (s_o, d_o)

    # истинные шаги по минутам
    arr: list[int] = []
    for m in range(wake, max(wake, end)):
        v = rng.uniform(0, 6) * bgf
        for ws, wd, rate in walks:
            if ws <= m < ws + wd:
                v += rate * rng.uniform(0.85, 1.15)
        if nap and nap[0] <= m < nap[0] + nap[1]:
            v = 0.0
        arr.append(int(v + 0.5))

    # окна выпавших часов (все устройства)
    drops: list[tuple[int, int]] = []
    hg = _rnd(p.seed, d, "hgap")
    if p.hour_gap_prob > 0 and hg.random() < p.hour_gap_prob:
        ln = hg.randint(1, max(1, p.hour_gap_max_hours))
        h0 = hg.randint(8, max(8, 22 - ln))
        drops.append((ds + h0 * 3600, ds + (h0 + ln) * 3600))
    for gd, h0, h1 in p.hour_gaps:
        if gd == d:
            drops.append((ds + h0 * 3600, ds + h1 * 3600))

    def dropped(a: int, b: int) -> bool:
        return any(a < hi and b > lo for lo, hi in drops)

    B = p.steps_interval_min
    # --- часы: шаги ---
    if watch_on:
        wrng = _rnd(p.seed, d, "wsteps")
        t0 = -(-wake // B) * B
        while t0 + B <= end:
            s, e = ds + t0 * 60, ds + (t0 + B) * 60
            cnt = sum(arr[t0 - wake:t0 - wake + B])
            noise = wrng.uniform(0.97, 1.03)
            if offwrist and t0 < offwrist[0] + offwrist[1] and t0 + B > offwrist[0]:
                cnt = 0
            else:
                cnt = int(cnt * noise + 0.5)
            if not dropped(s, e):
                out.append((s, _line_steps(WATCH_ID, s, e, cnt, off(s))))
            t0 += B
    # --- телефон: шаги (интервал длиннее и сдвинут -> пересекается с часами, числа чуть меньше) ---
    if phone_on:
        prng = _rnd(p.seed, d, "psteps")
        pb = B + max(1, B // 3)
        phase = 5 if pb > 5 else 1
        t0 = phase + (-(-(wake - phase) // pb)) * pb
        while t0 + pb <= end:
            skip = prng.random() < 0.12          # телефон остался дома
            k = prng.uniform(0.85, 0.95)
            s, e = ds + t0 * 60, ds + (t0 + pb) * 60
            if not skip and not dropped(s, e):
                cnt = int(sum(arr[t0 - wake:t0 - wake + pb]) * k + 0.5)
                out.append((s, _line_steps(PHONE_ID, s, e, cnt, off(s))))
            t0 += pb

    # --- часы: пульс днём ---
    if watch_on:
        hrng = _rnd(p.seed, d, "hrday")
        t = ds + wake * 60 + hrng.randint(0, p.hr_interval_s)
        t_end = ds + end * 60
        base_day = rest + 12
        while t < t_end:
            m = (t - ds) // 60
            step = max(10, p.hr_interval_s + hrng.randint(-p.hr_interval_s // 5, p.hr_interval_s // 5))
            in_off = offwrist and offwrist[0] <= m < offwrist[0] + offwrist[1]
            if workout and workout[0] <= m < workout[0] + workout[1]:
                step = 30
                ws, wd, pk = workout
                x = (t - ds) / 60 - ws
                start_v = rest + 25
                if x < 5:
                    bpm = start_v + (pk - 10 - start_v) * x / 5
                elif x > wd - 3:
                    bpm = pk - 10 - (x - (wd - 3)) * 12
                else:
                    bpm = pk - hrng.uniform(0, 6)
                if not hard:
                    bpm = min(bpm, 168)
            elif nap and nap[0] <= m < nap[0] + nap[1]:
                bpm = rest + 3 + hrng.uniform(-2, 2)
            else:
                idx = m - wake
                rate = arr[idx] if 0 <= idx < len(arr) else 0
                bpm = base_day + min(55.0, rate * 0.45) + hrng.gauss(0, 2.5)
            if not in_off and not dropped(t, t + 1):
                out.append((t, _line_hr(WATCH_ID, t, max(25, min(250, int(round(bpm)))), off(t))))
            t += step

    # --- дневной сон ---
    if nap:
        s, e = ds + nap[0] * 60, ds + (nap[0] + nap[1]) * 60
        if not dropped(s, e):
            nrng2 = _rnd(p.seed, d, "napdev")
            use_phone = nrng2.random() < 0.5
            if watch_on and not (offwrist and nap[0] < offwrist[0] + offwrist[1] and nap[0] + nap[1] > offwrist[0]):
                out.append((s, _line_sleep(WATCH_ID, s, e, "asleep", off(s))))
            if phone_on and (use_phone or not watch_on):
                out.append((s + 60, _line_sleep(PHONE_ID, s + 60, e - 60, "asleep", off(s + 60))))

    out.sort(key=lambda x: x[0])
    return [ln for _, ln in out]


def _device_lines(plan: Plan) -> list[str]:
    lines = []
    if "watch" in plan.devices:
        lines.append(f'{{"v":1,"type":"device","device_id":"{WATCH_ID}","kind":"watch","model":"Acme W2"}}')
    if "phone" in plan.devices:
        lines.append(f'{{"v":1,"type":"device","device_id":"{PHONE_ID}","kind":"phone","model":"Pixel"}}')
    return lines


def _iter(plan: Plan, d0: date, d1: date) -> Iterator[str]:
    yield from _device_lines(plan)
    d = d0
    while d <= d1:
        yield from _day_lines(plan, d)
        d += timedelta(days=1)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def iter_lines(params: GenParams, date_from: Optional[date] = None,
               date_to: Optional[date] = None) -> Iterator[str]:
    """Потоково выдать строки NDJSON (без перевода строки в конце).

    Параметры проверяются сразу (ValueError), сами данные — лениво. `date_from`/`date_to`
    ограничивают выдачу подпериодом: сценарии/пропуски/пояса считаются по всему `params`,
    поэтому значения за одну и ту же дату совпадают во всех кусках.
    """
    plan = build_plan(params)
    d0 = max(date_from or params.date_from, params.date_from)
    d1 = min(date_to or params.date_to, params.date_to)
    return _iter(plan, d0, d1)


def split_ranges(params: GenParams, parts: int, overlap_days: int = 0) -> list[tuple[date, date]]:
    """Разбить период на `parts` кусков; каждый (кроме первого) начинается на `overlap_days` раньше."""
    if parts < 1 or overlap_days < 0:
        raise ValueError("parts >= 1, overlap_days >= 0")
    total = (params.date_to - params.date_from).days + 1
    parts = min(parts, total)
    res = []
    for i in range(parts):
        a = params.date_from + timedelta(days=total * i // parts)
        b = params.date_from + timedelta(days=total * (i + 1) // parts - 1)
        if i > 0:
            a = max(params.date_from, a - timedelta(days=overlap_days))
        res.append((a, b))
    return res


def day_length_min(params: GenParams, d: date) -> int:
    """Длина локального дня d в минутах с учётом поездки/летнего времени (1380/1440/1500)."""
    return build_plan(params).timeline.day_length_min(d)


def lines_to_text(lines: Iterable[str]) -> Iterator[str]:
    """Добавить '\\n' к каждой строке (удобно для StreamingResponse)."""
    for ln in lines:
        yield ln + "\n"
