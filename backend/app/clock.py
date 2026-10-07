"""Единственный источник «сейчас». В тестах подменяется HT_FAKE_NOW или set_fake_now()."""
from __future__ import annotations

import os
from datetime import datetime, timezone

_fake: datetime | None = None


def set_fake_now(dt: datetime | None) -> None:
    global _fake
    _fake = dt


def now() -> datetime:
    if _fake is not None:
        return _fake
    env = os.environ.get("HT_FAKE_NOW")
    if env:
        return datetime.fromisoformat(env.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)
