"""Phase 5: real ``/api/ws`` route integration for paired-device auth.

Every WebSocket assertion enters FastAPI/Starlette's actual route with
``TestClient.websocket_connect``.  Only the downstream TUI gateway handler is
replaced, after authentication and Host/Origin/peer checks have run.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.mobile_devices import _reset_for_tests as reset_devices
from hermes_cli.dashboard_auth.mobile_rate_limit import _reset_for_tests as reset_rate_limits
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests as reset_tickets,
    internal_ws_credential,
    mint_ticket,
)

_ADMIN_HEADERS = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}
_WS_HEADERS = {
    "host": "127.0.0.1:9119",
    "origin": "http://127.0.0.1:9119",
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_providers()
    reset_devices()
    reset_tickets()
    reset_rate_limits()
    previous = {
        "bound_host": getattr(web_server.app.state, "bound_host", None),
        "bound_port": getattr(web_server.app.state, "bound_port", None),
        "auth_required": getattr(web_server.app.state, "auth_required", None),
    }
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    yield
    web_server.app.state.bound_host = previous["bound_host"]
    web_server.app.state.bound_port = previous["bound_port"]
    web_server.app.state.auth_required = previous["auth_required"]
    reset_devices()
    reset_tickets()
    reset_rate_limits()


@pytest.fixture
def client():
    return TestClient(web_server.app, base_url="http://127.0.0.1:9119")


@pytest.fixture
def audit_log_path(tmp_path):
    return tmp_path / "logs" / "dashboard-auth.log"


async def _narrow_handle_ws(ws):
    """Prove the real route reached its downstream handler, then close."""
    await ws.accept()
    await ws.send_json({"route": "reached"})
    await ws.close()


@contextmanager
def _connect(client: TestClient, path: str, *, headers=None):
    with patch("tui_gateway.ws.handle_ws", new=_narrow_handle_ws):
        with client.websocket_connect(path, headers=headers or _WS_HEADERS) as socket:
            assert socket.receive_json() == {"route": "reached"}
            yield socket


def _assert_rejected(client: TestClient, path: str, code: int, *, headers=None):
    with patch("tui_gateway.ws.handle_ws", new=_narrow_handle_ws):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(path, headers=headers or _WS_HEADERS):
                pass
    assert exc_info.value.code == code


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _pair(client: TestClient):
    created = client.post(
        "/api/mobile/pairing-codes",
        headers=_ADMIN_HEADERS,
        json={"device_name": "Phase 5 Phone"},
    )
    assert created.status_code == 200, created.text
    code = created.json()["code"]
    paired = client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Phase 5 Phone"},
    )
    assert paired.status_code == 200, paired.text
    return {"code": code, **paired.json()}


def _mint(client: TestClient, credential):
    response = client.post(
        "/api/mobile/ws-ticket",
        json={
            "device_id": credential["device_id"],
            "device_secret": credential["device_secret"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def _mobile_accepts(path):
    return [event for event in _events(path) if event.get("event") == "mobile_ws_accepted"]


def test_mobile_ticket_traverses_real_route_and_correlates_audit(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)

    with _connect(client, f"/api/ws?ticket={ticket}"):
        pass

    events = _events(audit_log_path)
    minted = [event for event in events if event.get("event") == "mobile_ticket_minted"]
    accepted = _mobile_accepts(audit_log_path)
    assert len(minted) == 1
    assert len(accepted) == 1
    assert minted[0]["ticket_fp"] == accepted[0]["ticket_fp"]
    assert accepted[0]["device_id"] == credential["device_id"]
    assert accepted[0]["user_id"] == f"mobile:{credential['device_id']}"

    raw_log = audit_log_path.read_text()
    for secret in (credential["code"], credential["device_secret"], ticket):
        assert secret not in raw_log


def test_replayed_ticket_is_rejected_by_real_route(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)
    with _connect(client, f"/api/ws?ticket={ticket}"):
        pass
    _assert_rejected(client, f"/api/ws?ticket={ticket}", 4401)
    assert len(_mobile_accepts(audit_log_path)) == 1


def test_wrong_audience_ticket_is_rejected_by_real_route(client, audit_log_path):
    credential = _pair(client)
    ticket = mint_ticket(
        user_id=f"mobile:{credential['device_id']}",
        provider="mobile-device",
        audience="/api/console",
    )
    _assert_rejected(client, f"/api/ws?ticket={ticket}", 4401)
    assert _mobile_accepts(audit_log_path) == []


def test_revoke_invalidates_outstanding_ticket_at_real_route(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)
    revoked = client.post(
        f"/api/mobile/devices/{credential['device_id']}/revoke",
        headers=_ADMIN_HEADERS,
    )
    assert revoked.status_code == 200, revoked.text
    _assert_rejected(client, f"/api/ws?ticket={ticket}", 4401)
    assert _mobile_accepts(audit_log_path) == []


def test_rotation_invalidates_outstanding_ticket_at_real_route(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)
    rotated = client.post(
        "/api/mobile/credential/rotate",
        json={
            "device_id": credential["device_id"],
            "device_secret": credential["device_secret"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    _assert_rejected(client, f"/api/ws?ticket={ticket}", 4401)
    assert _mobile_accepts(audit_log_path) == []


def test_browser_ticket_reaches_route_without_mobile_accept_event(client, audit_log_path):
    ticket = mint_ticket(user_id="browser-user", provider="browser", audience="/api/ws")
    with _connect(client, f"/api/ws?ticket={ticket}"):
        pass
    assert _mobile_accepts(audit_log_path) == []


def test_legacy_loopback_token_reaches_route_without_mobile_event(client, audit_log_path):
    with _connect(client, f"/api/ws?token={web_server._SESSION_TOKEN}"):
        pass
    assert _mobile_accepts(audit_log_path) == []


def test_gated_mode_accepts_mobile_ticket_and_internal_credential(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)
    internal = internal_ws_credential()
    web_server.app.state.auth_required = True

    with _connect(client, f"/api/ws?ticket={ticket}"):
        pass
    with _connect(client, f"/api/ws?internal={internal}"):
        pass

    accepted = _mobile_accepts(audit_log_path)
    assert len(accepted) == 1
    assert accepted[0]["device_id"] == credential["device_id"]


def test_gated_mode_rejects_legacy_token_and_missing_credential(client):
    web_server.app.state.auth_required = True
    _assert_rejected(client, f"/api/ws?token={web_server._SESSION_TOKEN}", 4401)
    _assert_rejected(client, "/api/ws", 4401)


def test_host_origin_guard_is_traversed_after_ticket_auth(client, audit_log_path):
    credential = _pair(client)
    ticket = _mint(client, credential)
    _assert_rejected(
        client,
        f"/api/ws?ticket={ticket}",
        4403,
        headers={"host": "127.0.0.1:9119", "origin": "https://evil.example"},
    )
    assert _mobile_accepts(audit_log_path) == []


def test_embedded_chat_feature_gate_rejects_before_auth(client, monkeypatch):
    credential = _pair(client)
    ticket = _mint(client, credential)
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", False)
    _assert_rejected(client, f"/api/ws?ticket={ticket}", 4403)
