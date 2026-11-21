"""Live and paper trading runner for TopstepX.

  paper: real-time TopstepX market data, simulated fills (no orders sent).
  live:  real-time data and REAL orders on your TopstepX Combine account.

The loop polls completed 1-minute bars every few seconds and feeds them to the
same TradingEngine used by the backtester.
"""

from __future__ import annotations

import csv
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from .backtest import build_engine, instrument_from_config
from .broker.live import ProjectXBroker
from .broker.projectx import ProjectXClient, ProjectXError
from .broker.sim import SimBroker
from .engine import EngineEvent, TradingEngine
from .models import Instrument, TradeRecord
from .risk import CombineState, CombineTracker
from .sessions import SessionClock

log = logging.getLogger("topstep_bot.runner")
ONE_MIN = timedelta(minutes=1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TradeJournal:
    FIELDS = ["trading_day", "strategy", "side", "size", "entry_time", "entry_price", "exit_time",
              "exit_price", "gross_pnl", "fees", "net_pnl", "r_multiple", "exit_reason", "reason"]

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(self.FIELDS)

    def write(self, t: TradeRecord) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([
                t.trading_day, t.strategy, t.side.name, t.size, t.entry_time.isoformat(), t.entry_price,
                t.exit_time.isoformat(), t.exit_price, f"{t.gross_pnl:.2f}", f"{t.fees:.2f}",
                f"{t.net_pnl:.2f}", f"{t.r_multiple:.2f}", t.exit_reason, t.reason,
            ])


class Runner:
    def __init__(self, cfg: dict, client: ProjectXClient, mode: str = "paper", flatten_on_exit: bool = True):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        self.cfg = cfg
        self.client = client
        self.mode = mode
        self.flatten_on_exit = flatten_on_exit
        self.lc = cfg.get("live", {})
        self.poll = float(self.lc.get("poll_seconds", 5))
        self.clock = SessionClock.from_config(cfg.get("session", {}))
        self._stop = False
        self.engine: Optional[TradingEngine] = None
        self.account: Optional[dict] = None
        self.inst: Optional[Instrument] = None
        self.last_ts: Optional[datetime] = None
        self.last_price: Optional[float] = None
        state = self.lc.get("state_file", "state/combine_state.json")
        self.state_file = state if mode == "live" else state.replace(".json", "_paper.json")
        journal = self.lc.get("journal_file", "logs/trades.csv")
        self.journal = TradeJournal(journal if mode == "live" else journal.replace(".csv", "_paper.csv"))

    # ---- setup -----------------------------------------------------------------
    def _pick_account(self) -> dict:
        accounts = self.client.search_accounts(only_active=True)
        if not accounts:
            raise RuntimeError("no active TopstepX accounts found for this API key")
        want_id = self.lc.get("account_id")
        want_name = self.lc.get("account_name")
        for a in accounts:
            if want_id is not None and int(a.get("id")) == int(want_id):
                return a
            if want_name and str(a.get("name")) == str(want_name):
                return a
        listing = "\n".join(
            f"  id={a.get('id')} name={a.get('name')} balance={a.get('balance')} canTrade={a.get('canTrade')}"
            for a in accounts
        )
        raise RuntimeError(
            "Set live.account_id (or live.account_name) in your config to one of:\n" + listing
        )

    def _resolve_instrument(self) -> Instrument:
        base = instrument_from_config(self.cfg)
        cid = self.cfg.get("instrument", {}).get("contract_id")
        contract = self.client.contract_by_id(cid) if cid else self.client.resolve_contract(base.symbol)
        if not contract:
            raise RuntimeError(f"contract {cid} not found")
        inst = Instrument(
            symbol=base.symbol,
            tick_size=float(contract.get("tickSize") or base.tick_size),
            tick_value=float(contract.get("tickValue") or base.tick_value),
            contract_id=str(contract["id"]),
            mini_equivalent=base.mini_equivalent,
        )
        log.info("Trading %s (%s): tick %.4f = $%.2f", inst.contract_id, contract.get("description", ""),
                 inst.tick_size, inst.tick_value)
        return inst

    def _load_tracker(self, balance: float) -> CombineTracker:
        rules = self.cfg.get("combine", {})
        state = CombineTracker.load_state(self.state_file)
        if state is None:
            start = float(rules.get("starting_balance", 50000))
            max_loss = float(rules.get("max_loss_limit", 2000))
            peak = max(start, balance)
            mll = rules.get("resume_mll")
            if mll is None:
                mll = min(start, peak - max_loss) if rules.get("mll_stops_at_start", True) else peak - max_loss
            state = CombineState(
                balance=balance, mll=float(mll), peak_eod=peak, peak_intraday=peak, day_start_balance=balance,
            )
            if rules.get("resume_best_day"):
                state.daily_pnl["prior-best"] = float(rules["resume_best_day"])
            if abs(balance - start) > 1:
                log.warning(
                    "No saved state and balance %.2f != starting balance %.2f. Assuming MLL = %.2f. "
                    "Check the MLL on your TopstepX dashboard and set combine.resume_mll if it differs.",
                    balance, start, mll,
                )
        else:
            log.info("Loaded combine state: balance %.2f, MLL %.2f, status %s", state.balance, state.mll, state.status)
        return CombineTracker(rules, state, enforce=True)

    def setup(self) -> None:
        self.client.login()
        self.account = self._pick_account()
        if self.mode == "live" and not self.account.get("canTrade", True):
            raise RuntimeError(f"account {self.account.get('name')} cannot trade right now (canTrade=false)")
        self.inst = self._resolve_instrument()
        acct_id = int(self.account["id"])
        balance = float(self.account.get("balance") or self.cfg["combine"]["starting_balance"])
        tracker = self._load_tracker(balance if self.mode == "live" else float(self.cfg["combine"]["starting_balance"]))
        if self.mode == "live" and abs(tracker.balance - balance) >= 1.0:
            # Balance moved while the bot was off: adopt it without booking it as today's P&L.
            log.warning("Saved balance %.2f differs from TopstepX balance %.2f; using TopstepX.",
                        tracker.balance, balance)
            tracker.s.balance = balance
            tracker._check_breach(balance)

        bt = self.cfg.get("backtest", {})
        if self.mode == "live":
            broker = ProjectXBroker(
                self.client, acct_id, self.inst,
                bracket_mode=self.lc.get("bracket_mode", "native"),
                tick_sign=self.lc.get("bracket_tick_sign", "auto"),
            )
            existing = broker._api_position()
            if existing is not None:
                raise RuntimeError(
                    f"There is already an open {self.inst.contract_id} position on this account. "
                    "Close it in TopstepX before starting the bot."
                )
            broker._cancel_all()
        else:
            broker = SimBroker(self.inst, float(bt.get("commission_per_contract", 0.74)), int(bt.get("slippage_ticks", 1)))

        self.engine = build_engine(self.cfg, self.inst, enforce_combine=True, broker=broker, tracker=tracker)
        self.engine.on_trade = self._on_trade
        self.engine.on_event = self._on_event
        log.info("Account %s (id %s) | mode=%s | balance %.2f | MLL %.2f | profit %.2f / %.2f",
                 self.account.get("name"), acct_id, self.mode.upper(), tracker.balance, tracker.mll,
                 tracker.total_profit, tracker.target)

    def _seed_today(self) -> None:
        """After a restart, restore today's P&L and trade count from the API."""
        eng = self.engine
        assert eng is not None and self.account is not None
        today = self.clock.trading_day(utcnow())
        eng.risk.start_day(today)
        eng.current_day = today
        if self.mode != "live":
            return
        day_start = datetime.combine(today - timedelta(days=1), self.clock.day_start, tzinfo=self.clock.tz)
        try:
            fills = self.client.search_trades(int(self.account["id"]), day_start)
        except ProjectXError as exc:
            log.warning("could not load today's trades: %s", exc)
            return
        closes = [t for t in fills if not t.get("voided") and t.get("profitAndLoss") is not None]
        day_net = sum(float(t["profitAndLoss"]) for t in closes) - sum(float(t.get("fees") or 0) for t in fills if not t.get("voided"))
        key = today.isoformat()
        t = eng.risk.t
        recorded = float(t.s.daily_pnl.get(key, 0.0))
        if closes and abs(day_net - recorded) > 0.5:
            t.s.daily_pnl[key] = day_net
            log.info("Today's realized P&L from API: %.2f (%d closing fills)", day_net, len(closes))
        eng.risk.trades_today = len({t.get("orderId") for t in closes})

    def warmup(self) -> None:
        assert self.engine is not None and self.inst is not None
        days = int(self.lc.get("warmup_days", 4))
        end = utcnow()
        start = end - timedelta(days=days + 2)
        bars = self.client.retrieve_bars_range(self.inst.contract_id, start, end, chunk_days=2)
        bars = [b for b in bars if b.ts + ONE_MIN <= end]
        self.engine.warmup(bars)
        if bars:
            self.last_ts = bars[-1].ts
            self.last_price = bars[-1].close
        log.info("Warm-up complete with %d bars (last %s)", len(bars), self.last_ts)

    # ---- callbacks ----------------------------------------------------------------
    def _on_trade(self, t: TradeRecord) -> None:
        self.journal.write(t)
        self._save()

    def _on_event(self, ev: EngineEvent) -> None:
        level = logging.INFO
        if ev.kind in ("error", "flatten", "combine"):
            level = logging.WARNING
        elif ev.kind == "skip":
            level = logging.DEBUG
        log.log(level, "[%s] %s", ev.kind.upper(), ev.message)

    def _save(self) -> None:
        if self.engine is not None:
            self.engine.risk.t.save(self.state_file)

    def _reconcile_balance(self) -> None:
        """When flat, trust the API's balance over our own bookkeeping."""
        eng = self.engine
        if self.mode != "live" or eng is None or not eng.broker.is_flat or self.account is None:
            return
        try:
            accts = self.client.search_accounts(only_active=True)
        except ProjectXError:
            return
        for a in accts:
            if int(a.get("id")) == int(self.account["id"]):
                api_bal = float(a.get("balance") or 0)
                t = eng.risk.t
                diff = api_bal - t.balance
                if abs(diff) >= 1.0:
                    log.info("Balance reconciled with TopstepX: %.2f -> %.2f", t.balance, api_bal)
                    t.s.balance = api_bal
                    key = t.s.current_day
                    if key:
                        t.s.daily_pnl[key] = float(t.s.daily_pnl.get(key, 0.0)) + diff
                    t._check_breach(api_bal)
                if not a.get("canTrade", True):
                    log.warning("TopstepX reports canTrade=false for this account")

    # ---- loop -----------------------------------------------------------------------
    def stop(self, *_args) -> None:
        log.warning("Stop requested")
        self._stop = True

    def step(self) -> None:
        eng = self.engine
        assert eng is not None and self.inst is not None
        now = utcnow()
        since = (self.last_ts or now - timedelta(minutes=30)) - timedelta(minutes=2)
        bars = self.client.retrieve_bars(self.inst.contract_id, since, now + ONE_MIN, include_partial=True)
        done = [b for b in bars if b.ts + ONE_MIN <= now - timedelta(seconds=1)]
        partial = [b for b in bars if b.ts + ONE_MIN > now - timedelta(seconds=1)]
        for b in done:
            if self.last_ts is None or b.ts > self.last_ts:
                eng.on_bar(b)
                self.last_ts = b.ts
                self.last_price = b.close
        if partial:
            self.last_price = partial[-1].close
        eng.heartbeat(now, self.last_price)

    def run(self) -> None:
        self.setup()
        self.warmup()
        self._seed_today()
        signal.signal(signal.SIGINT, self.stop)
        try:
            signal.signal(signal.SIGTERM, self.stop)
        except (AttributeError, ValueError):
            pass
        log.info("Bot running (%s). Ctrl+C to stop.", self.mode.upper())
        errors = 0
        loops = 0
        while not self._stop:
            try:
                self.step()
                loops += 1
                if loops % 12 == 0:
                    self._reconcile_balance()
                    self._save()
                    self._status_line()
                errors = 0
            except (ProjectXError, requests.RequestException) as exc:
                errors += 1
                log.error("API error (%d in a row): %s", errors, exc)
                if errors >= 12:
                    log.critical("Too many consecutive API errors; stopping the bot.")
                    break
            except Exception:
                log.exception("Unexpected error in main loop")
                errors += 1
                if errors >= 5:
                    break
            for _ in range(int(self.poll * 10)):
                if self._stop:
                    break
                time.sleep(0.1)
        self.shutdown()

    def _status_line(self) -> None:
        eng = self.engine
        assert eng is not None
        t = eng.risk.t
        pos = eng.broker.position
        pos_s = f"{pos.side.name} x{pos.size} @ {pos.entry_price:.2f} stop {pos.stop_price:.2f}" if pos else "flat"
        log.info("Status | %s | balance %.2f | day %+.2f | to target %.2f | MLL %.2f (room %.2f) | %s",
                 t.status.upper(), t.balance, t.day_pnl(), t.required_total_profit() - t.total_profit,
                 t.mll, t.balance - t.mll, pos_s)

    def shutdown(self) -> None:
        eng = self.engine
        if eng is not None and self.flatten_on_exit and eng.broker.position is not None:
            log.warning("Flattening open position before exit")
            try:
                for t in eng.broker.flatten("bot shutdown", self.last_price or 0.0, utcnow()):
                    eng._record(t)
            except Exception:
                log.exception("Flatten on exit failed. CHECK YOUR TOPSTEPX ACCOUNT MANUALLY.")
        elif eng is not None and eng.broker.position is not None:
            log.warning("Exiting with an open position; its server-side stop/target remain active.")
        self._save()
        log.info("Bot stopped.")
