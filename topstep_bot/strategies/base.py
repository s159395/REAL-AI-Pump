"""Strategy interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

from ..models import Bar, Instrument, Signal
from ..sessions import SessionClock


@dataclass
class StrategyContext:
    """What the engine tells a strategy when it evaluates a bar."""

    now: datetime  # bar close time
    trading_day: date
    flat: bool  # True if the account has no open position
    entries_today: int  # entries this strategy has taken today


class Strategy:
    name = "base"

    def __init__(self, params: dict, instrument: Instrument, clock: SessionClock):
        self.p = dict(params or {})
        self.instrument = instrument
        self.clock = clock
        self.timeframe = int(self.p.get("timeframe", 5))
        self.current_day: Optional[date] = None

    # Called by the engine on every completed bar of `self.timeframe` minutes.
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Optional[Signal]:
        if ctx.trading_day != self.current_day:
            self.current_day = ctx.trading_day
            self.on_new_day(ctx.trading_day)
        return self.evaluate(bar, ctx)

    def on_new_day(self, day: date) -> None:  # pragma: no cover - default no-op
        pass

    def evaluate(self, bar: Bar, ctx: StrategyContext) -> Optional[Signal]:
        raise NotImplementedError

    # ---- helpers -----------------------------------------------------
    def local_time(self, ts: datetime) -> time:
        return self.clock.local(ts).time()

    def tick(self) -> float:
        return self.instrument.tick_size

    def r(self, price: float) -> float:
        return self.instrument.round_to_tick(price)
