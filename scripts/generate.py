#!/usr/bin/env python
"""CLI генератора тестовых данных «Трекера здоровья» (NDJSON v1).

Примеры:
  python scripts/generate.py --from 2026-09-01 --to 2026-09-30 --tz Asia/Almaty --seed 1 \
      --preset sleep-short-streak --out file.ndjson
  python scripts/generate.py --from 2025-01-01 --to 2026-01-01 --steps-interval 1 --hr-interval 60 > big.ndjson
  python scripts/generate.py --from 2026-03-01 --to 2026-03-31 --split 3 --overlap-days 2 --out part.ndjson
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_generator():
    """Подключить backend/app/generator.py по пути (без импорта пакета app и его зависимостей)."""
    path = ROOT / "backend" / "app" / "generator.py"
    spec = importlib.util.spec_from_file_location("ht_generator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ht_generator"] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load_generator()


def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидалась дата YYYY-MM-DD, получено {s!r}")


def _parse_args(argv=None) -> argparse.Namespace:
    presets = "\n".join(f"  {k}: {v}" for k, v in gen.describe_presets().items())
    ap = argparse.ArgumentParser(
        description="Генератор тестовых данных (NDJSON v1, docs/DESIGN.md раздел 2).",
        epilog="Пресеты:\n" + presets,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--from", dest="date_from", type=_date, required=True, help="первая дата периода YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", type=_date, required=True, help="последняя дата периода (включительно)")
    ap.add_argument("--tz", default="UTC", help="часовой пояс пользователя, IANA (по умолчанию UTC)")
    ap.add_argument("--seed", type=int, default=0, help="seed: тот же seed и параметры дают те же данные")
    ap.add_argument("--devices", default="watch,phone",
                    help="устройства через запятую: watch, phone (по умолчанию оба)")
    ap.add_argument("--preset", action="append", default=[],
                    help="сценарий-пресет (можно несколько раз или через запятую), см. список ниже")
    ap.add_argument("--list-presets", action="store_true", help="показать пресеты и выйти")
    g = ap.add_argument_group("пропуски (пропущенное не пишется вообще)")
    g.add_argument("--gap-days", default="", help="даты без данных через запятую: 2026-09-05,2026-09-06")
    g.add_argument("--gap-day-fraction", type=float, default=0.0, help="доля случайных дней без данных, 0..1")
    g.add_argument("--gap-hours-prob", type=float, default=0.0,
                   help="вероятность, что в дне выпал кусок часов (на всех устройствах), 0..1")
    g.add_argument("--gap-hours-max", type=int, default=3, help="макс. длина выпавшего куска, часов (по умолчанию 3)")
    g.add_argument("--gap-hours", action="append", default=[],
                   help="явный пропуск часов ДАТА:ЧАС-ЧАС, напр. 2026-09-05:10-13 (локальное время; можно повторять)")
    g.add_argument("--absent", default="",
                   help="устройство отсутствует N дней: УСТРОЙСТВО:N[:ДАТА], напр. phone:5 или watch:3:2026-09-10")
    t = ap.add_argument_group("поездка (пресет timezone-trip)")
    t.add_argument("--trip-tz", help="зона назначения (по умолчанию — на +1 час к востоку от --tz)")
    t.add_argument("--trip-date", type=_date, help="локальная дата переезда (по умолчанию середина периода)")
    d = ap.add_argument_group("детализация (для большого объёма)")
    d.add_argument("--steps-interval", type=int, default=15, help="длина интервала шагов часов, минут (1..60; 15)")
    d.add_argument("--hr-interval", type=int, default=300, help="шаг точек пульса, секунд (10..3600; 300)")
    o = ap.add_argument_group("вывод")
    o.add_argument("--out", default="-", help="файл вывода; '-' (по умолчанию) — stdout")
    o.add_argument("--split", type=int, default=1,
                   help="разбить на N файлов (имя.part1.ext, ...), требует --out файл")
    o.add_argument("--overlap-days", type=int, default=0,
                   help="с --split: каждый следующий файл начинается на N дней раньше (перекрытие)")
    return ap.parse_args(argv)


def _params(a: argparse.Namespace):
    presets = tuple(x for chunk in a.preset for x in chunk.split(",") if x.strip())
    absent_device, absent_days, absent_from = None, 0, None
    if a.absent:
        parts = a.absent.split(":", 2)
        absent_device, absent_days = parts[0], int(parts[1])
        if len(parts) > 2:
            absent_from = date.fromisoformat(parts[2])
    hour_gaps = []
    for s in a.gap_hours:
        day, rng_ = s.split(":")
        h0, h1 = rng_.split("-")
        hour_gaps.append((date.fromisoformat(day), int(h0), int(h1)))
    return gen.GenParams(
        date_from=a.date_from, date_to=a.date_to, tz=a.tz, seed=a.seed,
        devices=tuple(x.strip() for x in a.devices.split(",") if x.strip()),
        presets=presets,
        gap_days=tuple(date.fromisoformat(x) for x in a.gap_days.split(",") if x.strip()),
        gap_day_fraction=a.gap_day_fraction, hour_gap_prob=a.gap_hours_prob,
        hour_gap_max_hours=a.gap_hours_max, hour_gaps=tuple(hour_gaps),
        absent_device=absent_device, absent_days=absent_days, absent_from=absent_from,
        trip_tz=a.trip_tz, trip_date=a.trip_date,
        steps_interval_min=a.steps_interval, hr_interval_s=a.hr_interval,
    )


def _write(lines, out_path: str) -> int:
    n = 0
    if out_path == "-":
        w = sys.stdout.buffer
        for ln in lines:
            w.write(ln.encode("utf-8") + b"\n")      # всегда \n, без перевода строки Windows
            n += 1
        w.flush()
    else:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            for ln in lines:
                f.write(ln)
                f.write("\n")
                n += 1
    return n


def main(argv=None) -> int:
    a = _parse_args(argv)
    if a.list_presets:
        for k, v in gen.describe_presets().items():
            print(f"{k}: {v}")
        return 0
    try:
        params = _params(a)
        if a.split > 1:
            if a.out == "-":
                raise ValueError("--split требует --out файл")
            ranges = gen.split_ranges(params, a.split, a.overlap_days)
            p = Path(a.out)
            for i, (d0, d1) in enumerate(ranges, 1):
                path = p.with_name(f"{p.stem}.part{i}{p.suffix}")
                n = _write(gen.iter_lines(params, d0, d1), str(path))
                print(f"{path}: {d0}..{d1}, строк: {n}", file=sys.stderr)
        else:
            n = _write(gen.iter_lines(params), a.out)
            print(f"записано строк: {n}", file=sys.stderr)
    except ValueError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
