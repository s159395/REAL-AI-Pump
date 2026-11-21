"""Broker interface used by the trading engine.

The engine is identical in backtests, paper trading and live trading; only the
broker implementation changes. That keeps backtest results honest: the live
bot runs exactly the logic that was tested.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models import Bar, Bracket, OpenPosition, Signal, TradeRecord


class Broker(ABC):
    position: Optional[OpenPosition] = None

    @property
    @abstractmethod
    def is_flat(self) -> bool:
        """No open position and no pending entry."""

    @abstractmethod
    def submit_entry(self, signal: Signal, size: int, bracket: Bracket, now: datetime) -> bool:
        """Enter at market with a protective stop and a profit target."""

    @abstractmethod
    def process_bar(self, bar: Bar) -> list[TradeRecord]:
        """Advance to the close of `bar`; return trades that closed."""

    @abstractmethod
    def flatten(self, reason: str, price: float, now: datetime) -> list[TradeRecord]:
        """Close everything at market and cancel working orders."""

    @abstractmethod
    def modify_stop(self, new_stop: float) -> bool:
        """Move the protective stop (e.g. to breakeven)."""

    def heartbeat(self, now: datetime) -> list[TradeRecord]:
        """Optional between-bar sync (live only)."""
        return []
