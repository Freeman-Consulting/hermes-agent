from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers
from hermes_cli.dashboard_auth.mobile_devices import (
    DeviceAuthInvalid,
    DeviceStoreCorrupt,
    DeviceStoreError,
    PairingCodeInvalid,
    _db_path,
    _legacy_backup_path,
    _legacy_json_path,
    _open_store_unlocked,
    _reset_for_tests,
    complete_pairing,
    create_pairing_code,
    verify_device,
)
from hermes_cli.dashboard_auth.ws_tickets import _reset_for_tests as reset_tickets
from hermes_cli.sqlite_util import write_txn


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


def _read_db_devices(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM devices ORDER BY device_id"))
    finally:
        conn.close()


def _write_legacy_json(tmp_path: Path, payload: dict) -> tuple[Path, bytes]:
    json_path = tmp_path / "dashboard" / "mobile-devices.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    original_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    json_path.write_bytes(original_bytes)
    return json_path, original_bytes


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

    db_path = tmp_path / "dashboard" / "mobile-devices.db"
    assert db_path.is_file()
    raw = db_path.read_bytes()
    assert credential.device_secret.encode("utf-8") not in raw
    rows = _read_db_devices(db_path)
    assert len(rows) == 1
    assert rows[0]["device_id"] == credential.device_id
    assert rows[0]["secret_sha256"]
    assert rows[0]["secret_sha256"] != credential.device_secret
    assert rows[0]["revoked_at"] is None
    assert rows[0]["credential_version"] == 1


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


def test_missing_store_initializes_clean_db(tmp_path):
    pairing = create_pairing_code(device_name="fresh")
    credential = complete_pairing(code=pairing.code)
    db_path = _db_path()
    assert db_path.is_file()
    assert not _legacy_json_path().exists()
    assert (
        verify_device(
            device_id=credential.device_id,
            device_secret=credential.device_secret,
        ).device_id
        == credential.device_id
    )
    mode = db_path.stat().st_mode & 0o777
    assert mode == 0o600 or os.name == "nt"
    dash_mode = db_path.parent.stat().st_mode & 0o777
    assert dash_mode == 0o700 or os.name == "nt"


def test_legacy_json_migrates_preserving_credentials(tmp_path):
    secret = "legacy-device-secret-value-32chars!!"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    legacy = {
        "version": 1,
        "devices": [
            {
                "device_id": "ios_legacy_device_1",
                "device_name": "Legacy Phone",
                "secret_sha256": digest,
                "created_at": "2026-01-02T03:04:05+00:00",
                "last_used_at": "2026-01-03T04:05:06+00:00",
                "disabled": False,
            },
            {
                "device_id": "ios_legacy_disabled",
                "device_name": "Disabled Phone",
                "secret_sha256": hashlib.sha256(b"other").hexdigest(),
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_used_at": None,
                "disabled": True,
            },
        ],
    }
    json_path, original_bytes = _write_legacy_json(tmp_path, legacy)

    principal = verify_device(device_id="ios_legacy_device_1", device_secret=secret)
    assert principal.device_name == "Legacy Phone"

    db_path = _db_path()
    assert db_path.is_file()
    assert not json_path.exists()
    backup = _legacy_backup_path()
    assert backup.is_file()
    assert backup.read_bytes() == original_bytes
    backup_mode = backup.stat().st_mode & 0o777
    assert backup_mode == 0o600 or os.name == "nt"

    rows = {row["device_id"]: row for row in _read_db_devices(db_path)}
    assert rows["ios_legacy_device_1"]["secret_sha256"] == digest
    assert rows["ios_legacy_device_1"]["created_at"] == "2026-01-02T03:04:05+00:00"
    assert rows["ios_legacy_device_1"]["last_used_at"]  # updated by verify
    assert rows["ios_legacy_device_1"]["revoked_at"] is None
    assert rows["ios_legacy_disabled"]["revoked_at"] == "2026-01-01T00:00:00+00:00"
    assert (
        rows["ios_legacy_disabled"]["secret_sha256"]
        == hashlib.sha256(b"other").hexdigest()
    )

    with pytest.raises(DeviceAuthInvalid):
        verify_device(device_id="ios_legacy_disabled", device_secret="other")


def test_corrupt_json_fails_closed_and_preserves_bytes(tmp_path):
    json_path = tmp_path / "dashboard" / "mobile-devices.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b'{"version": 1, "devices": [NOT_JSON\n'
    json_path.write_bytes(corrupt)

    pairing = create_pairing_code(device_name="phone")
    with pytest.raises(DeviceStoreCorrupt):
        complete_pairing(code=pairing.code)

    assert json_path.read_bytes() == corrupt
    assert not _db_path().exists()
    assert not _legacy_backup_path().exists()


def test_structurally_invalid_json_fails_closed(tmp_path):
    json_path = tmp_path / "dashboard" / "mobile-devices.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "devices": [{"device_name": "no-id"}]}).encode(
        "utf-8"
    )
    json_path.write_bytes(payload)

    with pytest.raises(DeviceStoreCorrupt):
        create_pairing_code(device_name="x")
        # force store open via verify path
        verify_device(device_id="ios_missing", device_secret="x")

    # Also corrupt path via complete after pairing
    pairing = create_pairing_code(device_name="x")
    with pytest.raises(DeviceStoreCorrupt):
        complete_pairing(code=pairing.code)

    assert json_path.read_bytes() == payload
    assert not _db_path().exists()


