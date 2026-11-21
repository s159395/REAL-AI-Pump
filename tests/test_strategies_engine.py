from datetime import date, timedelta

import pytest

from topstep_bot.backtest import build_engine, run_combine_simulation, run_continuous, combine_summary
from topstep_bot.config import DEFAULTS, deep_merge
from topstep_bot.data import generate_synthetic
from topstep_bot.models import KNOWN_INSTRUMENTS, Bar, Side
from topstep_bot.sessions import SessionClock
from topstep_bot.strategies import StrategyContext, VWAPPullback

from .helpers import bar, ct

MNQ = KNOWN_INSTRUMENTS["MNQ"]
D = date(2026, 1, 6)


def cfg_with(**sections):
    return deep_merge(DEFAULTS, sections)


def orb_morning(day=D, breakout_close=20030.0):
    """Opening range 20000-20020 from 8:30-8:44, then a breakout to `breakout_close`."""
    bars = []
    t = ct(day, 8, 30)
    for i in range(15):
        h = 20020.0 if i == 3 else 20012.0
        lo = 20000.0 if i == 7 else 20008.0
        bars.append(bar(t + timedelta(minutes=i), 20010, h, lo, 20010))
    closes = [20014, 20018, 20022, 20026, breakout_close]
    prev = 20010.0
    for j, c in enumerate(closes):
        bars.append(bar(t + timedelta(minutes=15 + j), prev, max(prev, c) + 0.5, min(prev, c) - 0.5, c))
        prev = c
    return bars


def walk(start_ts, start_px, end_px, minutes):
    out = []
    step = (end_px - start_px) / minutes
    px = start_px
    for i in range(minutes):
        nxt = px + step
        out.append(bar(start_ts + timedelta(minutes=i), px, max(px, nxt) + 0.5, min(px, nxt) - 0.5, nxt))
        px = nxt
    return out


def test_orb_long_entry_and_target():
    cfg = cfg_with(strategies={"enabled": ["orb"]}, management={"breakeven_at_r": 0, "time_stop_minutes": 0})
    eng = build_engine(cfg, MNQ, enforce_combine=True)
    bars = orb_morning()
    for b in bars:
        eng.on_bar(b)
    entries = [e for e in eng.events if e.kind == "entry"]
    assert len(entries) == 1
    assert "LONG" in entries[0].message and "stop 20010.00" in entries[0].message
    assert "target 20070.00" in entries[0].message
    # Price runs to the target.
    for b in walk(bars[-1].ts + timedelta(minutes=1), 20030, 20080, 30):
        eng.on_bar(b)
    assert len(eng.trades) == 1
    tr = eng.trades[0]
    assert tr.side is Side.LONG and tr.exit_reason == "target"
    assert tr.net_pnl > 0
    assert tr.r_multiple == pytest.approx(2.0, abs=0.15)


def test_orb_skips_when_range_too_wide():
    cfg = cfg_with(strategies={"enabled": ["orb"], "orb": {"max_stop_points": 5, "min_stop_points": 1}})
    eng = build_engine(cfg, MNQ)
    for b in orb_morning():
        eng.on_bar(b)
    assert not [e for e in eng.events if e.kind == "entry"]


def test_no_entries_outside_windows_or_on_weekends():
    cfg = cfg_with(strategies={"enabled": ["orb"]}, session={"entry_windows": ["13:00-14:00"]})
    eng = build_engine(cfg, MNQ)
    for b in orb_morning():
        eng.on_bar(b)
    assert not [e for e in eng.events if e.kind == "entry"]
    assert any("outside entry window" in e.message for e in eng.events if e.kind == "skip")


