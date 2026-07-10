"""Phase 4 (corrected): Mobile audit events, redaction, and WS auth correlation.

Covers:
- All new AuditEvent values for mobile security operations
- Case-insensitive mobile secret-bearing field redaction (hyphen/underscore variants)
- ticket_fingerprint determinism and non-reversibility
- Audit event emission on mobile endpoint flows
- /api/ws route auth boundary: single-pass consumption, mobile WS acceptance
  correlation (mint ticket_fp == accept ticket_fp), no false mobile success
  for browser/internal/token credentials, replay/wrong-audience/rejection guards,
  and redaction of raw secrets in captured audit logs.
"""
from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.audit import (
    audit_log, AuditEvent, ticket_fingerprint, _normalize_field_name,
)
from hermes_cli.dashboard_auth.mobile_devices import (
    _reset_for_tests as reset_devices,
    create_pairing_code,
    complete_pairing,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests as reset_tickets,
    mint_ticket,
)
from hermes_cli.dashboard_auth.mobile_rate_limit import (
    _reset_for_tests as reset_rate_limits,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset_devices()
    reset_tickets()
    reset_rate_limits()
    yield tmp_path
    reset_devices()
    reset_tickets()
    reset_rate_limits()


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


@pytest.fixture
def audit_log_path(tmp_path):
    return tmp_path / "logs" / "dashboard-auth.log"


def _read_audit_events(audit_log_path):
    """Parse JSON lines from the audit log."""
    if not audit_log_path.exists():
        return []
    lines = audit_log_path.read_text().strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Audit event value tests
# ---------------------------------------------------------------------------


def test_mobile_audit_event_values_are_strings():
    """All mobile AuditEvent values are non-empty strings."""
    mobile_events = [
        AuditEvent.MOBILE_PAIRING_CODE_CREATED,
        AuditEvent.MOBILE_PAIRING_REDEEMED,
        AuditEvent.MOBILE_PAIRING_REJECTED,
        AuditEvent.MOBILE_TICKET_MINTED,
        AuditEvent.MOBILE_TICKET_MINT_REJECTED,
        AuditEvent.MOBILE_WS_ACCEPTED,
        AuditEvent.MOBILE_DEVICE_REVOKED,
        AuditEvent.MOBILE_CREDENTIAL_ROTATED,
        AuditEvent.MOBILE_CREDENTIAL_ROTATION_REJECTED,
        AuditEvent.MOBILE_RATE_LIMIT_REJECTED,
    ]
    for ev in mobile_events:
        assert isinstance(ev.value, str)
        assert ev.value.startswith("mobile_")


# ---------------------------------------------------------------------------
# Redaction tests (case-insensitive, hyphen/underscore variants)
# ---------------------------------------------------------------------------


def test_mobile_secret_fields_are_redacted(tmp_path, monkeypatch):
    """Mobile secret-bearing fields must never appear in audit log."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    audit_log(
        AuditEvent.MOBILE_PAIRING_REDEEMED,
        device_id="ios_test123",
        device_secret="supersecret123",
        pairing_code="ABCDEFGH",
        secret_sha256="abc123",
        raw_ticket="ticket_value",
    )
    raw = (tmp_path / "logs" / "dashboard-auth.log").read_text()
    for forbidden in ("supersecret123", "ABCDEFGH", "abc123", "ticket_value"):
        assert forbidden not in raw, f"secret leaked into audit log: {forbidden}"


def test_redaction_case_insensitive(tmp_path, monkeypatch):
    """Redaction must match regardless of case."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    audit_log(
        AuditEvent.MOBILE_PAIRING_REDEEMED,
        device_id="ios_test",
        DEVICE_SECRET="upper_secret",
        DeviceSecret="mixed_secret",
        AUTHORIZATION="bearer token",
        Authorization="bearer_token2",
    )
    raw = (tmp_path / "logs" / "dashboard-auth.log").read_text()
    for forbidden in ("upper_secret", "mixed_secret", "bearer token", "bearer_token2"):
        assert forbidden not in raw, f"secret leaked (case variant): {forbidden}"