def test_corrupt_sqlite_fails_closed_typed_error_preserves_bytes(tmp_path):
    db_path = tmp_path / "dashboard" / "mobile-devices.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b"NOT A SQLITE DATABASE\x00\x01\x02garbage-bytes"
    db_path.write_bytes(corrupt)

    pairing = create_pairing_code(device_name="phone")
    with pytest.raises(DeviceStoreCorrupt) as exc_info:
        complete_pairing(code=pairing.code)

    assert isinstance(exc_info.value, DeviceStoreError)
    assert type(exc_info.value).__name__ == "DeviceStoreCorrupt"
    assert db_path.read_bytes() == corrupt
    # No rewrite/recreate of the existing image.
    assert db_path.stat().st_size == len(corrupt)


def test_unsupported_schema_version_fails_closed(tmp_path):
    db_path = tmp_path / "dashboard" / "mobile-devices.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY,
                device_name TEXT NOT NULL,
                secret_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT,
                credential_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '999')"
        )
        conn.commit()
    finally:
        conn.close()
    original = db_path.read_bytes()

    with pytest.raises(DeviceStoreCorrupt, match="unsupported schema_version"):
        verify_device(device_id="ios_x", device_secret="x")

    assert db_path.read_bytes() == original
    # Confirm production path did not silently rewrite schema_version.
    check = sqlite3.connect(str(db_path))
    try:
        version = check.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert version == "999"
    finally:
        check.close()


