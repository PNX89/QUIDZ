"""The only place in the package allowed to read the wall clock.

Every module that needs the time takes a Clock or an explicit `now: float`. That is what lets
the tolerance window boundary and the backoff schedule be tested without a real sleep and
without monkeypatching the time module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FakeClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Unix seconds."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class FakeClock:
    t: float = 0.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds
