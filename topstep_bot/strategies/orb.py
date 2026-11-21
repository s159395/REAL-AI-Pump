"""Opening Range Breakout (ORB).

The first N minutes after the 8:30 CT cash open set the day's opening range.
A close beyond that range, in the direction of VWAP and the higher-timeframe
trend, often starts the day's main move. This is one of the most widely used
intraday index-futures setups because the stop has a clear structural level
(the range midpoint or the far side of the range).

Rules (all configurable):
  * Opening range = high/low of bars from rth_open to rth_open + or_minutes.
  * Long: a bar CLOSES above OR high + buffer, close > session VWAP,
    close > trend EMA. Short is the mirror image.
  * Stop: OR midpoint ("mid") or the far side of the range ("opposite"),
    widened to at least min_stop_points; skipped if wider than max_stop_points.
  * Target: target_r x risk.
  * Only before entry_end (default 10:30 CT), max one entry per day by default,
    and never the same direction twice in one day.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..indicators import EMA, SessionVWAP
from ..models import Bar, Side, Signal
from ..sessions import parse_hhmm
from .base import Strategy, StrategyContext


class OpeningRangeBreakout(Strategy):
    name = "orb"

    def __init__(self, params, instrument, clock):
        super().__init__(params, instrument, clock)
        self.or_minutes = int(self.p.get("or_minutes", 15))
        if self.or_minutes % self.timeframe != 0:
            raise ValueError("orb.or_minutes must be a multiple of orb.timeframe")
        self.entry_end = parse_hhmm(self.p.get("entry_end", "10:30"))
        self.stop_mode = self.p.get("stop_mode", "mid")
        self.target_r = float(self.p.get("target_r", 2.0))
        self.buffer = int(self.p.get("buffer_ticks", 2)) * instrument.tick_size
        self.min_stop = float(self.p.get("min_stop_points", 8.0))
        self.max_stop = float(self.p.get("max_stop_points", 60.0))
        self.vwap_filter = bool(self.p.get("vwap_filter", True))
        self.ema_filter = bool(self.p.get("ema_trend_filter", True))
        self.max_entries = int(self.p.get("max_entries", 1))
        self.trend_ema = EMA(int(self.p.get("trend_ema", 50)))
        self.vwap = SessionVWAP()
        self._reset_day()
        self.last_skip: str = ""

    def _reset_day(self) -> None:
        self.or_high: Optional[float] = None
        self.or_low: Optional[float] = None
        self.or_complete = False
        self.taken_sides: set[Side] = set()
        self.vwap_started = False
        self.vwap.reset()

    def on_new_day(self, day: date) -> None:
        self._reset_day()

    def evaluate(self, bar: Bar, ctx: StrategyContext) -> Optional[Signal]:
        self.trend_ema.update(bar.close)
        open_dt = self.clock.rth_open_dt(ctx.trading_day)
        bar_open_local = self.clock.local(bar.ts)
        bar_close_local = bar_open_local + timedelta(minutes=self.timeframe)

        if bar_open_local < open_dt:
            return None  # pre-market: only the trend EMA is updated

        # Session VWAP anchored at the cash open.
        self.vwap.update(bar)

        or_end = open_dt + timedelta(minutes=self.or_minutes)
        if bar_open_local < or_end:
            self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
            self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
            if bar_close_local >= or_end:
                self.or_complete = True
            return None

        if not self.or_complete or self.or_high is None or self.or_low is None:
            return None
        if bar_close_local.time() > self.entry_end:
            return None
        if not ctx.flat or ctx.entries_today >= self.max_entries:
            return None

        close = bar.close
        vwap = self.vwap.value
        ema = self.trend_ema.value
        mid = (self.or_high + self.or_low) / 2.0

        side: Optional[Side] = None
        if close > self.or_high + self.buffer and Side.LONG not in self.taken_sides:
            if (not self.vwap_filter or close > vwap) and (
                not self.ema_filter or ema is None or close > ema
            ):
                side = Side.LONG
        elif close < self.or_low - self.buffer and Side.SHORT not in self.taken_sides:
            if (not self.vwap_filter or close < vwap) and (
                not self.ema_filter or ema is None or close < ema
            ):
                side = Side.SHORT
        if side is None:
            return None

        if self.stop_mode == "opposite":
            stop = self.or_low - self.buffer if side is Side.LONG else self.or_high + self.buffer
        else:
            stop = mid
        risk = (close - stop) * side.sign
        if risk < self.min_stop:
            stop = close - side.sign * self.min_stop
            risk = self.min_stop
        if risk > self.max_stop:
            self.last_skip = f"ORB {side.name} skipped: stop {risk:.2f} pts > max {self.max_stop}"
            # Don't keep re-checking the same oversized breakout all morning.
            self.taken_sides.add(side)
            return None

        target = close + side.sign * self.target_r * risk
        self.taken_sides.add(side)
        return Signal(
            side=side,
            entry_ref=close,
            stop=self.r(stop),
            target=self.r(target),
            strategy=self.name,
            reason=(
                f"ORB {side.name}: close {close:.2f} beyond OR "
                f"[{self.or_low:.2f}-{self.or_high:.2f}], VWAP {vwap:.2f}"
            ),
            ts=bar.ts + timedelta(minutes=self.timeframe),
        )