def test_flatten_at_session_end():
    cfg = cfg_with(
        strategies={"enabled": ["orb"], "orb": {"target_r": 20}},
        management={"breakeven_at_r": 0, "time_stop_minutes": 0},
    )
    eng = build_engine(cfg, MNQ)
    bars = orb_morning()
    for b in bars:
        eng.on_bar(b)
    t = bars[-1].ts + timedelta(minutes=1)
    n = int((ct(D, 15, 5) - t).total_seconds() // 60)
    for i in range(n):
        eng.on_bar(bar(t + timedelta(minutes=i), 20031, 20032, 20030, 20031))
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "flatten time"
    assert eng.trades[0].exit_time <= ct(D, 15, 1)
    assert eng.broker.position is None


def test_breakeven_stop_move():
    cfg = cfg_with(strategies={"enabled": ["orb"], "orb": {"target_r": 5}},
                   management={"breakeven_at_r": 1.0, "breakeven_offset_ticks": 2, "time_stop_minutes": 0})
    eng = build_engine(cfg, MNQ)
    bars = orb_morning()
    for b in bars:
        eng.on_bar(b)
    t = bars[-1].ts + timedelta(minutes=1)
    up = walk(t, 20030, 20055, 10)  # +25 pts > 1R (20 pts)
    for b in up:
        eng.on_bar(b)
    assert eng.broker.position.moved_to_breakeven
    entry = eng.broker.position.entry_price
    assert eng.broker.position.stop_price == pytest.approx(entry + 0.5)
    down = walk(up[-1].ts + timedelta(minutes=1), 20055, 20020, 10)
    for b in down:
        eng.on_bar(b)
    assert eng.trades[0].exit_reason == "stop"
    assert eng.trades[0].gross_pnl >= 0


def test_size_limited_by_daily_loss_room():
    # $50 of daily-loss room and ~$41.74 risk per contract (20 pt stop) -> exactly 1 contract.
    cfg = cfg_with(strategies={"enabled": ["orb"]}, risk={"max_daily_loss": 50, "risk_per_trade": 200})
    eng = build_engine(cfg, MNQ)
    for b in orb_morning():
        eng.on_bar(b)
    entry = [e for e in eng.events if e.kind == "entry"][0]
    assert " x1 " in entry.message


def _vwap_strategy():
    clk = SessionClock()
    params = dict(DEFAULTS["strategies"]["vwap_pullback"])
    params.update({"slope_lookback": 2, "atr_period": 5, "fast_ema": 5, "slow_ema": 10})
    return VWAPPullback(params, MNQ, clk), clk


def test_vwap_pullback_long_signal():
    strat, clk = _vwap_strategy()
    t = ct(D, 6, 0)
    px = 20000.0
    sig = None
    # Steady uptrend in 5-minute bars from 6:00 through 9:20.
    for i in range(41):
        ts = t + timedelta(minutes=5 * i)
        b = Bar(ts, px, px + 4, px - 1, px + 3, 500)
        ctx = StrategyContext(ts + timedelta(minutes=5), clk.trading_day(ts), True, 0)
        sig = strat.on_bar(b, ctx) or sig
        px += 3
    assert sig is None or sig.side is Side.LONG
    fast = strat.fast.value
    ts = t + timedelta(minutes=5 * 41)
    pull = Bar(ts, fast + 2, fast + 6, fast - 0.5, fast + 5, 500)  # dips to fast EMA, closes strong
    sig = strat.on_bar(pull, StrategyContext(ts + timedelta(minutes=5), D, True, 0))
    assert sig is not None and sig.side is Side.LONG
    assert sig.stop < pull.low
    assert sig.target > sig.entry_ref
    assert sig.reward_points == pytest.approx(1.8 * sig.risk_points, abs=0.3)


def test_vwap_pullback_no_signal_against_trend():
    strat, clk = _vwap_strategy()
    t = ct(D, 6, 0)
    px = 20000.0
    for i in range(41):
        ts = t + timedelta(minutes=5 * i)
        b = Bar(ts, px, px + 1, px - 4, px - 3, 500)  # downtrend
        strat.on_bar(b, StrategyContext(ts + timedelta(minutes=5), clk.trading_day(ts), True, 0))
        px -= 3
    fast = strat.fast.value
    ts = t + timedelta(minutes=5 * 41)
    bullish = Bar(ts, fast - 2, fast + 6, fast - 3, fast + 5, 500)
    sig = strat.on_bar(bullish, StrategyContext(ts + timedelta(minutes=5), D, True, 0))
    assert sig is None or sig.side is Side.SHORT


def test_full_backtest_and_combine_sim_run_on_synthetic_data():
    bars = generate_synthetic(date(2026, 1, 5), 25, seed=11)
    res = run_continuous(DEFAULTS, bars, MNQ)
    assert res.stats["trades"] > 0
    # Daily loss guard: no day may lose much more than the configured max (allowing slippage).
    assert res.stats["worst_day"] >= -DEFAULTS["risk"]["max_daily_loss"] * 1.5
    # No position is ever held past the flatten time.
    clk = SessionClock()
    for t in res.trades:
        assert clk.local(t.exit_time).time() <= clk.flatten_time.replace(minute=1)
    results = run_combine_simulation(DEFAULTS, bars, MNQ, step=5, max_days=10)
    s = combine_summary(results)
    assert s["attempts"] == len(results)
    for r in results:
        assert r.outcome in ("passed", "failed", "timeout")


def test_engine_never_exceeds_topstep_contract_limit():
    cfg = cfg_with(risk={"risk_per_trade": 100000, "max_contracts": 1000, "max_daily_loss": 100000,
                         "mll_buffer": 0, "scale_down_near_target": False},
                   combine={"max_loss_limit": 100000})
    bars = generate_synthetic(date(2026, 1, 5), 10, seed=5)
    res = run_continuous(cfg, bars, MNQ)
    assert res.trades and max(t.size for t in res.trades) <= 50
