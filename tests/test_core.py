from datetime import date, timedelta

import pytest

from topstep_bot.broker.sim import SimBroker
from topstep_bot.engine import BarAggregator
from topstep_bot.indicators import ATR, EMA, SessionVWAP
from topstep_bot.models import KNOWN_INSTRUMENTS, Bracket, Side, Signal
from topstep_bot.sessions import SessionClock

from .helpers import bar, ct

MNQ = KNOWN_INSTRUMENTS["MNQ"]
D = date(2026, 1, 6)  # a Tuesday


# ---- sessions -----------------------------------------------------------------
def test_trading_day_rolls_at_5pm_ct():
    clk = SessionClock()
    assert clk.trading_day(ct(D, 16, 59)) == D
    assert clk.trading_day(ct(D, 17, 0)) == D + timedelta(days=1)
    # Sunday evening belongs to Monday
    sunday = date(2026, 1, 4)
    assert clk.trading_day(ct(sunday, 18, 0)) == date(2026, 1, 5)


def test_entry_windows_flatten_and_blackouts():
    clk = SessionClock.from_config({
        "entry_windows": ["08:35-11:00"],
        "flatten_time": "15:00",
        "blackouts": [{"time": "09:55-10:05", "label": "data"},
                      {"date": D.isoformat(), "time": "13:00-13:30", "flatten": True, "label": "FOMC"}],
    })
    assert clk.can_enter(ct(D, 8, 40))[0]
    assert not clk.can_enter(ct(D, 8, 30))[0]
    assert not clk.can_enter(ct(D, 11, 0))[0]
    ok, why = clk.can_enter(ct(D, 10, 0))
    assert not ok and "blackout" in why
    assert clk.must_be_flat(ct(D, 15, 0))
    assert clk.must_be_flat(ct(D, 16, 30))
    assert not clk.must_be_flat(ct(D, 14, 59))
    assert not clk.must_be_flat(ct(D, 18, 0))
    assert clk.must_flatten_for_news(ct(D, 13, 10)).label == "FOMC"
    assert clk.must_flatten_for_news(ct(D + timedelta(days=1), 13, 10)) is None


def test_weekend_not_a_trade_day():
    clk = SessionClock()
    assert not clk.can_enter(ct(date(2026, 1, 10), 9, 0))[0]  # Saturday


# ---- indicators -----------------------------------------------------------------
def test_ema_seeds_with_sma_then_smooths():
    e = EMA(3)
    assert e.update(1) is None
    assert e.update(2) is None
    assert e.update(3) == pytest.approx(2.0)
    assert e.update(4) == pytest.approx(3.0)


def test_atr_and_vwap():
    a = ATR(2)
    t0 = ct(D, 9, 0)
    a.update(bar(t0, 10, 12, 9, 11))
    v = a.update(bar(t0, 11, 15, 10, 14))
    assert v == pytest.approx((3 + 5) / 2)
    vw = SessionVWAP()
    vw.update(bar(t0, 0, 12, 9, 9, v=100))  # typical 10
    vw.update(bar(t0, 0, 21, 18, 21, v=300))  # typical 20
    assert vw.value == pytest.approx(17.5)


# ---- aggregator ---------------------------------------------------------------
def test_five_minute_aggregation():
    clk = SessionClock()
    agg = BarAggregator(5, clk)
    start = ct(D, 8, 30)
    out = []
    for i in range(10):
        out += agg.add(bar(start + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10))
    assert len(out) == 2
    first = out[0]
    assert first.ts == start
    assert (first.open, first.high, first.low, first.close, first.volume) == (100, 105, 99, 104.5, 50)


def test_aggregator_emits_on_gap():
    clk = SessionClock()
    agg = BarAggregator(5, clk)
    start = ct(D, 8, 30)
    assert agg.add(bar(start, 1, 2, 0, 1)) == []
    out = agg.add(bar(start + timedelta(minutes=7), 1, 2, 0, 1))
    assert len(out) == 1 and out[0].ts == start


# ---- simulated fills ------------------------------------------------------------
def _sig(side=Side.LONG, entry=20000.0):
    return Signal(side, entry, entry - side.sign * 10, entry + side.sign * 20, "t", "", ct(D, 9, 0))


def test_sim_entry_next_open_with_slippage_and_stop():
    b = SimBroker(MNQ, commission_per_contract=0.74, slippage_ticks=1)
    b.submit_entry(_sig(), 2, Bracket(40, 80), ct(D, 9, 0))
    t0 = ct(D, 9, 0)
    assert b.process_bar(bar(t0, 20000, 20002, 19998, 20001)) == []
    assert b.position.entry_price == 20000.25
    assert b.position.stop_price == 19990.25
    assert b.position.target_price == 20020.25
    closed = b.process_bar(bar(t0 + timedelta(minutes=1), 20000, 20001, 19990, 19991))
    assert len(closed) == 1
    tr = closed[0]
    assert tr.exit_reason == "stop"
    assert tr.exit_price == 19990.0  # stop minus 1 tick slippage
    assert tr.gross_pnl == pytest.approx((19990.0 - 20000.25) * 2 * 2)
    assert tr.fees == pytest.approx(1.48)


def test_sim_stop_assumed_first_when_both_touched():
    b = SimBroker(MNQ, slippage_ticks=0)
    b.submit_entry(_sig(), 1, Bracket(40, 80), ct(D, 9, 0))
    t0 = ct(D, 9, 0)
    b.process_bar(bar(t0, 20000, 20001, 19999, 20000))
    closed = b.process_bar(bar(t0 + timedelta(minutes=1), 20000, 20030, 19980, 20010))
    assert closed[0].exit_reason == "stop"


def test_sim_target_needs_trade_through():
    b = SimBroker(MNQ, slippage_ticks=0, limit_through_ticks=1)
    b.submit_entry(_sig(Side.SHORT), 1, Bracket(40, 80), ct(D, 9, 0))
    t0 = ct(D, 9, 0)
    b.process_bar(bar(t0, 20000, 20001, 19999, 20000))
    assert b.position.target_price == 19980.0
    assert b.process_bar(bar(t0 + timedelta(minutes=1), 19990, 19991, 19980, 19985)) == []  # touch only
    closed = b.process_bar(bar(t0 + timedelta(minutes=2), 19985, 19986, 19979.75, 19982))
    assert closed[0].exit_reason == "target" and closed[0].exit_price == 19980.0
    assert closed[0].gross_pnl == pytest.approx(40.0)


def test_sim_gap_through_stop_fills_at_open():
    b = SimBroker(MNQ, slippage_ticks=0)
    b.submit_entry(_sig(), 1, Bracket(40, 80), ct(D, 9, 0))
    t0 = ct(D, 9, 0)
    b.process_bar(bar(t0, 20000, 20001, 19999, 20000))
    closed = b.process_bar(bar(t0 + timedelta(minutes=1), 19980, 19985, 19975, 19982))
    assert closed[0].exit_price == 19980.0


def test_bracket_prices():
    br = Bracket(stop_ticks=40, target_ticks=80)
    assert br.prices(MNQ, Side.LONG, 100.0) == (90.0, 120.0)
    assert br.prices(MNQ, Side.SHORT, 100.0) == (110.0, 80.0)
