"""Small incremental (streaming) indicators.

Every indicator is updated one bar at a time so the exact same code runs in
backtests and in live trading.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .models import Bar


class EMA:
    def __init__(self, period: int):
        if period < 1:
            raise ValueError("EMA period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: Optional[float] = None
        self.count = 0
        self._seed: list[float] = []

    def update(self, x: float) -> Optional[float]:
        self.count += 1
        if self.value is None:
            # Seed with a simple average of the first `period` values.
            self._seed.append(x)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
                self._seed = []
            return self.value
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class ATR:
    """Wilder's Average True Range."""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._seed: list[float] = []

    def update(self, bar: Bar) -> Optional[float]:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
                self._seed = []
            return self.value
        self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class SessionVWAP:
    """Volume-weighted average price that the caller resets each session."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._pv = 0.0
        self._v = 0.0
        self.value: Optional[float] = None

    def update(self, bar: Bar) -> Optional[float]:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = bar.volume if bar.volume and bar.volume > 0 else 1.0
        self._pv += typical * vol
        self._v += vol
        self.value = self._pv / self._v
        return self.value

    @property
    def ready(self) -> bool:
        return self.value is not None


class RollingWindow:
    """Fixed-length history of recent values (newest last)."""

    def __init__(self, size: int):
        self._d: deque = deque(maxlen=size)

    def append(self, x) -> None:
        self._d.append(x)

    def __len__(self) -> int:
        return len(self._d)

    def __getitem__(self, i):
        return self._d[i]

    @property
    def full(self) -> bool:
        return len(self._d) == self._d.maxlen

    def values(self) -> list:
        return list(self._d)
