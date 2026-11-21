"""Trading-session clock: Chicago time, trading-day boundaries, entry windows,
forced-flat time and news blackouts.

CME equity futures trade almost 24h. A Topstep "trading day" starts at
5:00 PM CT and runs to the next afternoon, so bars at/after 17:00 CT belong to
the NEXT calendar day's session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo


def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))


@dataclass
class Blackout:
    """A no-new-entries window. ``day`` = None means it applies every day."""

    start: time
    end: time
    day: Optional[date] = None
    flatten: bool = False  # also close any open position when the window starts
    label: str = ""

    def active(self, local_dt: datetime, trading_day: date) -> bool:
        if self.day is not None and self.day != trading_day:
            return False
        t = local_dt.time()
        return self.start <= t < self.end

    @staticmethod
    def parse(spec) -> "Blackout":
        """Accepts dicts like {"time": "07:25-07:40"} or
        {"date": "2026-12-16", "time": "12:55-13:30", "flatten": true, "label": "FOMC"}."""
        if isinstance(spec, str):
            spec = {"time": spec}
        start_s, end_s = spec["time"].split("-")
        day = spec.get("date")
        if isinstance(day, str):
            day = date.fromisoformat(day)
        return Blackout(
            start=parse_hhmm(start_s),
            end=parse_hhmm(end_s),
            day=day,
            flatten=bool(spec.get("flatten", False)),
            label=str(spec.get("label", "")),
        )


@dataclass
class SessionClock:
    timezone: str = "America/Chicago"
    day_start: time = time(17, 0)  # trading day rolls over at 5 PM CT
    rth_open: time = time(8, 30)
    rth_close: time = time(15, 0)
    entry_windows: list[tuple[time, time]] = field(
        default_factory=lambda: [(time(8, 35), time(11, 0)), (time(13, 0), time(14, 30))]
    )
    flatten_time: time = time(15, 0)  # Topstep requires flat by 3:10 PM CT; we leave margin
    blackouts: list[Blackout] = field(default_factory=list)
    trade_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon..Fri (by trading day)

    def __post_init__(self):
        self.tz = ZoneInfo(self.timezone)

    # ---- conversions -------------------------------------------------
    def local(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return ts.astimezone(self.tz)

    def trading_day(self, ts: datetime) -> date:
        lt = self.local(ts)
        d = lt.date()
        if lt.time() >= self.day_start:
            d = d + timedelta(days=1)
        return d

    def rth_open_dt(self, trading_day: date) -> datetime:
        return datetime.combine(trading_day, self.rth_open, tzinfo=self.tz)

    def flatten_dt(self, trading_day: date) -> datetime:
        return datetime.combine(trading_day, self.flatten_time, tzinfo=self.tz)

    # ---- rules -------------------------------------------------------
    def is_trade_day(self, trading_day: date) -> bool:
        return trading_day.weekday() in self.trade_weekdays

    def in_rth(self, ts: datetime) -> bool:
        t = self.local(ts).time()
        return self.rth_open <= t < self.rth_close

    def in_entry_window(self, ts: datetime) -> bool:
        lt = self.local(ts)
        t = lt.time()
        if not self.is_trade_day(self.trading_day(ts)):
            return False
        return any(start <= t < end for start, end in self.entry_windows)

    def active_blackout(self, ts: datetime) -> Optional[Blackout]:
        lt = self.local(ts)
        day = self.trading_day(ts)
        for b in self.blackouts:
            if b.active(lt, day):
                return b
        return None

    def can_enter(self, ts: datetime) -> tuple[bool, str]:
        if not self.in_entry_window(ts):
            return False, "outside entry window"
        if self.must_be_flat(ts):
            return False, "past flatten time"
        b = self.active_blackout(ts)
        if b is not None:
            return False, f"news blackout {b.label or ''}".strip()
        return True, ""

    def must_be_flat(self, ts: datetime) -> bool:
        """True from flatten_time until the next trading day starts."""
        lt = self.local(ts)
        t = lt.time()
        return self.flatten_time <= t < self.day_start

    def must_flatten_for_news(self, ts: datetime) -> Optional[Blackout]:
        b = self.active_blackout(ts)
        return b if (b is not None and b.flatten) else None

    @staticmethod
    def from_config(cfg: dict) -> "SessionClock":
        cfg = cfg or {}
        windows = cfg.get("entry_windows")
        kwargs = {}
        if "timezone" in cfg:
            kwargs["timezone"] = cfg["timezone"]
        for key in ("day_start", "rth_open", "rth_close", "flatten_time"):
            if key in cfg:
                kwargs[key] = parse_hhmm(cfg[key])
        if windows:
            kwargs["entry_windows"] = [
                (parse_hhmm(w.split("-")[0]), parse_hhmm(w.split("-")[1])) for w in windows
            ]
        if "blackouts" in cfg:
            kwargs["blackouts"] = [Blackout.parse(b) for b in cfg["blackouts"] or []]
        if "trade_weekdays" in cfg:
            kwargs["trade_weekdays"] = tuple(int(x) for x in cfg["trade_weekdays"])
        return SessionClock(**kwargs)


def iter_trading_days(clock: SessionClock, timestamps: Iterable[datetime]) -> list[date]:
    seen: list[date] = []
    last = None
    for ts in timestamps:
        d = clock.trading_day(ts)
        if d != last:
            seen.append(d)
            last = d
    return seen
