"""Bar data I/O: CSV loading/saving and a synthetic generator for smoke tests.

The synthetic generator is ONLY for testing that the code runs. Its results say
nothing about how a strategy performs in real markets. Backtest on real data
(e.g. `python -m topstep_bot download ...`) before trusting any strategy.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .models import Bar

_TS_KEYS = ("timestamp", "datetime", "time", "date_time", "ts", "t", "date")
_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "last"),
    "volume": ("volume", "v", "vol"),
}


def _parse_ts(raw: str, tz: ZoneInfo) -> datetime:
    raw = raw.strip()
    try:
        x = float(raw)
        if x > 1e12:  # epoch milliseconds
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=timezone.utc)
    except ValueError:
        pass
    s = raw.replace("Z", "+00:00")
    for fmt in (None, "%Y%m%d %H%M%S", "%Y%m%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognised timestamp: {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def load_csv(path: str | Path, tz: str = "UTC", bar_time: str = "open") -> list[Bar]:
    """Load 1-minute OHLCV bars from a CSV.

    * ``tz``: timezone for timestamps that have no offset.
    * ``bar_time``: "open" if timestamps mark the bar's start (TopstepX, most
      vendors) or "close" if they mark its end (e.g. NinjaTrader exports).
    """
    zone = ZoneInfo(tz)
    shift = timedelta(minutes=-1) if bar_time == "close" else timedelta(0)
    bars: dict[datetime, Bar] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")
        cols = {k.strip().lower(): k for k in reader.fieldnames}

        def col(name: str) -> Optional[str]:
            for alias in _ALIASES[name]:
                if alias in cols:
                    return cols[alias]
            return None

        c_open, c_high, c_low, c_close, c_vol = (col(n) for n in ("open", "high", "low", "close", "volume"))
        if not all((c_open, c_high, c_low, c_close)):
            raise ValueError(f"CSV needs open/high/low/close columns, got {reader.fieldnames}")
        split_date_time = "date" in cols and "time" in cols
        ts_col = None
        if not split_date_time:
            ts_col = next((cols[k] for k in _TS_KEYS if k in cols), None)
            if ts_col is None:
                raise ValueError("CSV needs a timestamp column (timestamp/datetime/time/date)")
        for row in reader:
            if split_date_time:
                raw = f"{row[cols['date']]} {row[cols['time']]}"
            else:
                raw = row[ts_col]
            if not raw:
                continue
            ts = _parse_ts(raw, zone) + shift
            bars[ts] = Bar(
                ts=ts,
                open=float(row[c_open]),
                high=float(row[c_high]),
                low=float(row[c_low]),
                close=float(row[c_close]),
                volume=float(row[c_vol]) if c_vol and row.get(c_vol) not in (None, "") else 0.0,
            )
    return [bars[k] for k in sorted(bars)]


def save_csv(bars: Iterable[Bar], path: str | Path) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts.astimezone(timezone.utc).isoformat(), b.open, b.high, b.low, b.close, b.volume])
            n += 1
    return n


def generate_synthetic(
    start: date,
    days: int,
    seed: int = 7,
    start_price: float = 21000.0,
    tick: float = 0.25,
    tz: str = "America/Chicago",
    trend_days: bool = True,
) -> list[Bar]:
    """Random-walk 1-minute bars with an intraday volatility profile and a mix
    of trend and range days. FOR SOFTWARE TESTING ONLY."""
    rng = random.Random(seed)
    zone = ZoneInfo(tz)
    bars: list[Bar] = []
    price = start_price
    d = start
    made = 0
    while made < days:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        session_start = datetime.combine(d - timedelta(days=1), time(17, 0), tzinfo=zone)
        trend = rng.choice([-1, 0, 0, 1]) * rng.uniform(0.00002, 0.00008) if trend_days else 0.0
        t = session_start
        end = datetime.combine(d, time(16, 0), tzinfo=zone)
        while t < end:
            lt = t.time()
            if time(8, 30) <= lt < time(9, 30):
                vol = 0.00065
            elif time(9, 30) <= lt < time(15, 0):
                vol = 0.00035
            else:
                vol = 0.00012
            drift = trend if time(8, 30) <= lt < time(15, 0) else 0.0
            ret = drift + vol * rng.gauss(0, 1)
            o = price
            c = price * math.exp(ret)
            wick = abs(rng.gauss(0, vol)) * price
            h = max(o, c) + wick * rng.random()
            low = min(o, c) - wick * rng.random()

            def q(x: float) -> float:
                return round(round(x / tick) * tick, 2)

            o, h, low, c = q(o), q(h), q(low), q(c)
            h = max(h, o, c)
            low = min(low, o, c)
            v = rng.randint(50, 400) * (4 if vol > 0.0003 else 1)
            bars.append(Bar(t.astimezone(timezone.utc), o, h, low, c, float(v)))
            price = c
            t += timedelta(minutes=1)
        made += 1
        d += timedelta(days=1)
    return bars
