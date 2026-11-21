"""Configuration loading: built-in defaults deep-merged with a YAML file.

Secrets (API key, username) come from environment variables or a .env file,
never from the YAML.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "instrument": {
        # Micro Nasdaq by default: $2/point lets the bot size risk precisely.
        "symbol": "MNQ",
        # Optional overrides (live trading reads these from the API instead).
        "tick_size": None,
        "tick_value": None,
        "contract_id": None,  # e.g. "CON.F.US.MNQ.Z26"; auto-resolved if null
    },
    # Topstep 50K Trading Combine rules. VERIFY these against your own account's
    # rules page before trading. Topstep changes them from time to time.
    "combine": {
        "starting_balance": 50000.0,
        "profit_target": 3000.0,
        "max_loss_limit": 2000.0,
        "mll_trailing": "eod",  # "eod" (end-of-day balance) or "intraday"
        "mll_stops_at_start": True,  # MLL stops trailing once it reaches the starting balance
        "daily_loss_limit": None,  # TopstepX: optional add-on ($1,000 on 50K). null = none
        "max_minis": 5,  # 5 minis = 50 micros on a 50K
        "consistency_pct": 0.50,  # best day must be < 50% of total profit
        "min_trading_days": 1,  # the consistency rule already forces >= 2 days
    },
    # The bot's own, stricter risk rules. These are what keep you in the game.
    "risk": {
        "risk_per_trade": 200.0,  # $ lost if the stop is hit (before slippage)
        "max_daily_loss": 450.0,  # stop trading for the day at -$450
        "daily_profit_cap": 900.0,  # stop for the day at +$900 (protects consistency rule)
        "max_trades_per_day": 3,
        "max_consecutive_losses": 2,  # per day
        "max_contracts": 15,  # hard cap on contracts per trade (of the traded symbol)
        "mll_buffer": 150.0,  # never let a full stop-out land closer than this to the MLL
        "min_contracts": 1,
        "stop_after_pass": True,  # stop trading once the combine is passed
        "slippage_ticks_estimate": 1,  # added to per-contract risk when sizing
        # Risk scaling: trade smaller after losing days, normal size otherwise.
        "drawdown_risk_scale": 0.5,  # multiply risk by this while below starting balance
        "drawdown_scale_threshold": 500.0,  # ...when more than this below the high-water EOD
    },
    "session": {
        "timezone": "America/Chicago",
        "day_start": "17:00",
        "rth_open": "08:30",
        "rth_close": "15:00",
        "entry_windows": ["08:35-11:00", "13:00-14:30"],
        "flatten_time": "15:00",  # Topstep: flat by 3:10 PM CT. We leave 10 minutes.
        "trade_weekdays": [0, 1, 2, 3, 4],
        # Daily blackout around 7:30 CT data releases, plus add FOMC/CPI dates yourself.
        "blackouts": [
            {"time": "07:25-07:40", "label": "8:30 ET data"},
        ],
    },
    "strategies": {
        # Which strategies run. All share one position and one risk manager.
        "enabled": ["orb", "vwap_pullback"],
        "orb": {
            "timeframe": 5,
            "or_minutes": 15,
            "entry_end": "10:30",
            "stop_mode": "mid",  # "mid" (OR midpoint) or "opposite" (other side of OR)
            "target_r": 2.0,
            "buffer_ticks": 2,
            "min_stop_points": 8.0,
            "max_stop_points": 60.0,
            "vwap_filter": True,
            "ema_trend_filter": True,
            "trend_ema": 50,
            "max_entries": 1,
        },
        "vwap_pullback": {
            "timeframe": 5,
            "fast_ema": 9,
            "slow_ema": 21,
            "slope_lookback": 3,
            "atr_period": 14,
            "touch_atr": 0.25,  # pullback must reach within 0.25 ATR of fast EMA
            "stop_buffer_ticks": 4,
            "swing_lookback": 3,
            "min_stop_points": 8.0,
            "max_stop_points": 50.0,
            "min_stop_atr": 0.8,
            "target_r": 1.8,
            "max_entries": 2,
            "cooldown_bars": 3,
            "start": "09:00",
            "end": "14:00",
            "max_vwap_distance_atr": 3.0,  # don't chase when price is stretched far from VWAP
        },
    },
    "management": {
        "breakeven_at_r": 1.0,  # move stop to entry + offset once trade is +1R
        "breakeven_offset_ticks": 2,
        "time_stop_minutes": 120,  # close a trade that hasn't resolved in 2 hours
    },
    "backtest": {
        "commission_per_contract": 0.74,  # round turn, $ (micros). Set to your actual fees.
        "slippage_ticks": 1,  # per side on market/stop fills
        "combine_max_days": 30,  # give up on a simulated combine after this many days
    },
    "live": {
        "base_url": "https://api.topstepx.com",
        "account_id": None,  # numeric; or set account_name
        "account_name": None,
        "poll_seconds": 5,
        "bracket_mode": "native",  # "native" (server-side OCO brackets) or "manual"
        "bracket_tick_sign": "auto",  # "auto", "signed" or "unsigned"
        "warmup_days": 4,
        "state_file": "state/combine_state.json",
        "journal_file": "logs/trades.csv",
        "log_file": "logs/bot.log",
        "max_order_retries": 2,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | os.PathLike | None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        cfg = deep_merge(cfg, user)
    return cfg


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def credentials() -> tuple[str, str]:
    user = os.environ.get("TOPSTEPX_USERNAME", "").strip()
    key = os.environ.get("TOPSTEPX_API_KEY", "").strip()
    if not user or not key:
        raise RuntimeError(
            "Missing credentials. Set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY "
            "(see .env.example)."
        )
    return user, key
