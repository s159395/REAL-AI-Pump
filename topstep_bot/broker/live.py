"""Live broker: executes the engine's decisions on a TopstepX account.

Safety design:
  * Every entry carries a protective stop. In "native" mode the stop and
    target are server-side OCO bracket orders attached to the entry, so they
    stay in place even if this program crashes or loses its connection.
  * After every fill the broker VERIFIES that a stop order exists on the
    correct side of the entry price. If it can't, it flattens immediately and
    switches to "manual" bracket mode (separate stop + limit orders).
  * When the bot's position goes flat, all remaining working orders on the
    contract are cancelled so no orphaned order can open a new position.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..models import Bar, Bracket, Instrument, OpenPosition, Side, Signal, TradeRecord
from .base import Broker
from .projectx import (
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POS_LONG,
    SIDE_BUY,
    SIDE_SELL,
    ProjectXClient,
    ProjectXError,
    parse_api_time,
)

log = logging.getLogger("topstep_bot.live")


@dataclass
class _Pending:
    order_id: int
    signal: Signal
    size: int
    bracket: Bracket
    submitted: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectXBroker(Broker):
    def __init__(
        self,
        client: ProjectXClient,
        account_id: int,
        instrument: Instrument,
        bracket_mode: str = "native",
        tick_sign: str = "auto",
        alert: Optional[Callable[[str], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not instrument.contract_id:
            raise ValueError("instrument.contract_id is required for live trading")
        self.c = client
        self.acct = int(account_id)
        self.inst = instrument
        self.cid = instrument.contract_id
        self.mode = bracket_mode
        self.tick_sign = tick_sign
        self.alert = alert or (lambda msg: log.critical(msg))
        self.sleep = sleep
        self.position: Optional[OpenPosition] = None
        self.pending: Optional[_Pending] = None
        self.stop_order_id: Optional[int] = None
        self.target_order_id: Optional[int] = None
        self.external_position = False
        self.last_price: Optional[float] = None
        self._exit_reason: Optional[str] = None
        self._window_start: Optional[datetime] = None
        self._tag_seq = 0
        # Trades the broker closed on its own (e.g. failed bracket check);
        # handed to the engine on the next sync so risk accounting stays exact.
        self._queue: list[TradeRecord] = []

    # ---- helpers -----------------------------------------------------------
    @property
    def is_flat(self) -> bool:
        return self.position is None and self.pending is None and not self.external_position

    def _api_position(self) -> Optional[dict]:
        for p in self.c.search_open_positions(self.acct):
            if p.get("contractId") == self.cid and int(p.get("size") or 0) > 0:
                return p
        return None

    def _open_orders(self) -> list[dict]:
        return [o for o in self.c.search_open_orders(self.acct) if o.get("contractId") == self.cid]

    def _cancel_all(self) -> None:
        for o in self._open_orders():
            try:
                self.c.cancel_order(self.acct, int(o["id"]))
                log.info("cancelled working order %s (type %s)", o.get("id"), o.get("type"))
            except ProjectXError as exc:
                log.warning("could not cancel order %s: %s", o.get("id"), exc)
        self.stop_order_id = None
        self.target_order_id = None

    def _tag(self, strategy: str) -> str:
        self._tag_seq += 1
        return f"tsbot-{strategy}-{int(time.time())}-{self._tag_seq}"

    @staticmethod
    def _bracket_payload(side: Side, bracket: Bracket, signed: bool) -> tuple[dict, dict]:
        sl, tp = bracket.stop_ticks, bracket.target_ticks
        if signed:
            # Signed convention: ticks are price offsets (stop below a long = negative).
            sl, tp = (-sl, tp) if side is Side.LONG else (sl, -tp)
        return {"ticks": sl, "type": ORDER_STOP}, {"ticks": tp, "type": ORDER_LIMIT}

    # ---- entries -------------------------------------------------------------
    def submit_entry(self, signal: Signal, size: int, bracket: Bracket, now: datetime) -> bool:
        if not self.is_flat or size <= 0:
            return False
        api_side = SIDE_BUY if signal.side is Side.LONG else SIDE_SELL
        self._window_start = _utcnow() - timedelta(seconds=5)
        try:
            if self.mode == "native":
                order_id = self._place_with_brackets(signal, size, bracket, api_side)
            else:
                order_id = self.c.place_order(
                    self.acct, self.cid, ORDER_MARKET, api_side, size, custom_tag=self._tag(signal.strategy)
                )
        except ProjectXError as exc:
            log.error("entry order rejected: %s", exc)
            return False
        log.info("entry order %s sent: %s %s x%d", order_id, signal.strategy, signal.side.name, size)
        self.pending = _Pending(order_id, signal, size, bracket, _utcnow())
        # Market orders fill almost instantly; wait briefly so brackets get verified now.
        for _ in range(10):
            self.sleep(0.5)
            pos = self._api_position()
            if pos is not None:
                self._on_fill(pos)
                break
        return True

    def _place_with_brackets(self, signal: Signal, size: int, bracket: Bracket, api_side: int) -> int:
        if self.tick_sign == "signed":
            attempts = [True]
        elif self.tick_sign == "unsigned":
            attempts = [False]
        else:
            attempts = [True, False]
        last: Optional[ProjectXError] = None
        for i, signed in enumerate(attempts):
            sl, tp = self._bracket_payload(signal.side, bracket, signed)
            try:
                oid = self.c.place_order(
                    self.acct,
                    self.cid,
                    ORDER_MARKET,
                    api_side,
                    size,
                    custom_tag=self._tag(signal.strategy),
                    stop_loss_bracket=sl,
                    take_profit_bracket=tp,
                )
                if self.tick_sign == "auto":
                    self.tick_sign = "signed" if signed else "unsigned"
                    log.info("bracket tick convention detected: %s", self.tick_sign)
                return oid
            except ProjectXError as exc:
                last = exc
                more = i < len(attempts) - 1
                if more and "tick" in exc.api_message.lower():
                    log.warning("bracket rejected (%s); retrying with the other tick convention", exc.api_message)
                    continue
                raise
        assert last is not None
        raise last

    def _on_fill(self, api_pos: dict) -> None:
        pe = self.pending
        assert pe is not None
        side = Side.LONG if int(api_pos.get("type", POS_LONG)) == POS_LONG else Side.SHORT
        avg = float(api_pos["averagePrice"])
        size = int(api_pos["size"])
        stop, target = pe.bracket.prices(self.inst, side, avg)
        try:
            entry_time = parse_api_time(api_pos["creationTimestamp"])
        except Exception:
            entry_time = _utcnow()
        self.position = OpenPosition(
            side=side,
            size=size,
            entry_price=avg,
            entry_time=entry_time,
            stop_price=stop,
            target_price=target,
            strategy=pe.signal.strategy,
            reason=pe.signal.reason,
            initial_stop=stop,
        )
        self.pending = None
        log.info("FILLED %s x%d @ %.2f (planned stop %.2f, target %.2f)", side.name, size, avg, stop, target)
        if self.mode == "native":
            if not self._verify_native_brackets():
                self.alert(
                    "Bracket verification FAILED - flattening and switching to manual brackets. "
                    "Check that Auto OCO Brackets are enabled in TopstepX settings."
                )
                self.mode = "manual"
                self._queue.extend(self.flatten("bracket verification failed", avg, _utcnow()))
        else:
            self._place_manual_brackets()

    def _verify_native_brackets(self) -> bool:
        pos = self.position
        assert pos is not None
        exit_side = SIDE_SELL if pos.side is Side.LONG else SIDE_BUY
        for _ in range(8):
            orders = self._open_orders()
            stops = [o for o in orders if int(o.get("type", 0)) == ORDER_STOP and int(o.get("side", -1)) == exit_side]
            limits = [o for o in orders if int(o.get("type", 0)) == ORDER_LIMIT and int(o.get("side", -1)) == exit_side]
            if stops:
                s = stops[0]
                sp = float(s.get("stopPrice") or 0)
                ok_side = sp < pos.entry_price if pos.side is Side.LONG else sp > pos.entry_price
                if not ok_side:
                    log.error("stop order %s at %.2f is on the WRONG side of entry %.2f", s.get("id"), sp, pos.entry_price)
                    return False
                self.stop_order_id = int(s["id"])
                pos.stop_price = sp
                pos.initial_stop = sp
                if limits:
                    lp = float(limits[0].get("limitPrice") or 0)
                    ok_t = lp > pos.entry_price if pos.side is Side.LONG else lp < pos.entry_price
                    if ok_t:
                        self.target_order_id = int(limits[0]["id"])
                        pos.target_price = lp
                    else:
                        log.error("target order at %.2f is on the wrong side; cancelling it", lp)
                        try:
                            self.c.cancel_order(self.acct, int(limits[0]["id"]))
                        except ProjectXError:
                            pass
                        return False
                log.info("brackets verified: stop %.2f (order %s), target %s", pos.stop_price, self.stop_order_id,
                         f"{pos.target_price:.2f}" if self.target_order_id else "none")
                return True
            self.sleep(0.5)
        log.error("no stop order found after entry")
        return False

    def _place_manual_brackets(self) -> None:
        pos = self.position
        assert pos is not None
        exit_side = SIDE_SELL if pos.side is Side.LONG else SIDE_BUY
        try:
            self.stop_order_id = self.c.place_order(
                self.acct, self.cid, ORDER_STOP, exit_side, pos.size,
                stop_price=pos.stop_price, custom_tag=self._tag("stop"),
            )
        except ProjectXError as exc:
            self.alert(f"could not place protective stop ({exc}) - flattening")
            self._queue.extend(self.flatten("stop placement failed", pos.entry_price, _utcnow()))
            return
        try:
            self.target_order_id = self.c.place_order(
                self.acct, self.cid, ORDER_LIMIT, exit_side, pos.size,
                limit_price=pos.target_price, custom_tag=self._tag("target"),
            )
        except ProjectXError as exc:
            log.warning("target order failed (%s); stop is in place, target will be managed at market", exc)
        log.info("manual brackets placed: stop %.2f, target %.2f", pos.stop_price, pos.target_price)

    # ---- syncing -------------------------------------------------------------
    def process_bar(self, bar: Bar) -> list[TradeRecord]:
        self.last_price = bar.close
        closed = self._sync(bar.ts + timedelta(minutes=1))
        # Manual mode: if the target order failed to place, emulate it.
        pos = self.position
        if pos is not None and self.mode == "manual" and self.target_order_id is None:
            hit = bar.high >= pos.target_price if pos.side is Side.LONG else bar.low <= pos.target_price
            if hit:
                closed += self.flatten("target", bar.close, bar.ts + timedelta(minutes=1))
        return closed

    def heartbeat(self, now: datetime) -> list[TradeRecord]:
        return self._sync(now)

    def _sync(self, now: datetime) -> list[TradeRecord]:
        queued, self._queue = self._queue, []
        return queued + self._sync_state(now)

    def _sync_state(self, now: datetime) -> list[TradeRecord]:
        api_pos = self._api_position()
        if self.pending is not None:
            if api_pos is not None:
                self._on_fill(api_pos)
                queued, self._queue = self._queue, []
                return queued
            elif _utcnow() - self.pending.submitted > timedelta(seconds=20):
                log.error("entry order %s not filled after 20s; cancelling", self.pending.order_id)
                try:
                    self.c.cancel_order(self.acct, self.pending.order_id)
                except ProjectXError:
                    pass
                self.pending = None
                self._cancel_all()
            return []

        if self.position is not None:
            if api_pos is None:
                rec = self._build_record(now, self._exit_reason)
                self._exit_reason = None
                self.position = None
                self._cancel_all()
                return [rec]
            if int(api_pos.get("size") or 0) != self.position.size:
                log.warning("position size changed outside the bot: %s", api_pos.get("size"))
                self.position.size = int(api_pos.get("size") or 0)
            return []

        # Bot thinks it's flat.
        if api_pos is not None:
            if not self.external_position:
                self.alert(
                    f"An open position on {self.cid} exists that the bot did not open "
                    f"(size {api_pos.get('size')}). The bot will not trade until it is closed."
                )
            self.external_position = True
        else:
            if self.external_position:
                log.info("external position closed; bot resumes")
            self.external_position = False
        return []

    def _build_record(self, now: datetime, reason: Optional[str]) -> TradeRecord:
        pos = self.position
        assert pos is not None
        start = self._window_start or (pos.entry_time - timedelta(minutes=1))
        fills: list[dict] = []
        for attempt in range(3):
            try:
                fills = [
                    t for t in self.c.search_trades(self.acct, start)
                    if t.get("contractId") == self.cid and not t.get("voided")
                ]
            except ProjectXError as exc:
                log.warning("trade search failed: %s", exc)
            if any(t.get("profitAndLoss") is not None for t in fills):
                break
            self.sleep(1.0)
        exits = [t for t in fills if t.get("profitAndLoss") is not None]
        fees = sum(float(t.get("fees") or 0) for t in fills)
        if exits:
            gross = sum(float(t["profitAndLoss"]) for t in exits)
            exit_price = float(exits[-1].get("price") or pos.stop_price)
            last_order = exits[-1].get("orderId")
            try:
                exit_time = parse_api_time(exits[-1]["creationTimestamp"])
            except Exception:
                exit_time = now
        else:
            exit_price = self.last_price or pos.entry_price
            gross = (exit_price - pos.entry_price) * pos.side.sign * pos.size * self.inst.point_value
            last_order = None
            exit_time = now
            log.warning("no exit fills found from the API; P&L estimated from last price")
        if reason is None:
            if last_order is not None and last_order == self.stop_order_id:
                reason = "stop"
            elif last_order is not None and last_order == self.target_order_id:
                reason = "target"
            else:
                reason = "closed"
        risk_dollars = self.inst.to_ticks(pos.entry_price - pos.initial_stop) * self.inst.tick_value * pos.size
        return TradeRecord(
            strategy=pos.strategy,
            side=pos.side,
            size=pos.size,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            gross_pnl=round(gross, 2),
            fees=round(fees, 2),
            exit_reason=reason,
            reason=pos.reason,
            initial_risk_dollars=risk_dollars,
        )

    # ---- exits ----------------------------------------------------------------
    def modify_stop(self, new_stop: float) -> bool:
        if self.position is None or self.stop_order_id is None:
            return False
        price = self.inst.round_to_tick(new_stop)
        try:
            self.c.modify_order(self.acct, self.stop_order_id, stop_price=price)
        except ProjectXError as exc:
            log.warning("stop modify failed: %s", exc)
            return False
        self.position.stop_price = price
        log.info("stop moved to %.2f", price)
        return True

    def flatten(self, reason: str, price: float, now: datetime) -> list[TradeRecord]:
        if self.pending is not None:
            try:
                self.c.cancel_order(self.acct, self.pending.order_id)
            except ProjectXError:
                pass
            self.pending = None
        api_pos = self._api_position()
        if api_pos is None and self.position is None:
            self._cancel_all()
            return []
        self._exit_reason = reason
        log.warning("FLATTEN (%s)", reason)
        try:
            self.c.close_position(self.acct, self.cid)
        except ProjectXError as exc:
            log.error("close position failed: %s", exc)
        self._cancel_all()
        for _ in range(20):
            if self._api_position() is None:
                break
            self.sleep(0.5)
        else:
            self.alert(f"FLATTEN FAILED for {self.cid} - position still open. Close it manually in TopstepX!")
            return []
        if self.position is None:
            self.external_position = False
            return []
        rec = self._build_record(now, reason)
        self._exit_reason = None
        self.position = None
        self._cancel_all()
        return [rec]
