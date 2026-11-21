"""Topstep Combine rule tracking + the bot's own risk manager.

CombineTracker mirrors the account rules (profit target, trailing Maximum Loss
Limit, optional Daily Loss Limit, consistency target) so the bot always knows
how far it is from passing or failing.

RiskManager decides *whether* a trade may be opened and *how many contracts*,
so that a full stop-out can never breach the MLL or the daily limits.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .models import Instrument, Signal, TradeRecord

ACTIVE, PASSED, FAILED = "active", "passed", "failed"


@dataclass
class CombineState:
    balance: float
    mll: float
    peak_eod: float
    peak_intraday: float
    day_start_balance: float
    current_day: Optional[str] = None
    daily_pnl: dict = field(default_factory=dict)  # "YYYY-MM-DD" -> realized net P&L
    status: str = ACTIVE
    status_reason: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "CombineState":
        return CombineState(**json.loads(text))


class CombineTracker:
    def __init__(self, rules: dict, state: Optional[CombineState] = None, enforce: bool = True):
        self.start = float(rules.get("starting_balance", 50000.0))
        self.target = float(rules.get("profit_target", 3000.0))
        self.max_loss = float(rules.get("max_loss_limit", 2000.0))
        self.trailing = str(rules.get("mll_trailing", "eod")).lower()
        self.stops_at_start = bool(rules.get("mll_stops_at_start", True))
        dll = rules.get("daily_loss_limit")
        self.dll: Optional[float] = float(dll) if dll else None
        self.consistency = float(rules.get("consistency_pct", 0.5) or 0)
        self.min_days = int(rules.get("min_trading_days", 1))
        self.max_minis = float(rules.get("max_minis", 5))
        self.enforce = enforce
        self.s = state or CombineState(
            balance=self.start,
            mll=self.start - self.max_loss,
            peak_eod=self.start,
            peak_intraday=self.start,
            day_start_balance=self.start,
        )

    # ---- persistence ---------------------------------------------------
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.s.to_json(), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def load_state(path: str | Path) -> Optional[CombineState]:
        p = Path(path)
        if not p.exists():
            return None
        return CombineState.from_json(p.read_text(encoding="utf-8"))

    # ---- properties ----------------------------------------------------
    @property
    def balance(self) -> float:
        return self.s.balance

    @property
    def mll(self) -> float:
        return self.s.mll if self.enforce else -math.inf

    @property
    def status(self) -> str:
        return self.s.status

    @property
    def total_profit(self) -> float:
        return self.s.balance - self.start

    def day_pnl(self) -> float:
        if self.s.current_day is None:
            return 0.0
        return float(self.s.daily_pnl.get(self.s.current_day, 0.0))

    @property
    def trading_days(self) -> int:
        return len(self.s.daily_pnl)

    @property
    def best_day(self) -> float:
        vals = list(self.s.daily_pnl.values())
        return max(vals) if vals else 0.0

    def consistency_ok(self) -> bool:
        if self.consistency <= 0:
            return True
        tp = self.total_profit
        return tp > 0 and self.best_day < self.consistency * tp

    def required_total_profit(self) -> float:
        """Total profit needed to pass given the best day so far."""
        need = self.target
        if self.consistency > 0 and self.best_day > 0:
            need = max(need, self.best_day / self.consistency + 1.0)
        return need

    # ---- lifecycle -----------------------------------------------------
    def start_day(self, day: date) -> None:
        key = day.isoformat()
        if self.s.current_day == key:
            return
        if self.s.current_day is not None:
            self.end_day()
        self.s.current_day = key
        self.s.day_start_balance = self.s.balance

    def end_day(self) -> None:
        """End-of-day: trail the MLL from the closing balance, check for a pass."""
        if self.trailing == "eod":
            self.s.peak_eod = max(self.s.peak_eod, self.s.balance)
            self._trail(self.s.peak_eod)
        self._check_pass()

    def _trail(self, peak: float) -> None:
        new_mll = peak - self.max_loss
        if self.stops_at_start:
            new_mll = min(new_mll, self.start)
        self.s.mll = max(self.s.mll, new_mll)

    def record_trade(self, net_pnl: float) -> None:
        self.s.balance += net_pnl
        key = self.s.current_day or "unknown"
        self.s.daily_pnl[key] = float(self.s.daily_pnl.get(key, 0.0)) + net_pnl
        if self.trailing == "intraday":
            self.s.peak_intraday = max(self.s.peak_intraday, self.s.balance)
            self._trail(self.s.peak_intraday)
        self._check_breach(self.s.balance)
        self._check_pass()

    def mark(self, unrealized_worst: float, unrealized_best: float = 0.0) -> None:
        """Intraday mark-to-market check. Unrealized losses count toward the MLL."""
        if self.trailing == "intraday" and unrealized_best > 0:
            self.s.peak_intraday = max(self.s.peak_intraday, self.s.balance + unrealized_best)
            self._trail(self.s.peak_intraday)
        self._check_breach(self.s.balance + unrealized_worst)

    def dll_hit(self, unrealized: float = 0.0) -> bool:
        return self.dll is not None and (self.day_pnl() + unrealized) <= -self.dll

    def _check_breach(self, equity: float) -> None:
        if not self.enforce or self.s.status != ACTIVE:
            return
        if equity <= self.s.mll:
            self.s.status = FAILED
            self.s.status_reason = f"Maximum Loss Limit breached: equity {equity:.2f} <= MLL {self.s.mll:.2f}"

    def _check_pass(self) -> None:
        if not self.enforce or self.s.status != ACTIVE:
            return
        if (
            self.total_profit >= self.target
            and self.consistency_ok()
            and self.trading_days >= self.min_days
        ):
            self.s.status = PASSED
            self.s.status_reason = (
                f"Profit target reached: +{self.total_profit:.2f} over {self.trading_days} days, "
                f"best day {self.best_day:.2f}"
            )


class RiskManager:
    def __init__(
        self,
        risk_cfg: dict,
        tracker: CombineTracker,
        instrument: Instrument,
        commission_per_contract: float = 0.0,
    ):
        self.c = dict(risk_cfg or {})
        self.t = tracker
        self.inst = instrument
        self.commission = float(commission_per_contract)
        self.risk_per_trade = float(self.c.get("risk_per_trade", 200.0))
        self.max_daily_loss = float(self.c.get("max_daily_loss", 450.0))
        self.daily_profit_cap = float(self.c.get("daily_profit_cap", 900.0) or 0)
        self.max_trades = int(self.c.get("max_trades_per_day", 3))
        self.max_consec = int(self.c.get("max_consecutive_losses", 2))
        self.max_contracts = int(self.c.get("max_contracts", 15))
        self.min_contracts = int(self.c.get("min_contracts", 1))
        self.mll_buffer = float(self.c.get("mll_buffer", 150.0))
        self.stop_after_pass = bool(self.c.get("stop_after_pass", True))
        self.slip_ticks = float(self.c.get("slippage_ticks_estimate", 1))
        self.dd_scale = float(self.c.get("drawdown_risk_scale", 1.0))
        self.dd_threshold = float(self.c.get("drawdown_scale_threshold", 1e18))
        self.scale_near_target = bool(self.c.get("scale_down_near_target", True))
        self.trades_today = 0
        self.consec_losses = 0
        self.locked_reason: str = ""

    # ---- day lifecycle -------------------------------------------------
    def start_day(self, day: date) -> None:
        self.t.start_day(day)
        self.trades_today = 0
        self.consec_losses = 0
        self.locked_reason = ""

    def on_entry(self) -> None:
        self.trades_today += 1

    def on_trade_closed(self, trade: TradeRecord) -> None:
        self.t.record_trade(trade.net_pnl)
        if trade.net_pnl < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0
        dp = self.t.day_pnl()
        if dp <= -self.max_daily_loss:
            self.locked_reason = f"daily loss limit reached ({dp:.2f})"
        elif self.daily_profit_cap and dp >= self.daily_profit_cap:
            self.locked_reason = f"daily profit cap reached (+{dp:.2f})"
        elif self.max_consec and self.consec_losses >= self.max_consec:
            self.locked_reason = f"{self.consec_losses} consecutive losses"
        elif self.t.dll_hit():
            self.locked_reason = "Topstep daily loss limit hit"

    # ---- gates ---------------------------------------------------------
    def can_open(self) -> tuple[bool, str]:
        st = self.t.status
        if st == FAILED:
            return False, "combine failed"
        if st == PASSED and self.stop_after_pass:
            return False, "combine passed - trading stopped"
        if self.locked_reason:
            return False, self.locked_reason
        if self.trades_today >= self.max_trades:
            return False, f"max trades per day ({self.max_trades})"
        if self.max_consec and self.consec_losses >= self.max_consec:
            return False, "consecutive loss limit"
        dp = self.t.day_pnl()
        if dp <= -self.max_daily_loss:
            return False, "daily loss limit"
        if self.daily_profit_cap and dp >= self.daily_profit_cap:
            return False, "daily profit cap"
        if self.t.balance - self.t.mll - self.mll_buffer <= 0:
            return False, "too close to Maximum Loss Limit"
        return True, ""

    def per_contract_risk(self, signal: Signal) -> float:
        stop_ticks = self.inst.to_ticks(signal.entry_ref - signal.stop)
        return (stop_ticks + 2 * self.slip_ticks) * self.inst.tick_value + self.commission

    def risk_budget(self, target_r: float = 2.0) -> float:
        budget = self.risk_per_trade
        if self.t.balance < self.t.s.peak_eod - self.dd_threshold:
            budget *= self.dd_scale
        if self.scale_near_target and self.t.enforce:
            remaining = self.t.required_total_profit() - self.t.total_profit
            if remaining > 0 and target_r > 0:
                needed = remaining / target_r * 1.25
                budget = min(budget, max(needed, self.risk_per_trade * 0.25))
        # A full stop-out must not land within mll_buffer of the MLL...
        budget = min(budget, self.t.balance - self.t.mll - self.mll_buffer)
        # ...or push the day past the bot's daily loss limit...
        budget = min(budget, self.max_daily_loss + self.t.day_pnl())
        # ...or past Topstep's DLL if the account has one.
        if self.t.dll is not None:
            budget = min(budget, self.t.dll + self.t.day_pnl() - 25.0)
        return max(budget, 0.0)

    def size_for(self, signal: Signal) -> tuple[int, float, str]:
        """Returns (contracts, dollars at risk, reason-if-zero)."""
        per = self.per_contract_risk(signal)
        if per <= 0:
            return 0, 0.0, "invalid stop distance"
        target_r = signal.reward_points / signal.risk_points if signal.risk_points > 0 else 2.0
        budget = self.risk_budget(target_r)
        size = int(math.floor(budget / per + 1e-9))
        topstep_cap = int(math.floor(self.t.max_minis / self.inst.mini_equivalent + 1e-9))
        size = min(size, self.max_contracts, topstep_cap)
        if size < self.min_contracts:
            return 0, 0.0, f"risk ${per:.2f}/contract exceeds budget ${budget:.2f}"
        return size, size * per, ""

    def must_flatten(self, unrealized: float) -> Optional[str]:
        """Emergency exits checked on every update while a position is open."""
        equity = self.t.balance + unrealized
        if self.t.enforce and equity <= self.t.mll + 50.0:
            return "equity within $50 of the Maximum Loss Limit"
        if self.t.dll is not None and self.t.day_pnl() + unrealized <= -(self.t.dll - 25.0):
            return "approaching Topstep daily loss limit"
        # Kill switch if a stop failed and the day is far past the bot's limit.
        if self.t.day_pnl() + unrealized <= -1.5 * self.max_daily_loss:
            return "hard daily loss kill-switch"
        return None
