"""ProjectX REST client tests with a fake HTTP session (no network)."""

from datetime import datetime, timezone

import pytest

from topstep_bot.broker.projectx import ProjectXClient, ProjectXError, _parse_dt


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, script):
        self.script = list(script)  # list of (path_suffix, Resp)
        self.sent = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append((url, json, headers))
        suffix, resp = self.script.pop(0)
        assert url.endswith(suffix), f"expected {suffix}, got {url}"
        return resp


def client(script):
    s = FakeSession(script)
    return ProjectXClient("me", "key", session=s), s


def test_login_and_authorized_call():
    c, s = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "abc", "success": True})),
        ("/api/Account/search", Resp(200, {"accounts": [{"id": 7, "name": "50KTC"}], "success": True})),
    ])
    accts = c.search_accounts()
    assert accts[0]["id"] == 7
    assert s.sent[0][1] == {"userName": "me", "apiKey": "key"}
    assert s.sent[1][2]["Authorization"] == "Bearer abc"
    assert s.sent[1][1] == {"onlyActiveAccounts": True}


def test_api_error_raises():
    c, _ = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "abc", "success": True})),
        ("/api/Order/place", Resp(200, {"success": False, "errorCode": 2, "errorMessage": "Invalid ticks"})),
    ])
    with pytest.raises(ProjectXError) as ei:
        c.place_order(1, "CON", 2, 0, 1)
    assert "Invalid ticks" in ei.value.api_message


def test_401_triggers_relogin():
    c, s = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "old", "success": True})),
        ("/api/Position/searchOpen", Resp(401, {})),
        ("/api/Auth/loginKey", Resp(200, {"token": "new", "success": True})),
        ("/api/Position/searchOpen", Resp(200, {"positions": [], "success": True})),
    ])
    assert c.search_open_positions(1) == []
    assert s.sent[-1][2]["Authorization"] == "Bearer new"


def test_order_payload_with_brackets():
    c, s = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "t", "success": True})),
        ("/api/Order/place", Resp(200, {"orderId": 55, "success": True})),
    ])
    oid = c.place_order(1, "CON.F.US.MNQ.Z26", 2, 0, 3, custom_tag="x",
                        stop_loss_bracket={"ticks": -40, "type": 4},
                        take_profit_bracket={"ticks": 80, "type": 1})
    assert oid == 55
    body = s.sent[1][1]
    assert body["type"] == 2 and body["side"] == 0 and body["size"] == 3
    assert body["stopLossBracket"] == {"ticks": -40, "type": 4}
    assert body["takeProfitBracket"] == {"ticks": 80, "type": 1}


def test_bars_parsed_and_sorted():
    c, s = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "t", "success": True})),
        ("/api/History/retrieveBars", Resp(200, {"success": True, "bars": [
            {"t": "2026-01-06T15:01:00+00:00", "o": 2, "h": 3, "l": 1, "c": 2.5, "v": 10},
            {"t": "2026-01-06T15:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 5},
        ]})),
    ])
    bars = c.retrieve_bars("CON", datetime(2026, 1, 6, tzinfo=timezone.utc), datetime(2026, 1, 7, tzinfo=timezone.utc))
    assert [b.close for b in bars] == [1.5, 2.5]
    body = s.sent[1][1]
    assert body["unit"] == 2 and body["unitNumber"] == 1 and body["startTime"].endswith("Z")


def test_resolve_contract_prefers_active_exact_root():
    c, _ = client([
        ("/api/Auth/loginKey", Resp(200, {"token": "t", "success": True})),
        ("/api/Contract/search", Resp(200, {"success": True, "contracts": [
            {"id": "CON.F.US.MNQE.H26", "symbolId": "F.US.MNQE", "activeContract": True},
            {"id": "CON.F.US.MNQ.Z25", "symbolId": "F.US.MNQ", "activeContract": False},
            {"id": "CON.F.US.MNQ.H26", "symbolId": "F.US.MNQ", "activeContract": True},
        ]})),
    ])
    assert c.resolve_contract("MNQ")["id"] == "CON.F.US.MNQ.H26"


def test_parse_dt_handles_long_fractions():
    dt = _parse_dt("2026-01-06T15:00:00.1234567+00:00")
    assert dt.tzinfo is not None and dt.microsecond == 123456
