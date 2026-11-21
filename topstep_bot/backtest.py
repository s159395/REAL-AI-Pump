"""Backtesting and Topstep Combine simulation.

Two views of a strategy:
  * Continuous backtest: trade the whole data set with the daily risk rules but
    without the Combine pass/fail stop. Gives win rate, profit factor, drawdown.
  * Combine simulation: start a fresh 50K Combine on many different days and
    run each one until it passes, fails, or runs out of days. Gives the pass
    rate and typical days-to-pass, which is what actually matters.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .broker.sim import SimBroker
from .engine import DaySummary, TradingEngine
from .models import Bar, Instrument, KNOWN_INSTRUMENTS, TradeRecord
from .risk import ACTIVE, FAILED, PASSED, CombineTracker, RiskManager
from .sessions import SessionClock
from .strategies import build_strategies


def instrument_from_config(cfg: dict) -> Instrument:
    ic = cfg.get("instrument", {})
    sym = str(ic.get("symbol", "MNQ")).upper()
    base = KNOWN_INSTRUMENTS.get(sym)
    tick_size = ic.get("tick_size") or (base.tick_size if base else None)
    tick_value = ic.get("tick_value") or (base.tick_value if base else None)
    if tick_size is None or tick_value is None:
        raise ValueError(f"unknown symbol {sym}: set instrument.tick_size and instrument.tick_value")
    mini_eq = base.mini_equivalent if base else (0.1 if sym.startswith("M") else 1.0)
    return Instrument(sym, float(tick_size), float(tick_value), ic.get("contract_id"), mini_eq)


def build_engine(
    cfg: dict,
    instrument: Instrument,
    enforce_combine: bool = True,
    broker=None,
    tracker: Optional[CombineTracker] = None,
) -> TradingEngine:
    clock = SessionClock.from_config(cfg.get("session", {}))
    bt = cfg.get("backtest", {})
    commission = float(bt.get("commission_per_contract", 0.74))
    tracker = tracker or CombineTracker(cfg.get("combine", {}), enforce=enforce_combine)
    risk = RiskManager(cfg.get("risk", {}), tracker, instrument, commission)
    strategies = build_strategies(cfg, instrument, clock)
    if broker is None:
        broker = SimBroker(instrument, commission, int(bt.get("slippage_ticks", 1)))
    return TradingEngine(cfg, instrument, clock, strategies, risk, broker)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def trade_stats(trades: list[TradeRecord], days: list[DaySummary]) -> dict:
    n = len(trades)
    wins = [t.net_pnl for t in trades if t.net_pnl > 0]
    losses = [t.net_pnl for t in trades if t.net_pnl <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    traded_days = [d for d in days if d.trades > 0]
    day_pnls = [d.pnl for d in traded_days]
    by_strategy: dict[str, dict] = {}
    for t in trades:
        s = by_strategy.setdefault(t.strategy, {"trades": 0, "wins": 0, "net": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if t.net_pnl > 0 else 0
        s["net"] += t.net_pnl
    exits: dict[str, int] = {}
    for t in trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    return {
        "trades": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "net_pnl": sum(t.net_pnl for t in trades),
        "fees": sum(t.fees for t in trades),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win else 0.0,
        "expectancy": (sum(t.net_pnl for t in trades) / n) if n else 0.0,
        "avg_r": statistics.mean([t.r_multiple for t in trades]) if n else 0.0,
        "max_drawdown": max_dd,
        "days": len(days),
        "traded_days": len(traded_days),
        "green_days": sum(1 for p in day_pnls if p > 0),
        "best_day": max(day_pnls) if day_pnls else 0.0,
        "worst_day": min(day_pnls) if day_pnls else 0.0,
        "by_strategy": by_strategy,
        "exits": exits,
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    days: list[DaySummary]
    stats: dict
    engine: TradingEngine


def run_continuous(cfg: dict, bars: list[Bar], instrument: Instrument) -> BacktestResult:
    eng = build_engine(cfg, instrument, enforce_combine=False)
    for b in bars:
        eng.on_bar(b)
    eng.finish()
    return BacktestResult(eng.trades, eng.days, trade_stats(eng.trades, eng.days), eng)


@dataclass
class AttemptResult:
    start_day: date
    outcome: str  # passed / failed / timeout / data_end
    days: int
    profit: float
    trades: int
    reason: str = ""


def _day_index(bars: list[Bar], clock: SessionClock) -> list[tuple[date, int]]:
    """(trading_day, first bar index) for every trading day in the data."""
    out: list[tuple[date, int]] = []
    last = None
    for i, b in enumerate(bars):
        d = clock.trading_day(b.ts)
        if d != last:
            out.append((d, i))
            last = d
    return out


def run_combine_attempt(
    cfg: dict,
    bars: list[Bar],
    instrument: Instrument,
    start_idx: int,
    start_day: date,
    max_days: int,
) -> AttemptResult:
    eng = build_engine(cfg, instrument, enforce_combine=True)
    tracker = eng.risk.t
    days_seen = 0
    current = None
    for b in bars[start_idx:]:
        d = eng.clock.trading_day(b.ts)
        if d != current:
            current = d
            if d >= start_day:
                if tracker.status != ACTIVE:
                    break
                days_seen += 1
                if days_seen > max_days:
                    days_seen -= 1
                    break
        eng.trading_enabled = d >= start_day
        eng.on_bar(b)
        if tracker.status == FAILED:
            break
    eng.finish()
    status = tracker.status
    if status == PASSED:
        outcome = "passed"
    elif status == FAILED:
        outcome = "failed"
    elif days_seen >= max_days:
        outcome = "timeout"
    else:
        outcome = "data_end"
    return AttemptResult(
        start_day=start_day,
        outcome=outcome,
        days=days_seen,
        profit=tracker.total_profit,
        trades=len(eng.trades),
        reason=tracker.s.status_reason,
    )


def run_combine_simulation(
    cfg: dict,
    bars: list[Bar],
    instrument: Instrument,
    step: int = 1,
    max_days: Optional[int] = None,
    warmup_days: int = 3,
    progress=None,
) -> list[AttemptResult]:
    clock = SessionClock.from_config(cfg.get("session", {}))
    max_days = int(max_days or cfg.get("backtest", {}).get("combine_max_days", 30))
    index = [(d, i) for d, i in _day_index(bars, clock) if clock.is_trade_day(d)]
    results: list[AttemptResult] = []
    starts = list(range(warmup_days, len(index), max(1, step)))
    for k, pos in enumerate(starts):
        start_day = index[pos][0]
        warm_idx = index[pos - warmup_days][1]
        res = run_combine_attempt(cfg, bars, instrument, warm_idx, start_day, max_days)
        if res.outcome != "data_end":
            results.append(res)
        if progress:
            progress(k + 1, len(starts), res)
    return results


def combine_summary(results: list[AttemptResult]) -> dict:
    n = len(results)
    passed = [r for r in results if r.outcome == "passed"]
    failed = [r for r in results if r.outcome == "failed"]
    timeout = [r for r in results if r.outcome == "timeout"]
    return {
        "attempts": n,
        "passed": len(passed),
        "failed": len(failed),
        "timeout": len(timeout),
        "pass_rate": len(passed) / n if n else 0.0,
        "median_days_to_pass": statistics.median([r.days for r in passed]) if passed else None,
        "median_days_to_fail": statistics.median([r.days for r in failed]) if failed else None,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_stats(stats: dict) -> str:
    lines = [
        "=== Continuous backtest (daily risk rules on, Combine stop off) ===",
        f"Trades           : {stats['trades']}",
        f"Win rate         : {stats['win_rate']*100:.1f}%",
        f"Net P&L          : ${stats['net_pnl']:,.2f}  (fees ${stats['fees']:,.2f})",
        f"Avg win / loss   : ${stats['avg_win']:,.2f} / ${stats['avg_loss']:,.2f}",
        f"Profit factor    : {stats['profit_factor']:.2f}",
        f"Expectancy       : ${stats['expectancy']:,.2f}/trade  ({stats['avg_r']:+.2f}R)",
        f"Max drawdown     : ${stats['max_drawdown']:,.2f}  (closed-trade)",
        f"Days traded      : {stats['traded_days']} of {stats['days']}  (green {stats['green_days']})",
        f"Best / worst day : ${stats['best_day']:,.2f} / ${stats['worst_day']:,.2f}",
        "By strategy      : "
        + ", ".join(
            f"{k}: {v['trades']} trades, {100*v['wins']/max(v['trades'],1):.0f}% win, ${v['net']:,.0f}"
            for k, v in stats["by_strategy"].items()
        ),
        "Exit reasons     : " + ", ".join(f"{k}={v}" for k, v in sorted(stats["exits"].items())),
    ]
    return "\n".join(lines)


def format_combine(summary: dict, max_days: int) -> str:
    if not summary["attempts"]:
        return "=== Combine simulation ===\nNot enough data for any attempt."
    lines = [
        f"=== 50K Combine simulation ({summary['attempts']} attempts, max {max_days} trading days each) ===",
        f"Passed           : {summary['passed']}  ({summary['pass_rate']*100:.1f}%)",
        f"Failed (MLL hit) : {summary['failed']}",
        f"Ran out of days  : {summary['timeout']}",
        f"Median days to pass : {summary['median_days_to_pass']}",
        f"Median days to fail : {summary['median_days_to_fail']}",
    ]
    return "\n".join(lines)


def write_trades_csv(trades: list[TradeRecord], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["trading_day", "strategy", "side", "size", "entry_time", "entry_price", "exit_time",
             "exit_price", "gross_pnl", "fees", "net_pnl", "r_multiple", "exit_reason", "reason"]
        )
        for t in trades:
            w.writerow(
                [t.trading_day, t.strategy, t.side.name, t.size, t.entry_time.isoformat(), t.entry_price,
                 t.exit_time.isoformat(), t.exit_price, f"{t.gross_pnl:.2f}", f"{t.fees:.2f}",
                 f"{t.net_pnl:.2f}", f"{t.r_multiple:.2f}", t.exit_reason, t.reason]
            )
