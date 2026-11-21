"""End-to-end test of the live/paper Runner against a fake TopstepX API with a
simulated clock: warm-up, bar polling, order placement and the 3 PM flatten."""

from datetime import date, timedelta

import pytest

import topstep_bot.live as live_mod
from topstep_bot.config import DEFAULTS, deep_merge
from topstep_bot.live import Runner

from .helpers import bar, ct
from .test_live_broker import FakeAPI
from .test_strategies_engine import orb_morning, walk

D = date(2026, 1, 6)


class FakeClient(FakeAPI):
    def __init__(self, bars, balance=50000.0):
        super().__init__("signed", price=20030.0)
        self.all_bars = bars
        self.balance = balance

    def login(self):
        pass

    def search_accounts(self, only_active=True):
        return [{"id": 1, "name": "50KTC-TEST", "balance": self.balance, "canTrade": True}]

    def resolve_contract(self, symbol, live=False):
        return {"id": "CON.F.US.MNQ.H26", "tickSize": 0.25, "tickValue": 0.5, "description": "MNQ test"}

    def contract_by_id(self, cid):
        return self.resolve_contract("MNQ")

    def retrieve_bars(self, cid, start, end, include_partial=False, **kw):
        return [b for b in self.all_bars if start <= b.ts < end]

    def retrieve_bars_range(self, cid, start, end, chunk_days=2):
        return self.retrieve_bars(cid, start, end)


def make_runner(tmp_path, mode, bars, monkeypatch, start_at):
    cfg = deep_merge(DEFAULTS, {
        "strategies": {"enabled": ["orb"]},
        "management": {"breakeven_at_r": 0, "time_stop_minutes": 0},
        "live": {"account_id": 1, "state_file": str(tmp_path / "state.json"),
                 "journal_file": str(tmp_path / "trades.csv"), "warmup_days": 1},
    })
    client = FakeClient(bars)
    clock = {"now": start_at}
    monkeypatch.setattr(live_mod, "utcnow", lambda: clock["now"])
    r = Runner(cfg, client, mode=mode)
    r.setup()
    r.warmup()
    r._seed_today()
    return r, client, clock


def session_bars():
    pre = walk(ct(D, 7, 0), 20000, 20010, 90)  # 7:00-8:29 pre-market
    morning = orb_morning()  # 8:30-8:49, breakout close 20030 at 8:49
    rest = [bar(morning[-1].ts + timedelta(minutes=i + 1), 20031, 20032, 20030, 20031)
            for i in range(int((ct(D, 15, 10) - morning[-1].ts).total_seconds() // 60))]
    return pre + morning + rest


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_runner_trades_and_flattens(tmp_path, monkeypatch, mode):
    bars = session_bars()
    r, client, clock = make_runner(tmp_path, mode, bars, monkeypatch, start_at=ct(D, 8, 0))
    assert r.last_ts is not None and r.last_ts < ct(D, 8, 0)

    t = ct(D, 8, 1)
    while t <= ct(D, 15, 3):
        clock["now"] = t + timedelta(seconds=2)
        client.price = 20031.0
        r.step()
        t += timedelta(minutes=1)

    eng = r.engine
    assert [e for e in eng.events if e.kind == "entry"], "expected an ORB entry"
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "flatten time"
    assert eng.broker.position is None
    if mode == "live":
        placed = [c for c in client.calls if c[0] == "place"]
        assert placed and placed[0][1] == 2  # market order with brackets
        assert placed[0][4]["ticks"] < 0  # signed stop ticks for a long
        assert client.positions == [] and client.orders == {}
    r._save()
    assert (tmp_path / ("state.json" if mode == "live" else "state_paper.json")).exists()
    journal = tmp_path / ("trades.csv" if mode == "live" else "trades_paper.csv")
    assert len(journal.read_text().strip().splitlines()) == 2  # header + 1 trade


def test_runner_refuses_to_start_with_open_position(tmp_path, monkeypatch):
    cfg = deep_merge(DEFAULTS, {"live": {"account_id": 1, "state_file": str(tmp_path / "s.json"),
                                         "journal_file": str(tmp_path / "t.csv")}})
    client = FakeClient([])
    client.positions = [{"contractId": "CON.F.US.MNQ.H26", "size": 1, "type": 1, "averagePrice": 1.0}]
    r = Runner(cfg, client, mode="live")
    with pytest.raises(RuntimeError, match="already an open"):
        r.setup()


def test_runner_lists_accounts_when_id_missing(tmp_path):
    cfg = deep_merge(DEFAULTS, {"live": {"state_file": str(tmp_path / "s.json"),
                                         "journal_file": str(tmp_path / "t.csv")}})
    r = Runner(cfg, FakeClient([]), mode="paper")
    with pytest.raises(RuntimeError, match="50KTC-TEST"):
        r.setup()
