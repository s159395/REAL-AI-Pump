"""The trading engine: one loop, shared by backtest, paper and live trading.

Feed it completed 1-minute bars in time order. On each bar it:
  1. rolls the trading day (end-of-day MLL trailing, daily counters reset),
  2. lets the broker process fills (stops/targets),
  3. marks the open position to market and checks the Combine limits,
  4. forces exits (3:00 PM CT flatten, news, risk kill-switch, time stop),
  5. moves the stop to breakeven once the trade is +1R,
  6. builds each strategy's higher-timeframe bars and asks for signals,
  7. sizes and submits a new bracket order if every risk gate allows it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .broker.base import Broker
from .models import Bar, Bracket, Instrument, Side, Signal, TradeRecord
from .risk import RiskManager
from .sessions import SessionClock
from .strategies.base import Strategy, StrategyContext

log = logging.getLogger("topstep_bot.engine")

ONE_MIN = timedelta(minutes=1)
MIN_BRACKET_TICKS = 8  # TopstepX reportedly rejects very tight tick brackets


class BarAggregator:
    """Builds N-minute bars from 1-minute bars, aligned to the local clock."""

    def __init__(self, minutes: int, clock: SessionClock):
        self.n = int(minutes)
        self.clock = clock
        self._bucket: Optional[datetime] = None
        self._o = self._h = self._l = self._c = 0.0
        self._v = 0.0

    def _bucket_start(self, ts: datetime) -> datetime:
        lt = self.clock.local(ts)
        mins = lt.hour * 60 + lt.minute
        start = mins - (mins % self.n)
        return lt.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)

    def _emit(self) -> Bar:
        assert self._bucket is not None
        bar = Bar(self._bucket, self._o, self._h, self._l, self._c, self._v)
        self._bucket = None
        return bar

    def add(self, bar: Bar) -> list[Bar]:
        if self.n <= 1:
            return [bar]
        out: list[Bar] = []
        b = self._bucket_start(bar.ts)
        if self._bucket is not None and b != self._bucket:
            out.append(self._emit())  # previous bucket had a gap at its end
        if self._bucket is None:
            self._bucket = b
            self._o, self._h, self._l, self._c, self._v = bar.open, bar.high, bar.low, bar.close, bar.volume
        else:
            self._h = max(self._h, bar.high)
            self._l = min(self._l, bar.low)
            self._c = bar.close
            self._v += bar.volume
        if self.clock.local(bar.ts) + ONE_MIN >= self._bucket + timedelta(minutes=self.n):
            out.append(self._emit())
        return out


@dataclass
class EngineEvent:
    ts: datetime
    kind: str
    message: str


@dataclass
class DaySummary:
    day: date
    pnl: float
    trades: int
    end_balance: float
    mll: float


class TradingEngine:
    def __init__(
        self,
        cfg: dict,
        instrument: Instrument,
        clock: SessionClock,
        strategies: list[Strategy],
        risk: RiskManager,
        broker: Broker,
        on_trade: Optional[Callable[[TradeRecord], None]] = None,
        on_event: Optional[Callable[[EngineEvent], None]] = None,
    ):
        self.cfg = cfg
        self.inst = instrument
        self.clock = clock
        self.strategies = strategies
        self.risk = risk
        self.broker = broker
        self.on_trade = on_trade
        self.on_event = on_event
        mg = cfg.get("management", {})
        self.be_r = float(mg.get("breakeven_at_r", 0) or 0)
        self.be_offset = int(mg.get("breakeven_offset_ticks", 0)) * instrument.tick_size
        self.time_stop = int(mg.get("time_stop_minutes", 0) or 0)
        self.aggs = {s.name: BarAggregator(s.timeframe, clock) for s in strategies}
        self.entries_today: dict[str, int] = defaultdict(int)
        self.current_day: Optional[date] = None
        self.trading_enabled = True
        self.trades: list[TradeRecord] = []
        self.days: list[DaySummary] = []
        self.events: list[EngineEvent] = []
        self._day_trades = 0
        self._entry_strategy: Optional[str] = None
        self.last_bar: Optional[Bar] = None

    # ---- events --------------------------------------------------------
    def _event(self, ts: datetime, kind: str, msg: str) -> None:
        ev = EngineEvent(ts, kind, msg)
        self.events.append(ev)
        if len(self.events) > 5000:
            del self.events[:1000]
        if self.on_event:
            self.on_event(ev)

    def _record(self, trade: TradeRecord) -> None:
        trade.trading_day = self.current_day
        self.trades.append(trade)
        self._day_trades += 1
        prev = self.risk.t.status
        self.risk.on_trade_closed(trade)
        if self.risk.t.status != prev:
            self._event(trade.exit_time, "combine", self.risk.t.s.status_reason)
        self._event(
            trade.exit_time,
            "exit",
            f"{trade.strategy} {trade.side.name} x{trade.size} closed by {trade.exit_reason}: "
            f"{trade.entry_price:.2f} -> {trade.exit_price:.2f}, net {trade.net_pnl:+.2f} "
            f"({trade.r_multiple:+.2f}R) | balance {self.risk.t.balance:.2f}",
        )
        if self.on_trade:
            self.on_trade(trade)

    # ---- day handling ----------------------------------------------------
    def _close_day(self) -> None:
        if self.current_day is None:
            return
        t = self.risk.t
        self.days.append(
            DaySummary(self.current_day, t.day_pnl(), self._day_trades, t.balance, t.mll)
        )

    def _new_day(self, day: date, ts: datetime) -> None:
        self._close_day()
        prev_status = self.risk.t.status
        self.risk.start_day(day)
        self.current_day = day
        self.entries_today = defaultdict(int)
        self._day_trades = 0
        if self.risk.t.status != prev_status:
            self._event(ts, "combine", self.risk.t.s.status_reason)

    def finish(self) -> None:
        """Call after the last bar so the final day is closed out."""
        if self.broker.position is not None and self.last_bar is not None:
            end = self.last_bar.ts + ONE_MIN
            for tr in self.broker.flatten("end of data", self.last_bar.close, end):
                self._record(tr)
        self._close_day()
        self.risk.t.end_day()
        self.current_day = None

    # ---- warmup (live start-up) ---------------------------------------------
    def warmup(self, bars: list[Bar]) -> None:
        """Feed history to the strategies only (no orders, no risk changes) so
        indicators and today's opening range are ready when live trading starts."""
        for bar in bars:
            for strat in self.strategies:
                for htf in self.aggs[strat.name].add(bar):
                    ctx = StrategyContext(
                        now=htf.ts + timedelta(minutes=strat.timeframe),
                        trading_day=self.clock.trading_day(htf.ts),
                        flat=False,  # strategies never signal when not flat
                        entries_today=0,
                    )
                    strat.on_bar(htf, ctx)
            self.last_bar = bar

    # ---- main step -------------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        now = bar.ts + ONE_MIN
        day = self.clock.trading_day(bar.ts)
        if day != self.current_day:
            if self.broker.position is not None and self.last_bar is not None:
                for tr in self.broker.flatten("day rollover", self.last_bar.close, bar.ts):
                    self._record(tr)
            self._new_day(day, bar.ts)
        self.last_bar = bar

        for tr in self.broker.process_bar(bar):
            self._record(tr)

        self._manage_position(bar, now)
        self._run_strategies(bar, now)

    def heartbeat(self, now: datetime, last_price: Optional[float]) -> None:
        """Between-bar safety checks for live trading (flatten time, kill-switch)."""
        for tr in self.broker.heartbeat(now):
            self._record(tr)
        pos = self.broker.position
        if pos is None or last_price is None:
            return
        reason = self._forced_exit_reason(now, pos.unrealized(last_price, self.inst), pos)
        if reason:
            self._event(now, "flatten", reason)
            for tr in self.broker.flatten(reason, last_price, now):
                self._record(tr)

    def _forced_exit_reason(self, now: datetime, unrealized: float, pos) -> Optional[str]:
        if self.clock.must_be_flat(now):
            return "flatten time"
        nb = self.clock.must_flatten_for_news(now)
        if nb is not None:
            return f"news flatten {nb.label}".strip()
        r = self.risk.must_flatten(unrealized)
        if r:
            return r
        if self.time_stop and now - pos.entry_time >= timedelta(minutes=self.time_stop):
            return "time stop"
        return None

    def _manage_position(self, bar: Bar, now: datetime) -> None:
        pos = self.broker.position
        if pos is None:
            return
        worst_px = bar.low if pos.side is Side.LONG else bar.high
        best_px = bar.high if pos.side is Side.LONG else bar.low
        prev = self.risk.t.status
        self.risk.t.mark(pos.unrealized(worst_px, self.inst), pos.unrealized(best_px, self.inst))
        if self.risk.t.status != prev:
            self._event(now, "combine", self.risk.t.s.status_reason)

        reason = self._forced_exit_reason(now, pos.unrealized(bar.close, self.inst), pos)
        if reason:
            self._event(now, "flatten", reason)
            for tr in self.broker.flatten(reason, bar.close, now):
                self._record(tr)
            return

        # Breakeven stop once the trade has moved be_r x initial risk in our favour.
        if self.be_r > 0 and not pos.moved_to_breakeven and pos.initial_risk_points > 0:
            fav = (bar.high - pos.entry_price) if pos.side is Side.LONG else (pos.entry_price - bar.low)
            if fav >= self.be_r * pos.initial_risk_points:
                new_stop = self.inst.round_to_tick(pos.entry_price + pos.side.sign * self.be_offset)
                # Only valid if the new stop is still on the correct side of price.
                valid = (
                    new_stop < bar.close - self.inst.tick_size
                    if pos.side is Side.LONG
                    else new_stop > bar.close + self.inst.tick_size
                )
                improves = (
                    new_stop > pos.stop_price if pos.side is Side.LONG else new_stop < pos.stop_price
                )
                if valid and improves and self.broker.modify_stop(new_stop):
                    pos.moved_to_breakeven = True
                    self._event(now, "manage", f"stop moved to breakeven {new_stop:.2f}")

    def _run_strategies(self, bar: Bar, now: datetime) -> None:
        signals: list[Signal] = []
        for strat in self.strategies:
            for htf in self.aggs[strat.name].add(bar):
                ctx = StrategyContext(
                    now=now,
                    trading_day=self.clock.trading_day(htf.ts),
                    flat=self.broker.is_flat,
                    entries_today=self.entries_today[strat.name],
                )
                sig = strat.on_bar(htf, ctx)
                if sig is not None:
                    signals.append(sig)
        if signals:
            # Strategies are listed in priority order in the config.
            self._try_enter(signals[0], now)

    def _try_enter(self, sig: Signal, now: datetime) -> None:
        if not self.trading_enabled:
            return
        if not self.broker.is_flat:
            return
        ok, why = self.clock.can_enter(now)
        if ok:
            ok, why = self.risk.can_open()
        if not ok:
            self._event(now, "skip", f"{sig.strategy} {sig.side.name} skipped: {why}")
            return
        size, risk_dollars, why = self.risk.size_for(sig)
        if size <= 0:
            self._event(now, "skip", f"{sig.strategy} {sig.side.name} skipped: {why}")
            return
        bracket = Bracket(
            stop_ticks=self.inst.to_ticks(sig.entry_ref - sig.stop),
            target_ticks=self.inst.to_ticks(sig.target - sig.entry_ref),
        )
        if bracket.stop_ticks < MIN_BRACKET_TICKS or bracket.target_ticks < MIN_BRACKET_TICKS:
            self._event(now, "skip", f"{sig.strategy} skipped: bracket too tight {bracket}")
            return
        if self.broker.submit_entry(sig, size, bracket, now):
            self.entries_today[sig.strategy] += 1
            self.risk.on_entry()
            self._event(
                now,
                "entry",
                f"{sig.strategy} {sig.side.name} x{size} @~{sig.entry_ref:.2f} "
                f"stop {sig.stop:.2f} ({bracket.stop_ticks}t) target {sig.target:.2f} "
                f"({bracket.target_ticks}t) risk ~${risk_dollars:.0f} | {sig.reason}",
            )
        else:
            self._event(now, "error", f"broker rejected entry for {sig.strategy}")