def test_malformed_devices_schema_fails_closed(tmp_path):
    db_path = tmp_path / "dashboard" / "mobile-devices.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        # Missing required columns on purpose.
        conn.execute(
            "CREATE TABLE devices (device_id TEXT PRIMARY KEY, note TEXT)"
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
    finally:
        conn.close()
    original = db_path.read_bytes()

    with pytest.raises(DeviceStoreCorrupt, match="missing required columns"):
        create_pairing_code(device_name="x")
        complete_pairing(code=create_pairing_code(device_name="x").code)

    pairing = create_pairing_code(device_name="x")
    with pytest.raises(DeviceStoreCorrupt, match="missing required columns"):
        complete_pairing(code=pairing.code)

    assert db_path.read_bytes() == original


def test_live_db_wal_shm_owner_only_under_umask_022(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX file modes are not enforced on Windows")

    from hermes_cli.dashboard_auth import mobile_devices as md

    old_umask = os.umask(0o022)
    try:
        with md._lock:
            conn = _open_store_unlocked()
            try:
                with write_txn(conn):
                    conn.execute(
                        """
                        INSERT INTO devices (
                            device_id, device_name, secret_sha256, created_at,
                            last_used_at, revoked_at, credential_version
                        ) VALUES (?, ?, ?, ?, NULL, NULL, 1)
                        """,
                        (
                            "ios_perm_probe",
                            "perm",
                            "a" * 64,
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                md._restrict_live_store_files(md._db_path())

                db_path = md._db_path()
                wal = Path(str(db_path) + "-wal")
                shm = Path(str(db_path) + "-shm")
                dash = md._dashboard_dir()

                assert (dash.stat().st_mode & 0o777) == 0o700
                assert (db_path.stat().st_mode & 0o777) == 0o600
                # Sidecars must be owner-only while the connection remains open.
                assert wal.is_file(), "expected live WAL after write under WAL mode"
                assert shm.is_file(), "expected live SHM after write under WAL mode"
                assert (wal.stat().st_mode & 0o777) == 0o600
                assert (shm.stat().st_mode & 0o777) == 0o600
            finally:
                conn.close()
    finally:
        os.umask(old_umask)


def test_legacy_invalid_secret_hash_fails_closed(tmp_path):
    payload = {
        "version": 1,
        "devices": [
            {
                "device_id": "ios_bad_hash",
                "device_name": "Bad",
                "secret_sha256": "not-a-sha256",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    json_path, original = _write_legacy_json(tmp_path, payload)
    with pytest.raises(DeviceStoreCorrupt, match="secret_sha256"):
        verify_device(device_id="ios_bad_hash", device_secret="x")
    assert json_path.read_bytes() == original
    assert not _db_path().exists()


def test_legacy_missing_created_at_fails_closed(tmp_path):
    digest = "a" * 64
    payload = {
        "version": 1,
        "devices": [
            {
                "device_id": "ios_no_created",
                "device_name": "NoCreated",
                "secret_sha256": digest,
            }
        ],
    }
    json_path, original = _write_legacy_json(tmp_path, payload)
    with pytest.raises(DeviceStoreCorrupt, match="created_at"):
        verify_device(device_id="ios_no_created", device_secret="x")
    assert json_path.read_bytes() == original
    assert not _db_path().exists()


def test_legacy_duplicate_device_id_fails_closed(tmp_path):
    digest = "b" * 64
    payload = {
        "version": 1,
        "devices": [
            {
                "device_id": "ios_dup",
                "device_name": "One",
                "secret_sha256": digest,
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "device_id": "ios_dup",
                "device_name": "Two",
                "secret_sha256": "c" * 64,
                "created_at": "2026-01-02T00:00:00+00:00",
            },
        ],
    }
    json_path, original = _write_legacy_json(tmp_path, payload)
    with pytest.raises(DeviceStoreCorrupt, match="duplicate device_id"):
        verify_device(device_id="ios_dup", device_secret="x")
    assert json_path.read_bytes() == original
    assert not _db_path().exists()


def test_legacy_unsupported_version_fails_closed(tmp_path):
    payload = {
        "version": 2,
        "devices": [
            {
                "device_id": "ios_v2",
                "device_name": "V2",
                "secret_sha256": "d" * 64,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    json_path, original = _write_legacy_json(tmp_path, payload)
    with pytest.raises(DeviceStoreCorrupt, match="unsupported version"):
        verify_device(device_id="ios_v2", device_secret="x")
    assert json_path.read_bytes() == original
    assert not _db_path().exists()


def test_restart_persistence_survives_process_reset(tmp_path):
    pairing = create_pairing_code(device_name="sticky")
    credential = complete_pairing(code=pairing.code, device_name="sticky")
    # Simulate process restart: clear in-memory pairing state only; keep DB.
    from hermes_cli.dashboard_auth import mobile_devices as md

    with md._lock:
        md._pairing_codes.clear()

    principal = verify_device(
        device_id=credential.device_id,
        device_secret=credential.device_secret,
    )
    assert principal.device_id == credential.device_id
    assert _db_path().is_file()


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
