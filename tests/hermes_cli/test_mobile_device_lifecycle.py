"""Phase 2: Mobile device lifecycle controls — list, revoke, rotate, ticket purge.

Covers the 11 acceptance criteria from VEGA_PHASE2.md:
1. Safe list DTO excludes secret/hash fields
2. List/revoke routes require dashboard auth in loopback mode
3. Revoke persists across reopen/restart and is idempotent
4. Revoked credential cannot mint a new ticket
5. Ticket minted before revoke is purged/rejected
6. Rotation rejects wrong current secret
7. Rotation increments version, invalidates old secret, accepts new secret
8. Ticket minted before rotation is purged/rejected
9. Pair and rotate responses have both no-cache headers
10. Store corruption returns sanitized 503 on all affected HTTP paths
11. Browser/internal ticket behavior remains unchanged
12. List/revoke routes require dashboard auth in gated/OAuth mode
13. Gated/OAuth authenticated session can list and revoke devices
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.mobile_devices import (
    DeviceAuthInvalid,
    DeviceStoreCorrupt,
    _db_path,
    _reset_for_tests,
    complete_pairing,
    create_pairing_code,
    verify_device,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests as reset_tickets,
    mint_ticket,
)
from hermes_cli.dashboard_auth import ws_tickets


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


def _fake_ws(*, query: dict, client_host: str = "127.0.0.1", path: str = "/api/ws"):
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


# ============================================================================
# 1. Safe list DTO excludes secret/hash fields
# ============================================================================

class TestSafeListDto:
    def test_list_response_has_no_secret_fields(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = loopback_client.get(
            "/api/mobile/devices",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 200
        devices = r.json()
        assert len(devices) == 1
        dev = devices[0]
        # Safe fields present
        assert "device_id" in dev
        assert "device_name" in dev
        assert "created_at" in dev
        assert "credential_version" in dev
        # Secret/hash fields MUST NOT be present
        assert "secret_sha256" not in dev
        assert "secret" not in dev
        assert dev["credential_version"] == 1

    def test_list_response_has_revoked_fields(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = loopback_client.get(
            "/api/mobile/devices",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 200
        dev = r.json()[0]
        assert dev["revoked_at"] is None
        assert dev["device_id"] == credential.device_id


# ============================================================================
# 2. List/revoke routes require dashboard auth in loopback mode
# ============================================================================

class TestAuthRequired:
    def test_list_requires_auth(self, loopback_client):
        r = loopback_client.get("/api/mobile/devices")
        assert r.status_code == 401

    def test_revoke_requires_auth(self, loopback_client):
        r = loopback_client.post("/api/mobile/devices/ios_x/revoke")
        assert r.status_code == 401


# ============================================================================
# 3. Revoke persists across reopen/restart and is idempotent
# ============================================================================

class TestRevokePersistence:
    def test_revoke_persists_and_is_idempotent(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # First revoke
        r = loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["revoked"] is True

        # Second revoke (idempotent)
        r2 = loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r2.status_code == 200

        # Verify via list that revoked_at is set
        r3 = loopback_client.get(
            "/api/mobile/devices",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        dev = r3.json()[0]
        assert dev["revoked_at"] is not None

        # Verify the credential is actually revoked (persisted)
        with pytest.raises(DeviceAuthInvalid, match="disabled"):
            verify_device(
                device_id=credential.device_id,
                device_secret=credential.device_secret,
            )

    def test_revoke_unknown_device_returns_404(self, loopback_client):
        r = loopback_client.post(
            "/api/mobile/devices/ios_nonexistent/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 404

    def test_revoke_persists_after_reset_pairing_codes(self, loopback_client, tmp_path):
        """Simulate restart: pairing codes cleared, DB persists."""
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Revoke
        loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )

        # Simulate restart: clear in-memory state
        from hermes_cli.dashboard_auth import mobile_devices as md
        with md._lock:
            md._pairing_codes.clear()

        # Credential still revoked after restart
        with pytest.raises(DeviceAuthInvalid, match="disabled"):
            verify_device(
                device_id=credential.device_id,
                device_secret=credential.device_secret,
            )


# ============================================================================
# 4. Revoked credential cannot mint a new ticket
# ============================================================================

class TestRevokedCannotMintTicket:
    def test_revoked_device_cannot_mint_ticket(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Revoke
        loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )

        # Mint ticket should fail
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert r.status_code == 401


# ============================================================================
# 5. Ticket minted before revoke is purged/rejected
# ============================================================================

class TestTicketPurgedAfterRevoke:
    def test_existing_ticket_purged_after_revoke(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Mint a ticket BEFORE revoke
        ticket_r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert ticket_r.status_code == 200
        ticket = ticket_r.json()["ticket"]

        # Revoke
        loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )

        # The ticket must be purged — ws_auth_ok must reject it
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        assert web_server._ws_auth_ok(ws) is False


# ============================================================================
# 6. Rotation rejects wrong current secret
# ============================================================================

class TestRotationRejectsWrongSecret:
    def test_rotate_wrong_secret_fails(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": "wrong-secret",
            },
        )
        assert r.status_code == 401


# ============================================================================
# 7. Rotation increments version, invalidates old, accepts new
# ============================================================================

class TestRotationAtomicity:
    def test_rotate_increments_version_and_works(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["device_id"] == credential.device_id
        assert body["device_secret"] != credential.device_secret

        # Old secret fails
        with pytest.raises(DeviceAuthInvalid):
            verify_device(
                device_id=credential.device_id,
                device_secret=credential.device_secret,
            )

        # New secret works
        new_principal = verify_device(
            device_id=credential.device_id,
            device_secret=body["device_secret"],
        )
        assert new_principal.device_id == credential.device_id

        # Version incremented
        r2 = loopback_client.get(
            "/api/mobile/devices",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        dev = r2.json()[0]
        assert dev["credential_version"] == 2


# ============================================================================
# 8. Ticket minted before rotation is purged/rejected
# ============================================================================

class TestTicketPurgedAfterRotate:
    def test_existing_ticket_purged_after_rotate(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Mint a ticket BEFORE rotate
        ticket_r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert ticket_r.status_code == 200
        ticket = ticket_r.json()["ticket"]

        # Rotate
        loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )

        # The ticket must be purged
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        assert web_server._ws_auth_ok(ws) is False


# ============================================================================
# 9. Pair and rotate responses have no-cache headers
# ============================================================================

class TestNoCacheHeaders:
    def test_pair_response_has_no_cache_headers(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": pairing.code, "device_name": "test"},
        )
        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "no-store"
        assert r.headers.get("Pragma") == "no-cache"

    def test_rotate_response_has_no_cache_headers(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )
        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "no-store"
        assert r.headers.get("Pragma") == "no-cache"


# ============================================================================
# 10. Store corruption returns sanitized 503 on all affected HTTP paths
# ============================================================================

class TestStoreCorruptionReturns503:
    def _corrupt_db(self, tmp_path):
        """Write a corrupt DB so the next store access fails."""
        db_path = tmp_path / "dashboard" / "mobile-devices.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove any existing DB
        if db_path.exists():
            db_path.unlink()
        # Write corrupt bytes
        db_path.write_bytes(b"NOT A SQLITE DATABASE")

    def test_list_returns_503_on_corrupt_store(self, loopback_client, tmp_path):
        self._corrupt_db(tmp_path)
        r = loopback_client.get(
            "/api/mobile/devices",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 503

    def test_revoke_returns_503_on_corrupt_store(self, loopback_client, tmp_path):
        self._corrupt_db(tmp_path)
        r = loopback_client.post(
            "/api/mobile/devices/ios_any/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert r.status_code == 503

    def test_ticket_returns_503_on_corrupt_store(self, loopback_client, tmp_path):
        self._corrupt_db(tmp_path)
        r = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": "ios_any", "device_secret": "x"},
        )
        assert r.status_code == 503

    def test_pair_returns_503_on_corrupt_store(self, loopback_client, tmp_path):
        pairing = create_pairing_code(device_name="test")
        self._corrupt_db(tmp_path)
        r = loopback_client.post(
            "/api/mobile/pair",
            json={"code": pairing.code, "device_name": "test"},
        )
        assert r.status_code == 503

    def test_rotate_returns_503_on_corrupt_store(self, loopback_client, tmp_path):
        self._corrupt_db(tmp_path)
        r = loopback_client.post(
            "/api/mobile/credential/rotate",
            json={"device_id": "ios_any", "device_secret": "x"},
        )
        assert r.status_code == 503


# ============================================================================
# 11. Browser/internal ticket behavior remains unchanged
# ============================================================================

class TestBrowserTicketUnchanged:
    def test_browser_ticket_survives_mobile_revoke(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Mint a browser ticket for a different user
        browser_ticket = mint_ticket(user_id="browser_user", provider="nous")

        # Revoke the mobile device
        loopback_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )

        # Browser ticket must still work
        ws = _fake_ws(query={"ticket": browser_ticket})
        assert web_server._ws_auth_ok(ws) is True

    def test_browser_ticket_survives_mobile_rotate(self, loopback_client):
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        # Mint a browser ticket
        browser_ticket = mint_ticket(user_id="browser_user", provider="nous")

        # Rotate the mobile device
        loopback_client.post(
            "/api/mobile/credential/rotate",
            json={
                "device_id": credential.device_id,
                "device_secret": credential.device_secret,
            },
        )

        # Browser ticket must still work
        ws = _fake_ws(query={"ticket": browser_ticket})
        assert web_server._ws_auth_ok(ws) is True


# ============================================================================
# 12. List/revoke routes require dashboard auth in gated/OAuth mode
# ============================================================================

@pytest.fixture
def gated_client():
    """Gated/OAuth mode client with StubAuthProvider."""
    from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def _complete_stub_login(client) -> None:
    """Walk the stub OAuth round trip so client carries a valid session."""
    r1 = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert r1.status_code == 302
    state = r1.headers["location"].split("state=")[1]
    r2 = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert r2.status_code == 302


class TestGatedAuthDeviceList:
    """Device list and revoke require gated auth; authenticated sessions succeed."""

    def test_gated_list_unauthenticated_returns_401(self, gated_client):
        """Unauthenticated list in gated mode must 401, not 200."""
        pairing = create_pairing_code(device_name="test")
        complete_pairing(code=pairing.code)
        r = gated_client.get("/api/mobile/devices")
        assert r.status_code == 401

    def test_gated_revoke_unauthenticated_returns_401(self, gated_client):
        """Unauthenticated revoke in gated mode must 401, not 200."""
        r = gated_client.post("/api/mobile/devices/ios_x/revoke")
        assert r.status_code == 401

    def test_gated_list_with_valid_session_succeeds(self, gated_client):
        """Authenticated session in gated mode can list devices."""
        _complete_stub_login(gated_client)
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = gated_client.get("/api/mobile/devices")
        assert r.status_code == 200
        devices = r.json()
        assert len(devices) >= 1
        device_ids = [d["device_id"] for d in devices]
        assert credential.device_id in device_ids

    def test_gated_revoke_with_valid_session_succeeds(self, gated_client):
        """Authenticated session in gated mode can revoke devices."""
        _complete_stub_login(gated_client)
        pairing = create_pairing_code(device_name="test")
        credential = complete_pairing(code=pairing.code)

        r = gated_client.post(
            f"/api/mobile/devices/{credential.device_id}/revoke",
        )
        assert r.status_code == 200
        assert r.json()["revoked"] is True

        # Verify it's actually revoked
        r2 = gated_client.get("/api/mobile/devices")
        assert r2.status_code == 200
        dev = [d for d in r2.json() if d["device_id"] == credential.device_id][0]
        assert dev["revoked_at"] is not None
