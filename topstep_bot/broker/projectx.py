"""TopstepX (ProjectX Gateway API) REST client.

Endpoints follow the ProjectX Gateway API (https://gateway.docs.projectx.com):
all calls are POST with a JSON body and a Bearer token from /api/Auth/loginKey.

Enums used (from the ProjectX docs):
  OrderType:    1 Limit, 2 Market, 4 Stop, 5 TrailingStop, 6 JoinBid, 7 JoinAsk
  OrderSide:    0 Bid (buy), 1 Ask (sell)
  PositionType: 1 Long, 2 Short
  BarUnit:      1 Second, 2 Minute, 3 Hour, 4 Day, 5 Week, 6 Month
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from ..models import Bar

log = logging.getLogger("topstep_bot.projectx")

ORDER_LIMIT, ORDER_MARKET, ORDER_STOP, ORDER_TRAILING = 1, 2, 4, 5
SIDE_BUY, SIDE_SELL = 0, 1
POS_LONG, POS_SHORT = 1, 2
UNIT_SECOND, UNIT_MINUTE, UNIT_HOUR, UNIT_DAY = 1, 2, 3, 4
# Order status values as documented by ProjectX.
STATUS_OPEN, STATUS_FILLED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_REJECTED, STATUS_PENDING = 1, 2, 3, 4, 5, 6


class ProjectXError(RuntimeError):
    def __init__(self, message: str, code: Any = None, path: str = ""):
        super().__init__(f"{path}: {message} (errorCode={code})" if path else message)
        self.code = code
        self.api_message = message
        self.path = path


class _RateLimiter:
    """Sliding-window limiter: at most `n` calls per `window` seconds."""

    def __init__(self, n: int, window: float):
        self.n = n
        self.window = window
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > self.window:
                self.calls.popleft()
            if len(self.calls) >= self.n:
                sleep_for = self.window - (now - self.calls[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self.calls.append(time.monotonic())


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    # Trim fractional seconds beyond microseconds, which fromisoformat rejects on older Pythons.
    if "." in s:
        head, _, rest = s.partition(".")
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6].ljust(6, '0')}{tz}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ProjectXClient:
    TOKEN_MAX_AGE = timedelta(hours=23)

    def __init__(
        self,
        username: str,
        api_key: str,
        base_url: str = "https://api.topstepx.com",
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ):
        self.username = username
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()
        self.token: Optional[str] = None
        self.token_time: Optional[datetime] = None
        # Conservative limits (below the published ProjectX limits).
        self._general = _RateLimiter(150, 60.0)
        self._history = _RateLimiter(40, 30.0)

    # ---- auth --------------------------------------------------------------
    def login(self) -> None:
        data = self._raw_post("/api/Auth/loginKey", {"userName": self.username, "apiKey": self.api_key}, auth=False)
        token = data.get("token")
        if not token:
            raise ProjectXError("login returned no token", data.get("errorCode"), "/api/Auth/loginKey")
        self.token = token
        self.token_time = datetime.now(timezone.utc)
        log.info("Authenticated with TopstepX API")

    def _ensure_token(self) -> None:
        if self.token is None or self.token_time is None:
            self.login()
            return
        if datetime.now(timezone.utc) - self.token_time > self.TOKEN_MAX_AGE:
            try:
                data = self._raw_post("/api/Auth/validate", {}, auth=True)
                new = data.get("newToken")
                if new:
                    self.token = new
                    self.token_time = datetime.now(timezone.utc)
                    return
            except Exception as exc:  # fall back to a fresh login
                log.warning("token validate failed (%s); logging in again", exc)
            self.login()

    # ---- transport -----------------------------------------------------------
    def _raw_post(self, path: str, payload: dict, auth: bool = True, retries: int = 3) -> dict:
        url = self.base + path
        limiter = self._history if path.startswith("/api/History") else self._general
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            limiter.wait()
            headers = {"Content-Type": "application/json", "accept": "text/plain"}
            if auth and self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                resp = self.http.post(url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(2 ** attempt, 10)
                log.warning("network error on %s (%s); retry in %ss", path, exc, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 401 and auth and attempt < retries:
                log.warning("401 on %s; re-authenticating", path)
                self.login()
                continue
            if resp.status_code == 429:
                wait = min(2 ** (attempt + 1), 30)
                log.warning("rate limited on %s; sleeping %ss", path, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code != 200:
                raise ProjectXError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code, path)
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProjectXError(f"invalid JSON: {resp.text[:200]}", None, path) from exc
            if isinstance(data, dict) and data.get("success") is False:
                raise ProjectXError(str(data.get("errorMessage") or "request failed"), data.get("errorCode"), path)
            return data
        raise ProjectXError(f"request failed after retries: {last_exc}", None, path)

    def post(self, path: str, payload: dict) -> dict:
        self._ensure_token()
        return self._raw_post(path, payload, auth=True)

    # ---- account / contracts -----------------------------------------------
    def search_accounts(self, only_active: bool = True) -> list[dict]:
        return self.post("/api/Account/search", {"onlyActiveAccounts": only_active}).get("accounts", []) or []

    def search_contracts(self, text: str, live: bool = False) -> list[dict]:
        return self.post("/api/Contract/search", {"searchText": text, "live": live}).get("contracts", []) or []

    def contract_by_id(self, contract_id: str) -> Optional[dict]:
        return self.post("/api/Contract/searchById", {"contractId": contract_id}).get("contract")

    def resolve_contract(self, symbol: str, live: bool = False) -> dict:
        """Find the active front-month contract for a root symbol like 'MNQ'."""
        symbol = symbol.upper()
        cands = self.search_contracts(symbol, live=live)

        def root(c: dict) -> str:
            sid = str(c.get("symbolId") or "")
            if sid:
                return sid.split(".")[-1].upper()
            parts = str(c.get("id", "")).split(".")
            return parts[3].upper() if len(parts) > 3 else ""

        exact = [c for c in cands if root(c) == symbol]
        active = [c for c in exact if c.get("activeContract")]
        pick = (active or exact)
        if not pick:
            names = ", ".join(str(c.get("name")) for c in cands[:10])
            raise ProjectXError(f"no contract found for {symbol}; search returned: {names or 'nothing'}")
        return pick[0]

    # ---- market data ---------------------------------------------------------
    def retrieve_bars(
        self,
        contract_id: str,
        start: datetime,
        end: datetime,
        unit: int = UNIT_MINUTE,
        unit_number: int = 1,
        limit: int = 20000,
        include_partial: bool = False,
        live: bool = False,
    ) -> list[Bar]:
        data = self.post(
            "/api/History/retrieveBars",
            {
                "contractId": contract_id,
                "live": live,
                "startTime": _iso(start),
                "endTime": _iso(end),
                "unit": unit,
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": include_partial,
            },
        )
        bars = []
        for b in data.get("bars", []) or []:
            bars.append(
                Bar(
                    ts=_parse_dt(b["t"]),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b.get("v") or 0),
                )
            )
        bars.sort(key=lambda x: x.ts)
        return bars

    def retrieve_bars_range(self, contract_id: str, start: datetime, end: datetime, chunk_days: int = 7) -> list[Bar]:
        """Download 1-minute bars over a long range in chunks."""
        out: dict[datetime, Bar] = {}
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=chunk_days), end)
            for b in self.retrieve_bars(contract_id, cur, nxt):
                out[b.ts] = b
            cur = nxt
        return [out[k] for k in sorted(out)]

    # ---- orders ----------------------------------------------------------------
    def place_order(
        self,
        account_id: int,
        contract_id: str,
        order_type: int,
        side: int,
        size: int,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_price: Optional[float] = None,
        custom_tag: Optional[str] = None,
        stop_loss_bracket: Optional[dict] = None,
        take_profit_bracket: Optional[dict] = None,
    ) -> int:
        payload: dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": order_type,
            "side": side,
            "size": int(size),
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "trailPrice": trail_price,
            "customTag": custom_tag,
        }
        if stop_loss_bracket is not None:
            payload["stopLossBracket"] = stop_loss_bracket
        if take_profit_bracket is not None:
            payload["takeProfitBracket"] = take_profit_bracket
        data = self.post("/api/Order/place", payload)
        oid = data.get("orderId")
        if oid is None:
            raise ProjectXError("order placed but no orderId returned", data.get("errorCode"), "/api/Order/place")
        return int(oid)

    def cancel_order(self, account_id: int, order_id: int) -> None:
        self.post("/api/Order/cancel", {"accountId": account_id, "orderId": order_id})

    def modify_order(
        self,
        account_id: int,
        order_id: int,
        size: Optional[int] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        trail_price: Optional[float] = None,
    ) -> None:
        payload: dict[str, Any] = {"accountId": account_id, "orderId": order_id}
        if size is not None:
            payload["size"] = size
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if trail_price is not None:
            payload["trailPrice"] = trail_price
        self.post("/api/Order/modify", payload)

    def search_open_orders(self, account_id: int) -> list[dict]:
        return self.post("/api/Order/searchOpen", {"accountId": account_id}).get("orders", []) or []

    def search_orders(self, account_id: int, start: datetime, end: Optional[datetime] = None) -> list[dict]:
        payload: dict[str, Any] = {"accountId": account_id, "startTimestamp": _iso(start)}
        if end is not None:
            payload["endTimestamp"] = _iso(end)
        return self.post("/api/Order/search", payload).get("orders", []) or []

    # ---- positions / trades ------------------------------------------------------
    def search_open_positions(self, account_id: int) -> list[dict]:
        return self.post("/api/Position/searchOpen", {"accountId": account_id}).get("positions", []) or []

    def close_position(self, account_id: int, contract_id: str) -> None:
        self.post("/api/Position/closeContract", {"accountId": account_id, "contractId": contract_id})

    def search_trades(self, account_id: int, start: datetime, end: Optional[datetime] = None) -> list[dict]:
        payload: dict[str, Any] = {"accountId": account_id, "startTimestamp": _iso(start)}
        if end is not None:
            payload["endTimestamp"] = _iso(end)
        return self.post("/api/Trade/search", payload).get("trades", []) or []


def parse_api_time(s: str) -> datetime:
    return _parse_dt(s)
