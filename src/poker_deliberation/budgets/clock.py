"""Injectable monotonic clocks for deterministic budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic_ns
from typing import Protocol


class MonotonicClock(Protocol):
    def now_ns(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SystemMonotonicClock:
    def now_ns(self) -> int:
        return monotonic_ns()


@dataclass(slots=True)
class FakeMonotonicClock:
    """A manually advanced clock; rollback is allowed so consumers can reject it."""

    current_ns: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.current_ns, bool) or not isinstance(self.current_ns, int):
            raise TypeError("current_ns must be an integer")
        if self.current_ns < 0:
            raise ValueError("current_ns must be non-negative")

    def now_ns(self) -> int:
        return self.current_ns

    def advance_ns(self, delta_ns: int) -> None:
        if isinstance(delta_ns, bool) or not isinstance(delta_ns, int):
            raise TypeError("delta_ns must be an integer")
        if delta_ns < 0:
            raise ValueError("delta_ns must be non-negative")
        self.current_ns += delta_ns

    def set_ns(self, value_ns: int) -> None:
        if isinstance(value_ns, bool) or not isinstance(value_ns, int):
            raise TypeError("value_ns must be an integer")
        if value_ns < 0:
            raise ValueError("value_ns must be non-negative")
        self.current_ns = value_ns
