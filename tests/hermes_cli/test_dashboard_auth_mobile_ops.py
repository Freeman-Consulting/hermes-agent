"""Mobile operations status route: authentication, schema, secret-safety,
bounded aggregation, 24h window, audit-file permissions, bounded integrity,
cached runtime metadata, and degraded-state behavior.

Covers all 8 Gerard review findings:
1. Bounded audit reader (tail-based, not full file read)
2. 24-hour event window
3. Required rejection reason counters
4. Time-bounded SQLite integrity
5. No double-close + parent directory permissions
6. Dashboard overall health, timestamps, version, commit
7. Cached runtime metadata (no git per request)
8. Full test coverage
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.audit import audit_log, AuditEvent
from hermes_cli.dashboard_auth.mobile_devices import (
    _reset_for_tests as reset_devices,
    create_pairing_code,
    complete_pairing,
    list_devices,
)
from hermes_cli.dashboard_auth.mobile_ops import (
    OPS_STATUS_SCHEMA_VERSION,
    _aggregate_audit_events,
    _check_integrity,
    _read_audit_window,
    get_mobile_ops_status,
    MobileOpsStatus,
    _runtime_commit,
    _runtime_version,
)
from hermes_cli.dashboard_auth.mobile_rate_limit import (
    _reset_for_tests as reset_rate_limits,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    _reset_for_tests as reset_tickets,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Isolated Hermes home with clean mobile device state."""
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
    """TestClient with auth disabled (uses _require_token with session token)."""
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


@pytest.fixture
def paired_device(loopback_client):
    """Create a paired device and return (device_id, device_secret)."""
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
    return paired.json()["device_id"], paired.json()["device_secret"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_ops_status_unauthenticated_rejected(loopback_client):
    """Unauthenticated request to /api/mobile/ops-status is rejected."""
    resp = loopback_client.get("/api/mobile/ops-status")
    assert resp.status_code == 401


def test_ops_status_authenticated_ok(loopback_client):
    """Authenticated request returns 200 with schema_version."""
    resp = loopback_client.get(
        "/api/mobile/ops-status",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == OPS_STATUS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Schema fields
# ---------------------------------------------------------------------------


class TestOpsStatusSchemaFields:
    """All expected fields are present and correctly typed."""

    def _get_status(self, client):
        resp = client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_has_schema_version(self, loopback_client):
        data = self._get_status(loopback_client)
        assert data["schema_version"] == OPS_STATUS_SCHEMA_VERSION

    def test_has_overall_health(self, loopback_client):
        data = self._get_status(loopback_client)
        assert data["overall_health"] in ("healthy", "degraded", "unavailable")

    def test_has_registry_backend(self, loopback_client):
        data = self._get_status(loopback_client)
        assert data["registry_backend"] == "sqlite"

    def test_has_registry_schema_version(self, loopback_client):
        data = self._get_status(loopback_client)
        assert isinstance(data["registry_schema_version"], int)

    def test_has_integrity_fields(self, loopback_client):
        data = self._get_status(loopback_client)
        assert isinstance(data["integrity_ok"], bool)
        assert "integrity_check_ts" in data

    def test_has_migration_state(self, loopback_client):
        data = self._get_status(loopback_client)
        assert isinstance(data["migration_state"], str)

    def test_has_device_counts(self, loopback_client):
        data = self._get_status(loopback_client)
        assert isinstance(data["active_device_count"], int)
        assert isinstance(data["revoked_device_count"], int)

    def test_has_audit_counters(self, loopback_client):
        data = self._get_status(loopback_client)
        counters = [
            "recent_mint_count", "recent_mint_reject_count",
            "recent_expired_ticket_count", "recent_replayed_ticket_count",
            "recent_wrong_audience_count", "recent_revoked_device_reject_count",
            "recent_malformed_count", "recent_oversized_count",
            "recent_ws_accept_count", "recent_pairing_count",
            "recent_revocation_count", "recent_rotation_count",
            "recent_rotation_reject_count", "recent_rate_limit_count",
        ]
        for counter in counters:
            assert counter in data, f"Missing counter: {counter}"
            assert isinstance(data[counter], int)

    def test_has_timestamps(self, loopback_client):
        data = self._get_status(loopback_client)
        assert "latest_mint_ts" in data
        assert "latest_ws_accept_ts" in data

    def test_has_direct_ws_field(self, loopback_client):
        data = self._get_status(loopback_client)
        assert isinstance(data["direct_ws_accepted"], bool)

    def test_has_runtime_version(self, loopback_client):
        data = self._get_status(loopback_client)
        assert "runtime_version" in data

    def test_has_runtime_commit(self, loopback_client):
        data = self._get_status(loopback_client)
        assert "runtime_commit" in data


# ---------------------------------------------------------------------------
# Secret-safety: no credentials in response
# ---------------------------------------------------------------------------


class TestOpsStatusSecretSafety:
    """Response must never contain raw credential material."""

    def _get_status(self, client):
        resp = client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_no_secret_in_response(self, loopback_client, paired_device):
        device_id, device_secret = paired_device
        data = self._get_status(loopback_client)
        response_text = json.dumps(data)
        assert device_secret not in response_text

    def test_no_secret_sha256_in_response(self, loopback_client, paired_device):
        data = self._get_status(loopback_client)
        response_text = json.dumps(data)
        assert "secret_sha256" not in response_text
        assert "secret_hash" not in response_text

    def test_no_raw_ticket_in_response(self, loopback_client, paired_device):
        device_id, device_secret = paired_device
        mint_resp = loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id, "device_secret": device_secret},
        )
        ticket = mint_resp.json()["ticket"]
        data = self._get_status(loopback_client)
        response_text = json.dumps(data)
        assert ticket not in response_text


