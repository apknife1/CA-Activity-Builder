from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
from time import perf_counter
from typing import Any


class LogMode(str, Enum):
    LIVE = "live"
    DEBUG = "debug"
    TRACE = "trace"


class Cat(str, Enum):
    NAV = "NAV"
    SECTION = "SECTION"
    SIDEBAR = "SIDEBAR"
    DROP = "DROP"
    PHANTOM = "PHANTOM"
    FROALA = "FROALA"
    TABLE = "TABLE"
    RETRY = "RETRY"
    UISTATE = "UISTATE"
    REG = "REG"
    STARTUP = "STARTUP"
    CONFIGURE = "CONFIGURE"
    PROPS = "PROPS"


@dataclass(frozen=True)
class InstrumentPolicy:
    mode: LogMode = LogMode.LIVE

    # Category minimum mode needed to emit *diagnostic* logs.
    # Signal logs are always allowed via emit_signal().
    diag_min_mode: dict[Cat, LogMode] = field(default_factory=dict)

    # If True, include ctx keys in all emitted lines.
    include_ctx: bool = True

    # Rate limits for noisy events (key -> seconds).
    rate_limits_s: dict[str, float] = field(default_factory=dict)


@dataclass
class Counters:
    _c: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def inc(self, key: str, n: int = 1) -> None:
        self._c[key] += n

    def get(self, key: str) -> int:
        return self._c.get(key, 0)

    def snapshot(self) -> dict[str, int]:
        return dict(self._c)


@dataclass
class RateLimiter:
    _last: dict[str, float] = field(default_factory=dict)

    def allow(self, key: str, every_s: float) -> bool:
        now = perf_counter()
        last = self._last.get(key)
        if last is None or (now - last) >= every_s:
            self._last[key] = now
            return True
        return False


def format_ctx(**ctx: Any) -> str:
    # Stable ordering makes grep life easier
    order = ["act", "sec", "fid", "type", "fi", "a"]
    parts = []
    for k in order:
        v = ctx.get(k)
        if v is None:
            continue
        parts.append(f"{k}={v}")
    # include any extras in alpha order
    extras = sorted((k, v) for k, v in ctx.items() if k not in order and v is not None)
    parts.extend([f"{k}={v}" for k, v in extras])
    return " ".join(parts)
