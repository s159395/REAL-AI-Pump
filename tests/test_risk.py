from datetime import date, datetime, timezone

import pytest

from topstep_bot.config import DEFAULTS
from topstep_bot.models import KNOWN_INSTRUMENTS, Side, Signal
from topstep_bot.risk import FAILED, PASSED, CombineTracker, RiskManager

RULES = DEFAULTS["combine"]
MNQ = KNOWN_INSTRUMENTS["MNQ"]
NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def tracker(**over):
    r = dict(RULES)
    r.update(over)
    return CombineTracker(r)


def test_initial_mll_is_start_minus_max_loss():
    t = tracker()
    assert t.mll == 48000
    assert t.balance == 50000


def test_eod_trailing_mll_and_lock_at_start():
    t = tracker()
    t.start_day(date(2026, 1, 5))
    t.record_trade(800)
    # Intraday profit does NOT trail an EOD MLL
    assert t.mll == 48000
    t.start_day(date(2026, 1, 6))  # end of day 1
    assert t.mll == 48800
    t.record_trade(1500)
    t.start_day(date(2026, 1, 7))  # EOD balance 52,300 -> MLL would be 50,300 but locks at 50,000
    assert t.mll == 50000
    t.record_trade(-500)
    t.start_day(date(2026, 1, 8))
    assert t.mll == 50000  # never trails down


def test_intraday_trailing_mode():
    t = tracker(mll_trailing="intraday")
    t.start_day(date(2026, 1, 5))
    t.mark(unrealized_worst=0, unrealized_best=600)
    assert t.mll == 48600


def test_breach_fails_combine_including_unrealized():
    t = tracker()
    t.start_day(date(2026, 1, 5))
    t.record_trade(-1500)
    assert t.status == "active"
    t.mark(unrealized_worst=-600)
    assert t.status == FAILED


def test_pass_requires_consistency():
    t = tracker()
    t.start_day(date(2026, 1, 5))
    t.record_trade(2000)  # one huge day
    t.start_day(date(2026, 1, 6))
    t.record_trade(1100)  # total 3,100 but best day 2,000 >= 50% of 3,100
    assert t.total_profit == 3100
    assert t.status == "active"
    assert t.required_total_profit() > 4000
    t.start_day(date(2026, 1, 7))
    t.record_trade(1000)  # total 4,100, best day 2,000 < 2,050
    assert t.status == PASSED


def test_pass_with_even_days():
    t = tracker()
    for i, pnl in enumerate([900, 800, 700, 650]):
        t.start_day(date(2026, 1, 5 + i))
        t.record_trade(pnl)
    assert t.total_profit == 3050
    assert t.status == PASSED


def test_dll_hit():
    t = tracker(daily_loss_limit=1000)
    t.start_day(date(2026, 1, 5))
    t.record_trade(-700)
    assert not t.dll_hit()
    assert t.dll_hit(unrealized=-300)


def sig(stop_pts=20.0, target_pts=40.0, side=Side.LONG, entry=20000.0):
    return Signal(side, entry, entry - side.sign * stop_pts, entry + side.sign * target_pts, "test", "", NOW)


def rm(risk_over=None, rules_over=None, commission=0.74):
    rc = dict(DEFAULTS["risk"])
    rc.update(risk_over or {})
    t = tracker(**(rules_over or {}))
    t.start_day(date(2026, 1, 5))
    return RiskManager(rc, t, MNQ, commission)


def test_position_size_from_risk_budget():
    r = rm({"risk_per_trade": 200, "scale_down_near_target": False})
    # 20 points on MNQ = 80 ticks * $0.50 = $40 + 2 ticks slippage ($1) + $0.74 = $41.74
    size, risk, why = r.size_for(sig(20))
    assert size == 4
    assert risk == pytest.approx(4 * 41.74)


def test_size_capped_by_topstep_limit_and_bot_cap():
    r = rm({"risk_per_trade": 5000, "max_contracts": 100, "max_daily_loss": 10000, "mll_buffer": 0,
            "scale_down_near_target": False})
    size, _, _ = r.size_for(sig(2))
    assert size == 50  # 5 minis = 50 micros on a 50K
    r2 = rm({"risk_per_trade": 5000, "max_contracts": 7, "max_daily_loss": 10000, "scale_down_near_target": False})
    assert r2.size_for(sig(2))[0] == 7


def test_size_never_lets_stop_breach_mll():
    r = rm({"risk_per_trade": 400, "mll_buffer": 150, "max_daily_loss": 5000, "scale_down_near_target": False})
    r.t.record_trade(-1700)  # balance 48,300; room above MLL+buffer = 150
    size, risk, _ = r.size_for(sig(20))
    assert risk <= 150
    assert size == 3


def test_size_respects_daily_loss_room():
    r = rm({"risk_per_trade": 400, "max_daily_loss": 450, "scale_down_near_target": False})
    r.t.record_trade(-300)
    size, risk, _ = r.size_for(sig(20))
    assert risk <= 150


def test_zero_size_when_stop_too_wide():
    r = rm({"risk_per_trade": 100, "scale_down_near_target": False})
    size, _, why = r.size_for(sig(60))  # $120+/contract
    assert size == 0 and "exceeds budget" in why


def test_risk_scales_down_near_target():
    r = rm({"risk_per_trade": 300})
    r.t.record_trade(800)
    r.t.start_day(date(2026, 1, 6))
    r.t.record_trade(800)
    r.t.start_day(date(2026, 1, 7))
    r.t.record_trade(1300)  # profit 2,900, need 100 more
    assert r.risk_budget(2.0) == pytest.approx(300 * 0.25)


def test_gates_daily_limits_and_consecutive_losses():
    from topstep_bot.models import TradeRecord

    r = rm({"max_consecutive_losses": 2, "max_daily_loss": 1000})

    def tr(pnl):
        return TradeRecord("t", Side.LONG, 1, NOW, 1, NOW, 1, pnl, 0, "stop")

    assert r.can_open()[0]
    r.on_trade_closed(tr(-100))
    assert r.can_open()[0]
    r.on_trade_closed(tr(-100))
    ok, why = r.can_open()
    assert not ok and "consecutive" in why
    r.start_day(date(2026, 1, 6))
    assert r.can_open()[0]
    r.on_trade_closed(tr(950))
    ok, why = r.can_open()
    assert not ok and "profit cap" in why


def test_gate_stops_after_pass_and_fail():
    r = rm()
    r.t.s.status = PASSED
    assert not r.can_open()[0]
    r.t.s.status = FAILED
    assert not r.can_open()[0]


def test_must_flatten_near_mll():
    r = rm({"max_daily_loss": 5000})
    r.t.record_trade(-1800)  # balance 48,200, MLL 48,000
    assert "Maximum Loss Limit" in r.must_flatten(-160)  # equity 48,040
    assert r.must_flatten(-100) is None  # equity 48,100


def test_kill_switch_when_day_far_past_limit():
    r = rm({"max_daily_loss": 400})
    r.t.record_trade(-500)
    assert r.must_flatten(-50) is None
    assert "kill-switch" in r.must_flatten(-150)  # day at -650 > 1.5 x 400


def test_state_roundtrip(tmp_path):
    t = tracker()
    t.start_day(date(2026, 1, 5))
    t.record_trade(123.45)
    p = tmp_path / "s.json"
    t.save(p)
    st = CombineTracker.load_state(p)
    t2 = CombineTracker(RULES, st)
    assert t2.balance == pytest.approx(50123.45)
    assert t2.day_pnl() == pytest.approx(123.45)
