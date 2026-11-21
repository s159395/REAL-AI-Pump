"""Live broker tests against an in-memory fake of the TopstepX API."""

from datetime import datetime, timedelta, timezone

import pytest

from topstep_bot.broker.live import ProjectXBroker
from topstep_bot.broker.projectx import (
    ORDER_LIMIT, ORDER_MARKET, ORDER_STOP, POS_LONG, POS_SHORT, SIDE_BUY, SIDE_SELL, ProjectXError,
)
from topstep_bot.models import Bracket, Instrument, Side, Signal

MNQ = Instrument("MNQ", 0.25, 0.50, contract_id="CON.F.US.MNQ.Z26", mini_equivalent=0.1)
NOW = datetime(2026, 1, 6, 15, 0, tzinfo=timezone.utc)


class FakeAPI:
    """Minimal exchange: market orders fill at `price`, brackets become OCO orders.

    convention: "signed"   -> stop ticks must be negative for longs (positive for shorts)
                "unsigned" -> ticks must be positive
                "broken"   -> accepts anything and puts the stop on the WRONG side
    """

    def __init__(self, convention="signed", price=20000.0):
        self.conv = convention
        self.price = price
        self.positions = []
        self.orders = {}
        self.trades = []
        self.next_id = 100
        self.calls = []

    def _id(self):
        self.next_id += 1
        return self.next_id

    # --- API surface used by the broker ---
    def search_open_positions(self, acct):
        return [dict(p) for p in self.positions]

    def search_open_orders(self, acct):
        return [dict(o) for o in self.orders.values()]

    def place_order(self, acct, cid, order_type, side, size, limit_price=None, stop_price=None,
                    trail_price=None, custom_tag=None, stop_loss_bracket=None, take_profit_bracket=None):
        self.calls.append(("place", order_type, side, size, stop_loss_bracket, take_profit_bracket,
                           limit_price, stop_price))
        long = side == SIDE_BUY
        if order_type == ORDER_MARKET and stop_loss_bracket is not None:
            sl, tp = stop_loss_bracket["ticks"], take_profit_bracket["ticks"]
            if self.conv == "signed" and ((long and sl >= 0) or (not long and sl <= 0)):
                raise ProjectXError(f"Invalid stop loss ticks ({sl}). Ticks should be less than zero when longing.")
            if self.conv == "unsigned" and (sl < 0 or tp < 0):
                raise ProjectXError(f"Invalid ticks ({sl}). Ticks must be positive.")
        oid = self._id()
        if order_type == ORDER_MARKET:
            self.positions = [{
                "id": 1, "accountId": acct, "contractId": cid, "creationTimestamp": NOW.isoformat(),
                "type": POS_LONG if long else POS_SHORT, "size": size, "averagePrice": self.price,
            }]
            self.trades.append({"id": self._id(), "contractId": cid, "price": self.price, "profitAndLoss": None,
                                "fees": 0.37 * size, "side": side, "size": size, "voided": False, "orderId": oid,
                                "creationTimestamp": NOW.isoformat()})
            if stop_loss_bracket is not None:
                exit_side = SIDE_SELL if long else SIDE_BUY
                n_sl, n_tp = abs(stop_loss_bracket["ticks"]), abs(take_profit_bracket["ticks"])
                sgn = 1 if long else -1
                if self.conv == "broken":
                    sgn = -sgn
                sid, tid = self._id(), self._id()
                self.orders[sid] = {"id": sid, "contractId": cid, "type": ORDER_STOP, "side": exit_side,
                                    "size": size, "stopPrice": self.price - sgn * n_sl * 0.25}
                self.orders[tid] = {"id": tid, "contractId": cid, "type": ORDER_LIMIT, "side": exit_side,
                                    "size": size, "limitPrice": self.price + sgn * n_tp * 0.25}
        else:
            self.orders[oid] = {"id": oid, "contractId": cid, "type": order_type, "side": side, "size": size,
                                "stopPrice": stop_price, "limitPrice": limit_price}
        return oid

    def cancel_order(self, acct, oid):
        self.calls.append(("cancel", oid))
        self.orders.pop(oid, None)

    def modify_order(self, acct, oid, size=None, limit_price=None, stop_price=None, trail_price=None):
        self.calls.append(("modify", oid, stop_price))
        if stop_price is not None:
            self.orders[oid]["stopPrice"] = stop_price

    def _exit(self, price, order_id):
        p = self.positions[0]
        sign = 1 if p["type"] == POS_LONG else -1
        pnl = (price - p["averagePrice"]) * sign * p["size"] * 2.0
        self.trades.append({"id": self._id(), "contractId": p["contractId"], "price": price, "profitAndLoss": pnl,
                            "fees": 0.37 * p["size"], "side": 1, "size": p["size"], "voided": False,
                            "orderId": order_id, "creationTimestamp": NOW.isoformat()})
        self.positions = []

    def close_position(self, acct, cid):
        self.calls.append(("close", cid))
        self._exit(self.price, 999)

    def search_trades(self, acct, start, end=None):
        return [dict(t) for t in self.trades]

    # --- test helpers ---
    def trigger(self, order_type):
        oid = next(o["id"] for o in self.orders.values() if o["type"] == order_type)
        px = self.orders[oid]["stopPrice"] if order_type == ORDER_STOP else self.orders[oid]["limitPrice"]
        self.orders.pop(oid)
        self._exit(px, oid)  # the other leg stays until someone cancels it (worst case)
        return oid


