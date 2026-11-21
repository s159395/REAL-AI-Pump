"""VWAP / EMA trend pullback.

Trend-continuation setup: when price is trending (fast EMA above slow EMA,
slow EMA rising, price above session VWAP), wait for a pullback into the fast
EMA and enter when a bar rejects that level and closes back in the trend
direction. Buying pullbacks in an established trend gives a tight, logical
stop (below the pullback's swing low) and a good reward-to-risk.

Long rules (short is the mirror image):
  * fast EMA > slow EMA, slow EMA higher than `slope_lookback` bars ago
  * close > VWAP, and not stretched more than `max_vwap_distance_atr` ATRs above it
  * bar low came within `touch_atr` x ATR of the fast EMA (the pullback)
  * bar closes above the fast EMA, is a bullish bar, and closes in its upper half
  * stop = lowest low of the last `swing_lookback` bars - buffer, at least
    max(min_stop_points, min_stop_atr x ATR); skip if wider than max_stop_points
  * target = target_r x risk
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ..indicators import ATR, EMA, RollingWindow, SessionVWAP
from ..models import Bar, Side, Signal
from ..sessions import parse_hhmm
from .base import Strategy, StrategyContext


class VWAPPullback(Strategy):
    name = "vwap_pullback"

    def __init__(self, params, instrument, clock):
        super().__init__(params, instrument, clock)
        self.fast = EMA(int(self.p.get("fast_ema", 9)))
        self.slow = EMA(int(self.p.get("slow_ema", 21)))
        self.atr = ATR(int(self.p.get("atr_period", 14)))
        self.slope_lookback = int(self.p.get("slope_lookback", 3))
        self.slow_hist = RollingWindow(self.slope_lookback + 1)
        self.swing_lookback = int(self.p.get("swing_lookback", 3))
        self.bars = RollingWindow(max(self.swing_lookback, 2))
        self.touch_atr = float(self.p.get("touch_atr", 0.25))
        self.buffer = int(self.p.get("stop_buffer_ticks", 4)) * instrument.tick_size
        self.min_stop = float(self.p.get("min_stop_points", 8.0))
        self.max_stop = float(self.p.get("max_stop_points", 50.0))
        self.min_stop_atr = float(self.p.get("min_stop_atr", 0.8))
        self.target_r = float(self.p.get("target_r", 1.8))
        self.max_entries = int(self.p.get("max_entries", 2))
        self.cooldown_bars = int(self.p.get("cooldown_bars", 3))
        self.start = parse_hhmm(self.p.get("start", "09:00"))
        self.end = parse_hhmm(self.p.get("end", "14:00"))
        self.max_vwap_dist = float(self.p.get("max_vwap_distance_atr", 3.0))
        self.vwap = SessionVWAP()
        self.cooldown = 0
        self.last_skip = ""

    def on_new_day(self, day: date) -> None:
        self.vwap.reset()
        self.cooldown = 0

    def evaluate(self, bar: Bar, ctx: StrategyContext) -> Optional[Signal]:
        fast = self.fast.update(bar.close)
        slow = self.slow.update(bar.close)
        atr = self.atr.update(bar)
        if slow is not None:
            self.slow_hist.append(slow)
        self.bars.append(bar)
        if self.cooldown > 0:
            self.cooldown -= 1

        open_dt = self.clock.rth_open_dt(ctx.trading_day)
        bar_open_local = self.clock.local(bar.ts)
        if bar_open_local < open_dt:
            return None
        vwap = self.vwap.update(bar)

        close_t = (bar_open_local + timedelta(minutes=self.timeframe)).time()
        if not (self.start <= close_t <= self.end):
            return None
        if fast is None or slow is None or atr is None or not self.slow_hist.full:
            return None
        if not ctx.flat or ctx.entries_today >= self.max_entries or self.cooldown > 0:
            return None

        slow_then = self.slow_hist[0]
        rng = bar.high - bar.low
        side: Optional[Side] = None

        if (
            fast > slow
            and slow > slow_then
            and bar.close > vwap
            and fast > vwap
            and (bar.close - vwap) <= self.max_vwap_dist * atr
            and bar.low <= fast + self.touch_atr * atr
            and bar.close > fast
            and bar.close > bar.open
            and rng > 0
            and (bar.close - bar.low) >= 0.5 * rng
        ):
            side = Side.LONG
        elif (
            fast < slow
            and slow < slow_then
            and bar.close < vwap
            and fast < vwap
            and (vwap - bar.close) <= self.max_vwap_dist * atr
            and bar.high >= fast - self.touch_atr * atr
            and bar.close < fast
            and bar.close < bar.open
            and rng > 0
            and (bar.high - bar.close) >= 0.5 * rng
        ):
            side = Side.SHORT
        if side is None:
            return None

        recent = self.bars.values()[-self.swing_lookback:]
        if side is Side.LONG:
            stop = min(b.low for b in recent) - self.buffer
        else:
            stop = max(b.high for b in recent) + self.buffer
        risk = (bar.close - stop) * side.sign
        floor = max(self.min_stop, self.min_stop_atr * atr)
        if risk < floor:
            risk = floor
            stop = bar.close - side.sign * risk
        if risk > self.max_stop:
            self.last_skip = f"VWAP pullback {side.name} skipped: stop {risk:.2f} > max {self.max_stop}"
            return None

        target = bar.close + side.sign * self.target_r * risk
        self.cooldown = self.cooldown_bars
        return Signal(
            side=side,
            entry_ref=bar.close,
            stop=self.r(stop),
            target=self.r(target),
            strategy=self.name,
            reason=(
                f"Pullback {side.name}: EMA{self.fast.period} {fast:.2f} / "
                f"EMA{self.slow.period} {slow:.2f}, VWAP {vwap:.2f}, ATR {atr:.2f}"
            ),
            ts=bar.ts + timedelta(minutes=self.timeframe),
        )
