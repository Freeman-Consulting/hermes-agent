from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.mobile_devices import (
    DeviceAuthInvalid,
    PairingCodeInvalid,
    _reset_for_tests,
    complete_pairing,
    create_pairing_code,
    verify_device,
)
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests as reset_tickets


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _reset_for_tests()
    reset_tickets()
    yield tmp_path
    _reset_for_tests()
    reset_tickets()


@pytest.fixture
def loopback_client():
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:9119")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def test_pairing_round_trip_persists_only_secret_hash(tmp_path):
    pairing = create_pairing_code(device_name="Angry iPhone")
    credential = complete_pairing(code=pairing.code, device_name="Angry iPhone")

    assert credential.device_id.startswith("ios_")
    assert len(credential.device_secret) >= 32
    principal = verify_device(
        device_id=credential.device_id,
        device_secret=credential.device_secret,
    )
    assert principal.user_id == f"mobile:{credential.device_id}"
    assert principal.provider == "mobile-device"

    store_path = tmp_path / "dashboard" / "mobile-devices.json"
    raw = store_path.read_text(encoding="utf-8")
    assert credential.device_secret not in raw
    data = json.loads(raw)
    assert data["devices"][0]["secret_sha256"]


def test_pairing_code_is_single_use():
    pairing = create_pairing_code(device_name="phone")
    complete_pairing(code=pairing.code)
    with pytest.raises(PairingCodeInvalid):
        complete_pairing(code=pairing.code)


def test_verify_rejects_wrong_secret():
    pairing = create_pairing_code(device_name="phone")
    credential = complete_pairing(code=pairing.code)
    with pytest.raises(DeviceAuthInvalid):
        verify_device(device_id=credential.device_id, device_secret="wrong")


def test_mobile_pairing_http_flow_mints_api_ws_ticket(loopback_client):
    unauth = loopback_client.post(
        "/api/mobile/pairing-codes",
        json={"device_name": "Angry iPhone"},
    )
    assert unauth.status_code == 401

    created = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Angry iPhone"},
    )
    assert created.status_code == 200
    code = created.json()["code"]

    paired = loopback_client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Angry iPhone"},
    )
    assert paired.status_code == 200
    pair_body = paired.json()
    assert pair_body["device_id"].startswith("ios_")
    assert len(pair_body["device_secret"]) >= 32

    ticket_response = loopback_client.post(
        "/api/mobile/ws-ticket",
        json={
            "device_id": pair_body["device_id"],
            "device_secret": pair_body["device_secret"],
        },
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]
    assert ticket_response.json()["ttl_seconds"] == 30

    ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
    assert web_server._ws_auth_ok(ws) is True


def test_mobile_ws_ticket_rejects_bad_device_credential(loopback_client):
    response = loopback_client.post(
        "/api/mobile/ws-ticket",
        json={"device_id": "ios_missing", "device_secret": "wrong"},
    )
    assert response.status_code == 401


def _fake_ws(*, query: dict, client_host: str = "127.0.0.1", path: str = "/api/ws"):
    from types import SimpleNamespace

    class _QP:
        def __init__(self, q):
            self._q = q

        def get(self, key, default=""):
            return self._q.get(key, default)

    return SimpleNamespace(
        query_params=_QP(query),
        client=SimpleNamespace(host=client_host),
        url=SimpleNamespace(path=path),
    )
