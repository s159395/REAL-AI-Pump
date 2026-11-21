from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from topstep_bot.models import Bar

CT = ZoneInfo("America/Chicago")


def ct(d: date, hh: int, mm: int) -> datetime:
    """A Chicago wall-clock time converted to UTC."""
    return datetime.combine(d, time(hh, mm), tzinfo=CT).astimezone(timezone.utc)


def bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100.0) -> Bar:
    return Bar(ts, o, h, l, c, v)


def flat_bars(start: datetime, n: int, price: float) -> list[Bar]:
    return [Bar(start + timedelta(minutes=i), price, price + 0.25, price - 0.25, price, 100) for i in range(n)]


def path_bars(start: datetime, closes: list[float], wick: float = 0.5) -> list[Bar]:
    """1-minute bars that walk through the given closes."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Bar(start + timedelta(minutes=i), o, max(o, c) + wick, min(o, c) - wick, c, 100))
        prev = c
    return out
