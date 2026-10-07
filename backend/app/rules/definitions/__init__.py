"""Каталог правил (хранится в коде; у пользователя — только enabled в rule_settings)."""
from __future__ import annotations

from .base import Ctx, Finding, Result, Rule, Verdict
from .heart import PeakHrHigh, RestingHrElevated
from .streaks import SleepShortStreak, StepsLowStreak
from .weekly import SleepDebtWeek, StepsWeekDrop

RULES: list[Rule] = [SleepShortStreak(), StepsLowStreak(), RestingHrElevated(), StepsWeekDrop(), SleepDebtWeek(),
                     PeakHrHigh()]
BY_ID: dict[str, Rule] = {r.id: r for r in RULES}

__all__ = ["RULES", "BY_ID", "Ctx", "Finding", "Result", "Rule", "Verdict"]
