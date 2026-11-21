"""Simulated broker for backtests and paper trading.

Fill model (deliberately conservative):
  * Market entries fill at the NEXT 1-minute bar's open, plus slippage.
  * Stop and target are applied as tick offsets from the actual fill price,
    like a server-side bracket order.
  * Stops fill at the stop price minus slippage (or at the open if the bar
    gaps through the stop).
  * Targets fill only if price trades THROUGH the target by `limit_through_ticks`.
  * If a single bar touches both stop and target, the stop is assumed to fill first.
  * Commission is charged per contract per round turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ..models import Bar, Bracket, Instrument, OpenPosition, Side, Signal, TradeRecord
from .base import Broker


@dataclass
class _PendingEntry:
    signal: Signal
    size: int
    bracket: Bracket
    submitted: datetime


class SimBroker(Broker):
    def __init__(
        self,
        instrument: Instrument,
        commission_per_contract: float = 0.74,
        slippage_ticks: int = 1,
        limit_through_ticks: int = 1,
    ):
        self.inst = instrument
        self.commission = float(commission_per_contract)
        self.slip = int(slippage_ticks) * instrument.tick_size
        self.through = int(limit_through_ticks) * instrument.tick_size
        self.position: Optional[OpenPosition] = None
        self.pending: Optional[_PendingEntry] = None
        self.last_price: Optional[float] = None

    @property
    def is_flat(self) -> bool:
        return self.position is None and self.pending is None

    def submit_entry(self, signal: Signal, size: int, bracket: Bracket, now: datetime) -> bool:
        if not self.is_flat or size <= 0:
            return False
        self.pending = _PendingEntry(signal, size, bracket, now)
        return True

    def modify_stop(self, new_stop: float) -> bool:
        if self.position is None:
            return False
        self.position.stop_price = self.inst.round_to_tick(new_stop)
        return True

    def _close(self, price: float, ts: datetime, reason: str) -> TradeRecord:
        pos = self.position
        assert pos is not None
        gross = (price - pos.entry_price) * pos.side.sign * pos.size * self.inst.point_value
        fees = self.commission * pos.size
        risk_dollars = self.inst.to_ticks(pos.entry_price - pos.initial_stop) * self.inst.tick_value * pos.size
        rec = TradeRecord(
            strategy=pos.strategy,
            side=pos.side,
            size=pos.size,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=ts,
            exit_price=self.inst.round_to_tick(price),
            gross_pnl=round(gross, 2),
            fees=round(fees, 2),
            exit_reason=reason,
            reason=pos.reason,
            initial_risk_dollars=risk_dollars,
        )
        self.position = None
        return rec

    def process_bar(self, bar: Bar) -> list[TradeRecord]:
        closed: list[TradeRecord] = []
        bar_end = bar.ts + timedelta(minutes=1)
        if self.pending is not None:
            pe = self.pending
            self.pending = None
            side = pe.signal.side
            fill = self.inst.round_to_tick(bar.open + side.sign * self.slip)
            stop, target = pe.bracket.prices(self.inst, side, fill)
            self.position = OpenPosition(
                side=side,
                size=pe.size,
                entry_price=fill,
                entry_time=bar.ts,
                stop_price=stop,
                target_price=target,
                strategy=pe.signal.strategy,
                reason=pe.signal.reason,
                initial_stop=stop,
            )
            entry_bar = True
        else:
            entry_bar = False

        pos = self.position
        if pos is not None:
            if pos.side is Side.LONG:
                if not entry_bar and bar.open <= pos.stop_price:
                    closed.append(self._close(bar.open - self.slip, bar_end, "stop"))
                elif not entry_bar and bar.open >= pos.target_price + self.through:
                    closed.append(self._close(bar.open, bar_end, "target"))
                elif bar.low <= pos.stop_price:
                    closed.append(self._close(pos.stop_price - self.slip, bar_end, "stop"))
                elif bar.high >= pos.target_price + self.through:
                    closed.append(self._close(pos.target_price, bar_end, "target"))
                else:
                    pos.max_favorable = max(pos.max_favorable, bar.high - pos.entry_price)
            else:
                if not entry_bar and bar.open >= pos.stop_price:
                    closed.append(self._close(bar.open + self.slip, bar_end, "stop"))
                elif not entry_bar and bar.open <= pos.target_price - self.through:
                    closed.append(self._close(bar.open, bar_end, "target"))
                elif bar.high >= pos.stop_price:
                    closed.append(self._close(pos.stop_price + self.slip, bar_end, "stop"))
                elif bar.low <= pos.target_price - self.through:
                    closed.append(self._close(pos.target_price, bar_end, "target"))
                else:
                    pos.max_favorable = max(pos.max_favorable, pos.entry_price - bar.low)
        self.last_price = bar.close
        return closed

    def flatten(self, reason: str, price: float, now: datetime) -> list[TradeRecord]:
        self.pending = None
        if self.position is None:
            return []
        exit_px = price - self.position.side.sign * self.slip
        return [self._close(exit_px, now, reason)]
