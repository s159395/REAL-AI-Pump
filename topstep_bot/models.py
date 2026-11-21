"""Core data types shared by strategies, brokers, the engine and the backtester."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(Enum):
    LONG = 1
    SHORT = -1

    @property
    def sign(self) -> int:
        return self.value

    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


@dataclass(frozen=True)
class Bar:
    """An OHLCV bar. ``ts`` is the bar's OPEN time (timezone-aware)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Instrument:
    """Contract specification. Point value = tick_value / tick_size."""

    symbol: str
    tick_size: float
    tick_value: float
    contract_id: Optional[str] = None
    # How many of these contracts count as one "mini" for Topstep's position limit
    # (micros count as 1/10 of a mini).
    mini_equivalent: float = 1.0

    @property
    def point_value(self) -> float:
        return self.tick_value / self.tick_size

    def round_to_tick(self, price: float) -> float:
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 10)

    def to_ticks(self, distance: float) -> int:
        """Convert a price distance to a whole number of ticks (rounded)."""
        return int(round(abs(distance) / self.tick_size))

    def floor_ticks(self, distance: float) -> int:
        return int(math.floor(abs(distance) / self.tick_size + 1e-9))


# Default specs for common CME index/commodity futures. Live trading pulls the
# exact tick size/value from the TopstepX contract search instead.
KNOWN_INSTRUMENTS = {
    "ES": Instrument("ES", 0.25, 12.50),
    "MES": Instrument("MES", 0.25, 1.25, mini_equivalent=0.1),
    "NQ": Instrument("NQ", 0.25, 5.00),
    "MNQ": Instrument("MNQ", 0.25, 0.50, mini_equivalent=0.1),
    "YM": Instrument("YM", 1.0, 5.00),
    "MYM": Instrument("MYM", 1.0, 0.50, mini_equivalent=0.1),
    "RTY": Instrument("RTY", 0.10, 5.00),
    "M2K": Instrument("M2K", 0.10, 0.50, mini_equivalent=0.1),
    "CL": Instrument("CL", 0.01, 10.00),
    "MCL": Instrument("MCL", 0.01, 1.00, mini_equivalent=0.1),
    "GC": Instrument("GC", 0.10, 10.00),
    "MGC": Instrument("MGC", 0.10, 1.00, mini_equivalent=0.1),
}


@dataclass
class Signal:
    """A strategy's request to enter a trade.

    ``entry_ref`` is the price the strategy expects to get (usually the signal
    bar's close). Stops and targets are converted into tick distances from
    ``entry_ref`` and re-applied to the actual fill price, exactly the way a
    server-side bracket order works.
    """

    side: Side
    entry_ref: float
    stop: float
    target: float
    strategy: str
    reason: str
    ts: datetime

    @property
    def risk_points(self) -> float:
        return abs(self.entry_ref - self.stop)

    @property
    def reward_points(self) -> float:
        return abs(self.target - self.entry_ref)


@dataclass
class Bracket:
    """Stop/target distances in ticks, relative to the fill price."""

    stop_ticks: int
    target_ticks: int

    def prices(self, instrument: Instrument, side: Side, fill_price: float) -> tuple[float, float]:
        tick = instrument.tick_size
        stop = fill_price - side.sign * self.stop_ticks * tick
        target = fill_price + side.sign * self.target_ticks * tick
        return instrument.round_to_tick(stop), instrument.round_to_tick(target)


@dataclass
class OpenPosition:
    side: Side
    size: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    strategy: str
    reason: str
    initial_stop: float = 0.0
    moved_to_breakeven: bool = False
    max_favorable: float = 0.0  # best excursion in points

    def unrealized(self, price: float, instrument: Instrument) -> float:
        return (price - self.entry_price) * self.side.sign * self.size * instrument.point_value

    @property
    def initial_risk_points(self) -> float:
        return abs(self.entry_price - self.initial_stop)


@dataclass
class TradeRecord:
    strategy: str
    side: Side
    size: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gross_pnl: float
    fees: float
    exit_reason: str
    reason: str = ""
    initial_risk_dollars: float = 0.0
    trading_day: Optional[object] = None
    extra: dict = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees

    @property
    def r_multiple(self) -> float:
        if self.initial_risk_dollars <= 0:
            return 0.0
        return self.net_pnl / self.initial_risk_dollars