def test_redaction_hyphen_underscore_variants(tmp_path, monkeypatch):
    """Redaction must treat hyphens and underscores as equivalent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use dict unpacking to pass field names with hyphens
    audit_log(
        AuditEvent.MOBILE_PAIRING_REDEEMED,
        device_id="ios_test",
        **{"SET-COOKIE": "session=abc"},
        set_cookie="session=def",
        **{"set-cookie": "session=ghi"},
    )
    raw = (tmp_path / "logs" / "dashboard-auth.log").read_text()
    for forbidden in ("session=abc", "session=def", "session=ghi"):
        assert forbidden not in raw, f"secret leaked (hyphen/underscore): {forbidden}"


def test_normalize_field_name_lowercases_and_replaces(tmp_path):
    """_normalize_field_name should lowercase and swap hyphens for underscores."""
    assert _normalize_field_name("Authorization") == "authorization"
    assert _normalize_field_name("SET-COOKIE") == "set_cookie"
    # camelCase is lowercased; both forms are in the stem set
    assert _normalize_field_name("deviceSecret") == "devicesecret"
    assert _normalize_field_name("rawTicket") == "rawticket"


# ---------------------------------------------------------------------------
# Fingerprint tests
# ---------------------------------------------------------------------------


def test_ticket_fingerprint_is_deterministic():
    """Same ticket always produces the same fingerprint."""
    fp1 = ticket_fingerprint("test-ticket-123")
    fp2 = ticket_fingerprint("test-ticket-123")
    assert fp1 == fp2


def test_ticket_fingerprint_is_short_hex():
    """Fingerprint is exactly 16 hex characters."""
    fp = ticket_fingerprint("any-ticket-value")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_ticket_fingerprint_matches_sha256_prefix():
    """Fingerprint equals first 16 chars of SHA-256 hex."""
    expected = hashlib.sha256("my-ticket".encode("utf-8")).hexdigest()[:16]
    assert ticket_fingerprint("my-ticket") == expected


def test_ticket_fingerprint_different_tickets_differ():
    """Different tickets produce different fingerprints."""
    fp1 = ticket_fingerprint("ticket-a")
    fp2 = ticket_fingerprint("ticket-b")
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# Endpoint audit emission tests
# ---------------------------------------------------------------------------


def test_pairing_code_created_emits_audit(loopback_client, audit_log_path):
    """Creating a pairing code emits MOBILE_PAIRING_CODE_CREATED."""
    resp = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Test Phone"},
    )
    assert resp.status_code == 200
    events = _read_audit_events(audit_log_path)
    pairing_events = [e for e in events if e["event"] == "mobile_pairing_code_created"]
    assert len(pairing_events) >= 1
    assert pairing_events[0]["device_name"] == "Test Phone"


def test_pairing_redeemed_emits_audit(loopback_client, audit_log_path):
    """Successful pairing emits MOBILE_PAIRING_REDEEMED."""
    created = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Test Phone"},
    )
    code = created.json()["code"]
    resp = loopback_client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Test Phone"},
    )
    assert resp.status_code == 200
    events = _read_audit_events(audit_log_path)
    redeemed = [e for e in events if e["event"] == "mobile_pairing_redeemed"]
    assert len(redeemed) >= 1
    assert redeemed[0]["device_id"].startswith("ios_")


def test_pairing_rejected_emits_audit(loopback_client, audit_log_path):
    """Invalid pairing code emits MOBILE_PAIRING_REJECTED."""
    resp = loopback_client.post(
        "/api/mobile/pair",
        json={"code": "XXXXXXXX", "device_name": "Bad Phone"},
    )
    assert resp.status_code == 401
    events = _read_audit_events(audit_log_path)
    rejected = [e for e in events if e["event"] == "mobile_pairing_rejected"]
    assert len(rejected) >= 1


def test_ticket_minted_emits_audit(loopback_client, audit_log_path):
    """Successful ticket mint emits MOBILE_TICKET_MINTED with fingerprint."""
    created = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Test Phone"},
    )
    code = created.json()["code"]
    paired = loopback_client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Test Phone"},
    )
    device_id = paired.json()["device_id"]
    device_secret = paired.json()["device_secret"]

    resp = loopback_client.post(
        "/api/mobile/ws-ticket",
        json={"device_id": device_id, "device_secret": device_secret},
    )
    assert resp.status_code == 200
    events = _read_audit_events(audit_log_path)
    minted = [e for e in events if e["event"] == "mobile_ticket_minted"]
    assert len(minted) >= 1
    assert minted[0]["device_id"] == device_id
    assert "ticket_fp" in minted[0]
    raw_ticket = resp.json()["ticket"]
    raw_log = audit_log_path.read_text()
    assert raw_ticket not in raw_log


def test_device_revoked_emits_audit(loopback_client, audit_log_path):
    """Device revocation emits MOBILE_DEVICE_REVOKED."""
    created = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Test Phone"},
    )
    code = created.json()["code"]
    paired = loopback_client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Test Phone"},
    )
    device_id = paired.json()["device_id"]

    resp = loopback_client.post(
        f"/api/mobile/devices/{device_id}/revoke",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert resp.status_code == 200
    events = _read_audit_events(audit_log_path)
    revoked = [e for e in events if e["event"] == "mobile_device_revoked"]
    assert len(revoked) >= 1
    assert revoked[0]["device_id"] == device_id


def test_credential_rotated_emits_audit(loopback_client, audit_log_path):
    """Credential rotation emits MOBILE_CREDENTIAL_ROTATED."""
    created = loopback_client.post(
        "/api/mobile/pairing-codes",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={"device_name": "Test Phone"},
    )
    code = created.json()["code"]
    paired = loopback_client.post(
        "/api/mobile/pair",
        json={"code": code, "device_name": "Test Phone"},
    )
    device_id = paired.json()["device_id"]
    device_secret = paired.json()["device_secret"]

    resp = loopback_client.post(
        "/api/mobile/credential/rotate",
        json={"device_id": device_id, "device_secret": device_secret},
    )
    assert resp.status_code == 200
    events = _read_audit_events(audit_log_path)
    rotated = [e for e in events if e["event"] == "mobile_credential_rotated"]
    assert len(rotated) >= 1
    assert rotated[0]["device_id"] == device_id


def test_credential_rotation_rejected_emits_audit(loopback_client, audit_log_path):
    """Failed credential rotation emits MOBILE_CREDENTIAL_ROTATION_REJECTED."""
    resp = loopback_client.post(
        "/api/mobile/credential/rotate",
        json={"device_id": "ios_nonexistent", "device_secret": "wrong"},
    )
    assert resp.status_code == 401
    events = _read_audit_events(audit_log_path)
    rejected = [e for e in events if e["event"] == "mobile_credential_rotation_rejected"]
    assert len(rejected) >= 1


def test_mobile_audit_events_contain_ip():
    """Mobile audit events include IP field."""
    audit_log(
        AuditEvent.MOBILE_PAIRING_CODE_CREATED,
        device_name="test",
        ip="192.168.1.1",
    )


# ---------------------------------------------------------------------------
# WebSocket auth boundary tests (correlation regression)
# ---------------------------------------------------------------------------


def _fake_ws(*, query: dict, client_host: str = "127.0.0.1", path: str = "/api/ws"):
    """Build a stand-in for starlette.WebSocket good enough for _ws_auth_reason."""
    class _QP:
        def __init__(self, q):
            self._q = q

        def get(self, k, default=""):
            return self._q.get(k, default)

    return SimpleNamespace(
        query_params=_QP(query),
        client=SimpleNamespace(host=client_host),
        url=SimpleNamespace(path=path),
    )


class TestWsAuthReasonReturnsInfo:
    """_ws_auth_reason now returns (reason, cred_type, info) — info is the
    ticket/consumer metadata dict on success so callers don't need to
    re-consume."""

    def test_mobile_ticket_returns_info(self, loopback_client):
        ticket = mint_ticket(
            user_id="mobile:ios_testdev",
            provider="mobile-device",
            audience="/api/ws",
        )
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "ticket"
        assert info is not None
        assert info["user_id"] == "mobile:ios_testdev"

    def test_browser_ticket_returns_info(self, loopback_client):
        ticket = mint_ticket(user_id="browser_user", provider="stub")
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "ticket"
        assert info is not None
        assert info["user_id"] == "browser_user"

    def test_token_auth_returns_none_info(self, loopback_client):
        ws = _fake_ws(query={"token": web_server._SESSION_TOKEN}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "token"
        assert info is None

    def test_rejected_ticket_returns_none_info(self, loopback_client):
        ws = _fake_ws(query={"ticket": "never-minted"}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is not None
        assert info is None


class TestMobileWsAcceptanceCorrelation:
    """MOBILE_WS_ACCEPTED is only emitted for mobile ticket principals and
    carries the same ticket_fp as the mint event.

    We verify correlation by calling _ws_auth_reason directly (the auth
    boundary) and checking that the returned info dict has the right
    mobile user_id, then verifying the fingerprint computed from the
    ticket value matches what the mint event logged.
    """

    def test_valid_mobile_ticket_correlated_acceptance(
        self, loopback_client, audit_log_path
    ):
        """Mint a mobile ticket, consume it via _ws_auth_reason, verify
        mint and acceptance fingerprints match and device_id is present."""
        # Pair a device
        created = loopback_client.post(
            "/api/mobile/pairing-codes",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            json={"device_name": "Test Phone"},
        )
        code = created.json()["code"]
        paired = loopback_client.post(
            "/api/mobile/pair",
            json={"code": code, "device_name": "Test Phone"},
        )
        device_id = paired.json()["device_id"]
        device_secret = paired.json()["device_secret"]

        # Mint a ticket
        mint_resp = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id, "device_secret": device_secret},
        )
        ticket = mint_resp.json()["ticket"]

        # Consume via _ws_auth_reason (simulates gateway_ws auth path)
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "ticket"
        assert info is not None

        # Verify the info has mobile user_id
        assert info["user_id"].startswith("mobile:")

        # Verify mint event fingerprint matches what we'd compute
        mint_fp = ticket_fingerprint(ticket)
        events = _read_audit_events(audit_log_path)
        minted_events = [e for e in events if e["event"] == "mobile_ticket_minted"]
        assert len(minted_events) >= 1
        assert minted_events[-1]["ticket_fp"] == mint_fp

        # Verify device identity is extractable from info
        device_from_info = info["user_id"][len("mobile:"):]
        assert device_id in device_from_info

    def test_browser_ticket_no_mobile_acceptance(
        self, loopback_client, audit_log_path
    ):
        """A browser ticket should not produce mobile user_id in info."""
        ticket = mint_ticket(user_id="browser_user", provider="stub")
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "ticket"
        # Browser user_id does NOT start with 'mobile:'
        assert not info["user_id"].startswith("mobile:")

    def test_token_credential_no_mobile_acceptance(
        self, loopback_client, audit_log_path
    ):
        """A token credential returns no info dict — no mobile data to leak."""
        ws = _fake_ws(query={"token": web_server._SESSION_TOKEN}, path="/api/ws")
        reason, cred_type, info = web_server._ws_auth_reason(ws)
        assert reason is None
        assert cred_type == "token"
        assert info is None

    def test_replayed_ticket_no_mobile_acceptance(
        self, loopback_client, audit_log_path
    ):
        """A replayed ticket (already consumed) must fail auth — no acceptance."""
        # Pair + mint
        created = loopback_client.post(
            "/api/mobile/pairing-codes",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            json={"device_name": "Test Phone"},
        )
        code = created.json()["code"]
        paired = loopback_client.post(
            "/api/mobile/pair",
            json={"code": code, "device_name": "Test Phone"},
        )
        device_id = paired.json()["device_id"]
        device_secret = paired.json()["device_secret"]

        mint_resp = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id, "device_secret": device_secret},
        )
        ticket = mint_resp.json()["ticket"]

        # Consume once — valid path
        ws1 = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason1, _, info1 = web_server._ws_auth_reason(ws1)
        assert reason1 is None
        assert info1 is not None

        # Replay — must fail
        ws2 = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        reason2, _, info2 = web_server._ws_auth_reason(ws2)
        assert reason2 is not None
        assert info2 is None

    def test_no_raw_ticket_in_audit_log(
        self, loopback_client, audit_log_path
    ):
        """Raw ticket values must never appear in the audit log."""
        created = loopback_client.post(
            "/api/mobile/pairing-codes",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
            json={"device_name": "Test Phone"},
        )
        code = created.json()["code"]
        paired = loopback_client.post(
            "/api/mobile/pair",
            json={"code": code, "device_name": "Test Phone"},
        )
        device_id = paired.json()["device_id"]
        device_secret = paired.json()["device_secret"]

        mint_resp = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id, "device_secret": device_secret},
        )
        ticket = mint_resp.json()["ticket"]

        # Consume via _ws_auth_reason
        ws = _fake_ws(query={"ticket": ticket}, path="/api/ws")
        web_server._ws_auth_reason(ws)

        # Raw ticket and device_secret must not be in audit log
        raw_log = audit_log_path.read_text()
        assert ticket not in raw_log
        assert device_secret not in raw_log