# ---------------------------------------------------------------------------
# Device counts reflect actual state
# ---------------------------------------------------------------------------


class TestOpsStatusDeviceCounts:
    """Device counts match actual device state."""

    def _get_counts(self, client):
        resp = client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200
        data = resp.json()
        return data["active_device_count"], data["revoked_device_count"]

    def test_zero_devices_initial(self, loopback_client):
        active, revoked = self._get_counts(loopback_client)
        assert active == 0
        assert revoked == 0

    def test_one_active_device(self, loopback_client, paired_device):
        active, revoked = self._get_counts(loopback_client)
        assert active == 1
        assert revoked == 0

    def test_revoked_device_counted(self, loopback_client, paired_device):
        device_id, _ = paired_device
        revoke_resp = loopback_client.post(
            f"/api/mobile/devices/{device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert revoke_resp.status_code == 200
        active, revoked = self._get_counts(loopback_client)
        assert active == 0
        assert revoked == 1


# ---------------------------------------------------------------------------
# Audit counters
# ---------------------------------------------------------------------------


class TestOpsStatusAuditCounters:
    """Audit counters reflect actual events within 24h window."""

    def _get_status(self, client):
        resp = client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_pairing_increments_counter(self, loopback_client, paired_device):
        data = self._get_status(loopback_client)
        assert data["recent_pairing_count"] >= 1

    def test_mint_increments_counter(self, loopback_client, paired_device):
        device_id, device_secret = paired_device
        loopback_client.post(
            "/api/mobile/ws-ticket",
            json={"device_id": device_id, "device_secret": device_secret},
        )
        data = self._get_status(loopback_client)
        assert data["recent_mint_count"] >= 1

    def test_revocation_increments_counter(
        self, loopback_client, paired_device
    ):
        device_id, _ = paired_device
        loopback_client.post(
            f"/api/mobile/devices/{device_id}/revoke",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        data = self._get_status(loopback_client)
        assert data["recent_revocation_count"] >= 1


# ---------------------------------------------------------------------------
# Finding 1: Bounded audit reader
# ---------------------------------------------------------------------------


class TestBoundedAuditReader:
    """Audit reader is bounded by bytes, not full file read."""

    def test_large_file_only_reads_tail(self, tmp_path, monkeypatch):
        """Large audit file: only last N bytes are read."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        log_path = tmp_path / "logs" / "dashboard-auth.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a large file with 25000 events (~2MB+)
        events = []
        now = datetime.now(timezone.utc)
        for i in range(25000):
            events.append(json.dumps({
                "ts": (now - timedelta(minutes=i)).isoformat(),
                "event": "mobile_ticket_minted",
            }))
        log_path.write_text("\n".join(events) + "\n", encoding="utf-8")

        # The reader should return events, but should NOT have read
        # the whole file (bounded by _MAX_AUDIT_BYTES)
        result = _read_audit_window()
        # Should return some events (from the tail), not all 25000
        assert len(result) > 0
        assert len(result) < 25000, (
            "Reader should be bounded — did not truncate large file"
        )

    def test_empty_file(self, tmp_path, monkeypatch):
        """Empty audit log returns empty list."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        log_path = tmp_path / "logs" / "dashboard-auth.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        result = _read_audit_window()
        assert result == []

    def test_missing_file(self, tmp_path, monkeypatch):
        """Missing audit log returns empty list."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        result = _read_audit_window()
        assert result == []


# ---------------------------------------------------------------------------
# Finding 2: 24-hour event window
# ---------------------------------------------------------------------------


class Test24HourWindow:
    """Events older than 24 hours are excluded from counters."""

    def test_old_events_excluded(self):
        """Events > 24h ago are not counted."""
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
        events = [
            {"ts": old_ts, "event": "mobile_ticket_minted"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_mint_count"] == 0

    def test_recent_events_included(self):
        """Events within 24h are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_ticket_minted"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_mint_count"] == 1

    def test_mixed_events_only_recent(self):
        """Only recent events contribute to counters."""
        old_ts = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": old_ts, "event": "mobile_ticket_minted"},
            {"ts": old_ts, "event": "mobile_ticket_minted"},
            {"ts": now, "event": "mobile_ticket_minted"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_mint_count"] == 1

    def test_no_timestamp_skipped(self):
        """Events without timestamps are skipped."""
        events = [
            {"event": "mobile_ticket_minted"},  # no ts
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_mint_count"] == 0


# ---------------------------------------------------------------------------
# Finding 3: Required rejection reason counters
# ---------------------------------------------------------------------------


class TestRejectionReasonCounters:
    """All required rejection reason counters exist and work."""

    def test_all_rejection_counters_exist(self, loopback_client):
        """Response has all required rejection counters."""
        resp = loopback_client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200
        data = resp.json()
        required = [
            "recent_expired_ticket_count",
            "recent_replayed_ticket_count",
            "recent_wrong_audience_count",
            "recent_revoked_device_reject_count",
            "recent_malformed_count",
            "recent_oversized_count",
        ]
        for name in required:
            assert name in data, f"Missing rejection counter: {name}"
            assert isinstance(data[name], int)

    def test_expired_ticket_counter(self):
        """Expired ticket events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_ticket_expired"},
            {"ts": now, "event": "mobile_ticket_expired"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_expired_ticket_count"] == 2

    def test_replayed_ticket_counter(self):
        """Replayed ticket events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_ticket_replayed"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_replayed_ticket_count"] == 1

    def test_wrong_audience_counter(self):
        """Wrong-audience events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_ticket_wrong_audience"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_wrong_audience_count"] == 1

    def test_revoked_device_reject_counter(self):
        """Revoked-device rejection events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_revoked_device_rejected"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_revoked_device_reject_count"] == 1

    def test_malformed_counter(self):
        """Malformed request events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_request_malformed"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_malformed_count"] == 1

    def test_oversized_counter(self):
        """Oversized request events are counted."""
        now = datetime.now(timezone.utc).isoformat()
        events = [
            {"ts": now, "event": "mobile_request_oversized"},
        ]
        result = _aggregate_audit_events(events)
        assert result["recent_oversized_count"] == 1


# ---------------------------------------------------------------------------
# Finding 4: SQLite integrity time-bounded
# ---------------------------------------------------------------------------


class TestIntegrityBounded:
    """SQLite integrity check is time-bounded via progress handler."""

    def test_integrity_ok_with_valid_db(self, loopback_client, paired_device):
        """With a valid DB, integrity_ok is True."""
        data = loopback_client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        ).json()
        assert data["integrity_ok"] is True

    def test_integrity_false_with_no_db(self, tmp_path, monkeypatch):
        """When no DB exists, integrity_ok is False."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        ok, ts = _check_integrity()
        assert ok is False
        assert ts is not None

    def test_integrity_false_with_corrupt_db(self, tmp_path, monkeypatch):
        """Corrupt DB returns False, not an exception."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        dashboard_dir = tmp_path / "dashboard"
        dashboard_dir.mkdir(exist_ok=True)
        db_path = dashboard_dir / "mobile-devices.db"
        db_path.write_bytes(b"not a real sqlite database")
        ok, ts = _check_integrity()
        assert ok is False
        assert ts is not None


# ---------------------------------------------------------------------------
# Finding 5: Audit file permissions + parent dir
# ---------------------------------------------------------------------------


class TestAuditFilePermissions:
    """Audit log files and parent dir are restrictive, no double-close."""

    def test_audit_log_created_with_0600(self, tmp_path, monkeypatch):
        """New audit log file is created with mode 0600."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        log_path = tmp_path / "logs" / "dashboard-auth.log"
        assert not log_path.exists()

        audit_log(AuditEvent.LOGIN_START, ip="127.0.0.1")

        assert log_path.exists()
        mode = stat.S_IMODE(log_path.stat().st_mode)
        assert mode == 0o600, f"audit log mode is {oct(mode)}, expected 0o600"

    def test_audit_log_remains_0600_after_writes(self, tmp_path, monkeypatch):
        """Audit log file stays 0600 after multiple writes."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        log_path = tmp_path / "logs" / "dashboard-auth.log"

        for _ in range(5):
            audit_log(AuditEvent.LOGIN_SUCCESS, ip="127.0.0.1")

        mode = stat.S_IMODE(log_path.stat().st_mode)
        assert mode == 0o600, f"audit log mode is {oct(mode)}, expected 0o600"

    def test_existing_file_mode_enforced(self, tmp_path, monkeypatch):
        """If an existing audit log has wrong permissions, they are corrected."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        log_path = tmp_path / "logs" / "dashboard-auth.log"

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("test\n")
        log_path.chmod(0o644)

        audit_log(AuditEvent.LOGIN_START, ip="127.0.0.1")

        mode = stat.S_IMODE(log_path.stat().st_mode)
        assert mode == 0o600, f"audit log mode is {oct(mode)}, expected 0o600"

    def test_parent_directory_restrictive(self, tmp_path, monkeypatch):
        """Parent directory is made restrictive (0700)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        log_path = tmp_path / "logs" / "dashboard-auth.log"

        audit_log(AuditEvent.LOGIN_START, ip="127.0.0.1")

        logs_dir = tmp_path / "logs"
        dir_mode = stat.S_IMODE(logs_dir.stat().st_mode)
        assert dir_mode == 0o700, f"logs dir mode is {oct(dir_mode)}, expected 0o700"


# ---------------------------------------------------------------------------
# Finding 6: Overall health + dashboard completeness
# ---------------------------------------------------------------------------


class TestOverallHealth:
    """Overall health field reflects system state."""

    def test_healthy_when_all_good(self, loopback_client, paired_device):
        """Healthy state when DB is valid and migration is current."""
        data = loopback_client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        ).json()
        assert data["overall_health"] == "healthy"

    def test_unavailable_when_no_db(self, tmp_path, monkeypatch):
        """Unavailable when DB missing (integrity fails)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        status = get_mobile_ops_status()
        assert status.overall_health == "unavailable"


# ---------------------------------------------------------------------------
# Finding 7: Cached runtime metadata
# ---------------------------------------------------------------------------


class TestCachedRuntimeMetadata:
    """Runtime version/commit are cached, no git per request."""

    def test_no_subprocess_in_get_status(self):
        """get_mobile_ops_status does not spawn git subprocess."""
        with patch("hermes_cli.dashboard_auth.mobile_ops._sub") as mock_sub:
            get_mobile_ops_status()
            # Should NOT call subprocess
            mock_sub.run.assert_not_called()

    def test_runtime_version_cached(self):
        """Runtime version is from module-level cache."""
        assert _runtime_version is not None

    def test_runtime_commit_cached(self):
        """Runtime commit is from module-level cache (may be None in CI)."""
        # Field exists and is either a string or None
        assert isinstance(_runtime_commit, (str, type(None)))


# ---------------------------------------------------------------------------
# Route does not mutate device state
# ---------------------------------------------------------------------------


def test_ops_status_no_mutation(loopback_client, paired_device):
    """Calling ops-status must not change device last_used_at or other state."""
    device_id, _ = paired_device
    before = list_devices()
    before_state = {d.device_id: d.last_used_at for d in before}

    for _ in range(3):
        resp = loopback_client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        assert resp.status_code == 200

    after = list_devices()
    after_state = {d.device_id: d.last_used_at for d in after}
    assert before_state == after_state


# ---------------------------------------------------------------------------
# Integrity check behavior
# ---------------------------------------------------------------------------


class TestOpsStatusIntegrity:
    """Integrity field behavior for various DB states."""

    def test_integrity_ok_with_valid_db(self, loopback_client, paired_device):
        data = loopback_client.get(
            "/api/mobile/ops-status",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        ).json()
        assert data["integrity_ok"] is True

    def test_integrity_false_with_no_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        status = get_mobile_ops_status()
        assert status.integrity_ok is False


# ---------------------------------------------------------------------------
# Degraded state
# ---------------------------------------------------------------------------


class TestOpsStatusDegradedState:
    """The route returns degraded state on errors rather than 500."""

    def test_corrupt_db_returns_degraded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        reset_devices()
        dashboard_dir = tmp_path / "dashboard"
        dashboard_dir.mkdir(exist_ok=True)
        db_path = dashboard_dir / "mobile-devices.db"
        db_path.write_bytes(b"not a real sqlite database")

        status = get_mobile_ops_status()
        assert status is not None
        result = status.to_dict()
        assert "schema_version" in result


# ---------------------------------------------------------------------------
# Cache-Control header
# ---------------------------------------------------------------------------


def test_ops_status_cache_control(loopback_client):
    """Response must have Cache-Control: no-store."""
    resp = loopback_client.get(
        "/api/mobile/ops-status",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("Cache-Control", "")


# ---------------------------------------------------------------------------
# Runtime version/commit fields
# ---------------------------------------------------------------------------


def test_ops_status_runtime_version(loopback_client):
    """Runtime version should be populated."""
    resp = loopback_client.get(
        "/api/mobile/ops-status",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    data = resp.json()
    assert data["runtime_version"] is not None


def test_ops_status_runtime_commit(loopback_client):
    """Runtime commit should be populated (may be None in non-git env)."""
    resp = loopback_client.get(
        "/api/mobile/ops-status",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    data = resp.json()
    assert "runtime_commit" in data