def make(conv="signed", mode="native", sign="auto", price=20000.0):
    api = FakeAPI(conv, price)
    alerts = []
    b = ProjectXBroker(api, 1, MNQ, bracket_mode=mode, tick_sign=sign, alert=alerts.append, sleep=lambda s: None)
    return api, b, alerts


def sig(side=Side.LONG):
    e = 20000.0
    return Signal(side, e, e - side.sign * 10, e + side.sign * 20, "orb", "test", NOW)


def test_native_bracket_signed_convention_and_verification():
    api, b, alerts = make("signed")
    assert b.submit_entry(sig(), 3, Bracket(40, 80), NOW)
    place = api.calls[0]
    assert place[4] == {"ticks": -40, "type": ORDER_STOP}
    assert place[5] == {"ticks": 80, "type": ORDER_LIMIT}
    assert b.position is not None and b.position.size == 3
    assert b.position.stop_price == 19990.0 and b.position.target_price == 20020.0
    assert b.stop_order_id is not None and b.target_order_id is not None
    assert b.tick_sign == "signed"
    assert not alerts


def test_auto_falls_back_to_unsigned_ticks():
    api, b, alerts = make("unsigned")
    assert b.submit_entry(sig(Side.SHORT), 1, Bracket(40, 80), NOW)
    assert b.tick_sign == "unsigned"
    assert b.position.side is Side.SHORT
    assert b.position.stop_price == 20010.0


def test_wrong_side_stop_triggers_flatten_and_manual_mode():
    api, b, alerts = make("broken")
    b.submit_entry(sig(), 2, Bracket(40, 80), NOW)
    assert alerts and "verification FAILED" in alerts[0]
    assert b.mode == "manual"
    assert b.position is None and api.positions == []
    assert api.orders == {}  # bad bracket orders cancelled
    closed = b.heartbeat(NOW)  # queued trade record is handed to the engine
    assert len(closed) == 1 and closed[0].exit_reason == "bracket verification failed"


def test_stop_fill_detected_and_orphan_target_cancelled():
    api, b, _ = make("signed")
    b.submit_entry(sig(), 2, Bracket(40, 80), NOW)
    api.trigger(ORDER_STOP)
    closed = b.heartbeat(NOW + timedelta(minutes=5))
    assert len(closed) == 1
    tr = closed[0]
    assert tr.exit_reason == "stop"
    assert tr.exit_price == 19990.0
    assert tr.gross_pnl == pytest.approx(-40.0)  # 10 pts x 2 contracts x $2
    assert tr.fees == pytest.approx(0.37 * 2 * 2)
    assert api.orders == {}  # leftover target cancelled
    assert b.is_flat


def test_target_fill_reason():
    api, b, _ = make("signed")
    b.submit_entry(sig(), 1, Bracket(40, 80), NOW)
    api.trigger(ORDER_LIMIT)
    tr = b.heartbeat(NOW)[0]
    assert tr.exit_reason == "target" and tr.gross_pnl == pytest.approx(40.0)


def test_manual_mode_places_separate_stop_and_target():
    api, b, _ = make("signed", mode="manual")
    b.submit_entry(sig(), 1, Bracket(40, 80), NOW)
    types = sorted(o["type"] for o in api.orders.values())
    assert types == [ORDER_LIMIT, ORDER_STOP]
    stop = next(o for o in api.orders.values() if o["type"] == ORDER_STOP)
    assert stop["stopPrice"] == 19990.0 and stop["side"] == SIDE_SELL


def test_modify_stop_and_flatten():
    api, b, _ = make("signed")
    b.submit_entry(sig(), 1, Bracket(40, 80), NOW)
    assert b.modify_stop(20000.5)
    assert api.orders[b.stop_order_id]["stopPrice"] == 20000.5
    api.price = 20005.0
    closed = b.flatten("flatten time", 20005.0, NOW)
    assert closed[0].exit_reason == "flatten time"
    assert closed[0].gross_pnl == pytest.approx(10.0)
    assert api.positions == [] and api.orders == {}


def test_external_position_blocks_trading():
    api, b, alerts = make("signed")
    api.positions = [{"contractId": MNQ.contract_id, "size": 1, "type": POS_LONG, "averagePrice": 1.0}]
    b.heartbeat(NOW)
    assert not b.is_flat and alerts
    assert not b.submit_entry(sig(), 1, Bracket(40, 80), NOW)
    api.positions = []
    b.heartbeat(NOW)
    assert b.is_flat


def test_rejected_entry_returns_false():
    api, b, _ = make("signed", sign="unsigned")  # forced wrong convention -> rejected, no retry
    assert not b.submit_entry(sig(), 1, Bracket(40, 80), NOW)
    assert b.is_flat and api.positions == []
