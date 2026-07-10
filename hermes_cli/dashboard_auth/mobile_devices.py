"""Device-bound credentials for Hermes Pocket / mobile Gateway clients.

A mobile device cannot set browser cookies during a WebSocket upgrade and must
not store the reusable dashboard session token. This module provides a narrow
pairing flow instead:

* an authenticated dashboard caller creates a short-lived pairing code;
* the phone redeems that code once and receives a device id + device secret;
* only a SHA-256 digest of the device secret is persisted;
* the phone presents the device credential to mint a short-lived single-use
  `/api/ws` ticket immediately before opening the Gateway WebSocket.

Pairing codes are process-local and expire quickly. Device records persist in
``$HERMES_HOME/dashboard/mobile-devices.db`` (SQLite). A valid legacy JSON
registry at ``mobile-devices.json`` is migrated once on first access; corrupt
JSON or corrupt SQLite fails closed and is left byte-for-byte intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

from hermes_cli.config import get_hermes_home
from hermes_cli.sqlite_util import write_txn

PAIRING_TTL_SECONDS = 10 * 60
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEVICE_ID_PREFIX = "ios_"
SCHEMA_VERSION = 1
LEGACY_STORE_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
LEGACY_JSON_NAME = "mobile-devices.json"
LEGACY_BACKUP_SUFFIX = ".pre-sqlite"
DB_NAME = "mobile-devices.db"
DIR_MODE = 0o700
FILE_MODE = 0o600
_SECRET_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REQUIRED_DEVICE_COLUMNS = frozenset(
    {
        "device_id",
        "device_name",
        "secret_sha256",
        "created_at",
        "last_used_at",
        "revoked_at",
        "credential_version",
    }
)

_lock = threading.Lock()
_pairing_codes: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: int
    ttl_seconds: int
    device_name: str


@dataclass(frozen=True)
class MobileDeviceCredential:
    device_id: str
    device_secret: str
    device_name: str
    created_at: str


@dataclass(frozen=True)
class MobileDevicePrincipal:
    device_id: str
    device_name: str
    provider: str = "mobile-device"

    @property
    def user_id(self) -> str:
        return f"mobile:{self.device_id}"


class PairingError(Exception):
    """Base class for pairing failures."""


class PairingCodeInvalid(PairingError):
    """Pairing code was missing, expired, already used, or invalid."""


class DeviceAuthInvalid(Exception):
    """Device credential was missing, disabled, or invalid."""


class DeviceStoreError(Exception):
    """Base class for durable registry failures."""


class DeviceStoreCorrupt(DeviceStoreError):
    """Registry is corrupt, unsupported, or structurally invalid; fail closed."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dashboard_dir() -> Path:
    return get_hermes_home() / "dashboard"


def _db_path() -> Path:
    return _dashboard_dir() / DB_NAME


def _legacy_json_path() -> Path:
    return _dashboard_dir() / LEGACY_JSON_NAME


def _legacy_backup_path() -> Path:
    return _dashboard_dir() / (LEGACY_JSON_NAME + LEGACY_BACKUP_SUFFIX)


def _restrict_permissions(path: Path, mode: int = FILE_MODE) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _ensure_dashboard_dir() -> Path:
    path = _dashboard_dir()
    path.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(path, DIR_MODE)
    return path


def _live_store_paths(db_path: Path) -> tuple[Path, Path, Path]:
    return db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")


def _restrict_live_store_files(db_path: Path) -> None:
    """Owner-only modes for DB + live WAL/SHM sidecars while the store is in use."""
    _restrict_permissions(_ensure_dashboard_dir(), DIR_MODE)
    for path in _live_store_paths(db_path):
        if path.exists():
            _restrict_permissions(path, FILE_MODE)


def _clean_device_name(value: Optional[str]) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "Hermes Pocket"
    return cleaned[:80]


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _hash_code(code: str) -> str:
    return _hash_secret(_normalize_code(code))


def _format_code(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:]}"


def _new_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(8))
    return _format_code(raw)


def _new_device_id() -> str:
    return DEVICE_ID_PREFIX + secrets.token_urlsafe(12).replace("-", "_")


def _as_store_corrupt(exc: BaseException, message: str) -> DeviceStoreCorrupt:
    return DeviceStoreCorrupt(message) if not isinstance(exc, DeviceStoreCorrupt) else exc


def _integrity_ok(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise DeviceStoreCorrupt(
            "mobile-devices.db failed SQLite integrity check"
        ) from exc
    if row is None or str(row[0]).lower() != "ok":
        detail = str(row[0]) if row is not None else "unknown"
        raise DeviceStoreCorrupt(
            f"mobile-devices.db failed SQLite integrity check: {detail}"
        )


def _verify_existing_schema(conn: sqlite3.Connection) -> None:
    """Fail closed on missing/malformed/unsupported schema for an existing DB."""
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "meta" not in tables:
            raise DeviceStoreCorrupt("mobile-devices.db missing meta table")
        if "devices" not in tables:
            raise DeviceStoreCorrupt("mobile-devices.db missing devices table")

        version_rows = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("schema_version",),
        ).fetchall()
        if len(version_rows) != 1:
            raise DeviceStoreCorrupt(
                "mobile-devices.db schema_version metadata is missing or duplicate"
            )
        raw_version = version_rows[0][0]
        try:
            version = int(str(raw_version).strip())
        except (TypeError, ValueError) as exc:
            raise DeviceStoreCorrupt(
                "mobile-devices.db schema_version is not an integer"
            ) from exc
        if version != SCHEMA_VERSION:
            raise DeviceStoreCorrupt(
                f"mobile-devices.db unsupported schema_version={version}"
            )

        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(devices)")
        }
        missing = sorted(_REQUIRED_DEVICE_COLUMNS - columns)
        if missing:
            raise DeviceStoreCorrupt(
                "mobile-devices.db devices table missing required columns: "
                + ", ".join(missing)
            )
    except DeviceStoreCorrupt:
        raise
    except sqlite3.Error as exc:
        raise DeviceStoreCorrupt(
            f"mobile-devices.db schema validation failed: {exc}"
        ) from exc


def _create_schema(conn: sqlite3.Connection) -> None:
    with write_txn(conn):
        conn.execute(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
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
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )


def _raw_connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection without enabling WAL or mutating schema."""
    try:
        conn = sqlite3.connect(
            str(path),
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
        )
    except sqlite3.Error as exc:
        raise DeviceStoreCorrupt(
            "mobile-devices.db is not a usable SQLite database"
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error as exc:
        conn.close()
        raise DeviceStoreCorrupt(
            f"mobile-devices.db failed to initialize connection: {exc}"
        ) from exc
    except Exception:
        conn.close()
        raise


def _enable_wal_and_restrict(conn: sqlite3.Connection, path: Path) -> None:
    """Prefer WAL, then force owner-only modes on DB + live sidecars."""
    _restrict_permissions(path, FILE_MODE)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        # WAL is preferred but not required on exotic filesystems.
        pass
    _restrict_live_store_files(path)


def _open_existing_db(db_path: Path) -> sqlite3.Connection:
    """Open an existing DB fail-closed: never delete/recreate/overwrite bytes."""
    _ensure_dashboard_dir()
    _restrict_permissions(db_path, FILE_MODE)
    conn = _raw_connect(db_path)
    try:
        # Validate before enabling WAL so a corrupt image is not journaled.
        _integrity_ok(conn)
        _verify_existing_schema(conn)
        _enable_wal_and_restrict(conn, db_path)
        return conn
    except DeviceStoreCorrupt:
        conn.close()
        raise
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DeviceStoreCorrupt(
            "mobile-devices.db is corrupt or unreadable"
        ) from exc
    except sqlite3.Error as exc:
        conn.close()
        raise DeviceStoreCorrupt(
            f"mobile-devices.db open/verify failed: {exc}"
        ) from exc
    except Exception:
        conn.close()
        raise


def _open_new_db(db_path: Path) -> sqlite3.Connection:
    """Create a genuinely new empty store at schema version 1."""
    _ensure_dashboard_dir()
    # Create an empty DB file so umask cannot leave a world-readable image.
    if not db_path.exists():
        fd = os.open(
            str(db_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            FILE_MODE,
        )
        os.close(fd)
    _restrict_permissions(db_path, FILE_MODE)
    conn = _raw_connect(db_path)
    try:
        # Mode before WAL sidecars, then schema, then re-restrict live files.
        _enable_wal_and_restrict(conn, db_path)
        _create_schema(conn)
        _verify_existing_schema(conn)
        _restrict_live_store_files(db_path)
        return conn
    except Exception:
        conn.close()
        # Best-effort cleanup of a half-created DB so retry stays fail-closed.
        try:
            for path in _live_store_paths(db_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        raise


def _validate_legacy_store(data: Any) -> list[dict[str, Any]]:
    """Return validated device rows or raise DeviceStoreCorrupt."""
    if not isinstance(data, dict):
        raise DeviceStoreCorrupt("legacy mobile-devices.json is not an object")

    version = data.get("version")
    if version is None:
        raise DeviceStoreCorrupt("legacy mobile-devices.json missing version")
    try:
        store_version = int(version)
    except (TypeError, ValueError) as exc:
        raise DeviceStoreCorrupt(
            "legacy mobile-devices.json version is not an integer"
        ) from exc
    if store_version != LEGACY_STORE_VERSION:
        raise DeviceStoreCorrupt(
            f"legacy mobile-devices.json unsupported version={store_version}"
        )

    devices = data.get("devices")
    if not isinstance(devices, list):
        raise DeviceStoreCorrupt("legacy mobile-devices.json devices is not a list")

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(devices):
        if not isinstance(record, dict):
            raise DeviceStoreCorrupt(f"legacy device entry {index} is not an object")
        device_id = record.get("device_id")
        secret_sha256 = record.get("secret_sha256")
        if not isinstance(device_id, str) or not device_id.strip():
            raise DeviceStoreCorrupt(f"legacy device entry {index} missing device_id")
        if not isinstance(secret_sha256, str) or not secret_sha256.strip():
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} missing secret_sha256"
            )
        secret_sha256 = secret_sha256.strip()
        if not _SECRET_SHA256_RE.fullmatch(secret_sha256):
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} secret_sha256 must be 64 hex characters"
            )
        normalized_id = device_id.strip()
        if normalized_id in seen_ids:
            raise DeviceStoreCorrupt(
                f"legacy mobile-devices.json has duplicate device_id: {normalized_id}"
            )
        seen_ids.add(normalized_id)

        device_name = record.get("device_name")
        if device_name is not None and not isinstance(device_name, str):
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} has invalid device_name"
            )
        created_at = record.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} missing created_at"
            )
        created_at = created_at.strip()
        last_used_at = record.get("last_used_at")
        if last_used_at is not None and not isinstance(last_used_at, str):
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} has invalid last_used_at"
            )
        disabled = record.get("disabled")
        if disabled is not None and not isinstance(disabled, bool):
            raise DeviceStoreCorrupt(
                f"legacy device entry {index} has invalid disabled"
            )
        revoked_at = created_at if disabled is True else None
        validated.append(
            {
                "device_id": normalized_id,
                "device_name": _clean_device_name(device_name),
                "secret_sha256": secret_sha256,
                "created_at": created_at,
                "last_used_at": last_used_at if isinstance(last_used_at, str) else None,
                "revoked_at": revoked_at,
                "credential_version": 1,
            }
        )
    return validated


def _parse_legacy_json_bytes(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeviceStoreCorrupt(
            "legacy mobile-devices.json is not valid UTF-8"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeviceStoreCorrupt(
            "legacy mobile-devices.json is not valid JSON"
        ) from exc
    return _validate_legacy_store(data)


def _open_store_unlocked() -> sqlite3.Connection:
    """Open the durable store, migrating valid legacy JSON once if needed.

    Fail-closed rules:
    * corrupt/structurally invalid legacy JSON raises DeviceStoreCorrupt and
      never creates a database;
    * existing corrupt/unsupported SQLite raises DeviceStoreCorrupt and never
      deletes, truncates, or overwrites the existing bytes;
    * missing legacy JSON initializes a clean database;
    * existing valid database is authoritative.
    """
    db_path = _db_path()
    json_path = _legacy_json_path()

    if db_path.exists():
        return _open_existing_db(db_path)

    # No DB yet. Corrupt JSON must not create one.
    if json_path.exists():
        raw = json_path.read_bytes()
        try:
            devices = _parse_legacy_json_bytes(raw)
        except DeviceStoreCorrupt:
            # Leave the corrupt file byte-for-byte untouched and create nothing.
            raise
        # Valid JSON: create DB, migrate, preserve backup.
        conn = _open_new_db(db_path)
        try:
            with write_txn(conn):
                for device in devices:
                    conn.execute(
                        """
                        INSERT INTO devices (
                            device_id, device_name, secret_sha256, created_at,
                            last_used_at, revoked_at, credential_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            device["device_id"],
                            device["device_name"],
                            device["secret_sha256"],
                            device["created_at"],
                            device["last_used_at"],
                            device["revoked_at"],
                            device["credential_version"],
                        ),
                    )
            backup = _legacy_backup_path()
            if backup.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup = json_path.with_name(
                    f"{LEGACY_JSON_NAME}{LEGACY_BACKUP_SUFFIX}.{stamp}"
                )
            backup.write_bytes(raw)
            _restrict_permissions(backup, FILE_MODE)
            try:
                json_path.unlink()
            except FileNotFoundError:
                pass
            _restrict_live_store_files(db_path)
            return conn
        except Exception as exc:
            conn.close()
            # Best-effort cleanup of a half-created DB so retry stays fail-closed.
            try:
                for path in _live_store_paths(db_path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            except OSError:
                pass
            if isinstance(exc, DeviceStoreCorrupt):
                raise
            raise _as_store_corrupt(
                exc, f"legacy mobile-devices.json migration failed: {exc}"
            ) from exc

    # Clean first-run: no JSON, no DB.
    return _open_new_db(db_path)


def _gc_pairing_codes_unlocked(now: Optional[int] = None) -> None:
    current = int(time.time()) if now is None else now
    expired = [
        key
        for key, entry in _pairing_codes.items()
        if int(entry.get("expires_at", 0)) < current
    ]
    for key in expired:
        _pairing_codes.pop(key, None)


def create_pairing_code(*, device_name: Optional[str] = None) -> PairingCode:
    """Create a short-lived one-time pairing code for a mobile client."""
    now = int(time.time())
    cleaned_name = _clean_device_name(device_name)
    with _lock:
        _gc_pairing_codes_unlocked(now)
        code = _new_pairing_code()
        code_hash = _hash_code(code)
        while code_hash in _pairing_codes:
            code = _new_pairing_code()
            code_hash = _hash_code(code)
        expires_at = now + PAIRING_TTL_SECONDS
        _pairing_codes[code_hash] = {
            "device_name": cleaned_name,
            "created_at": now,
            "expires_at": expires_at,
        }
    return PairingCode(
        code=code,
        expires_at=expires_at,
        ttl_seconds=PAIRING_TTL_SECONDS,
        device_name=cleaned_name,
    )


def complete_pairing(
    *, code: str, device_name: Optional[str] = None
) -> MobileDeviceCredential:
    """Redeem a one-time code and create a persistent device credential."""
    normalized = _normalize_code(code)
    if not normalized:
        raise PairingCodeInvalid("missing pairing code")
    code_hash = _hash_code(normalized)
    now = int(time.time())
    with _lock:
        _gc_pairing_codes_unlocked(now)
        entry = _pairing_codes.pop(code_hash, None)
        if entry is None:
            raise PairingCodeInvalid("invalid or expired pairing code")
        if int(entry.get("expires_at", 0)) < now:
            raise PairingCodeInvalid("invalid or expired pairing code")

        final_name = _clean_device_name(device_name or entry.get("device_name"))
        device_secret = secrets.token_urlsafe(32)
        device_id = _new_device_id()
        created_at = _utc_now_iso()
        secret_hash = _hash_secret(device_secret)

        conn = _open_store_unlocked()
        try:
            with write_txn(conn):
                existing = {
                    str(row["device_id"])
                    for row in conn.execute("SELECT device_id FROM devices")
                }
                while device_id in existing:
                    device_id = _new_device_id()
                conn.execute(
                    """
                    INSERT INTO devices (
                        device_id, device_name, secret_sha256, created_at,
                        last_used_at, revoked_at, credential_version
                    ) VALUES (?, ?, ?, ?, NULL, NULL, 1)
                    """,
                    (device_id, final_name, secret_hash, created_at),
                )
            _restrict_live_store_files(_db_path())
        finally:
            conn.close()

    return MobileDeviceCredential(
        device_id=device_id,
        device_secret=device_secret,
        device_name=final_name,
        created_at=created_at,
    )


def verify_device(*, device_id: str, device_secret: str) -> MobileDevicePrincipal:
    """Validate a mobile device credential and update last-used metadata."""
    cleaned_id = (device_id or "").strip()
    cleaned_secret = (device_secret or "").strip()
    if not cleaned_id or not cleaned_secret:
        raise DeviceAuthInvalid("missing device credential")
    provided_hash = _hash_secret(cleaned_secret)
    with _lock:
        conn = _open_store_unlocked()
        try:
            with write_txn(conn):
                row = conn.execute(
                    """
                    SELECT device_id, device_name, secret_sha256, revoked_at
                    FROM devices
                    WHERE device_id = ?
                    """,
                    (cleaned_id,),
                ).fetchone()
                if row is None:
                    raise DeviceAuthInvalid("invalid device credential")
                if row["revoked_at"] is not None:
                    raise DeviceAuthInvalid("device credential disabled")
                expected_hash = str(row["secret_sha256"] or "")
                if not expected_hash or not hmac.compare_digest(
                    provided_hash, expected_hash
                ):
                    raise DeviceAuthInvalid("invalid device credential")
                last_used = _utc_now_iso()
                conn.execute(
                    "UPDATE devices SET last_used_at = ? WHERE device_id = ?",
                    (last_used, cleaned_id),
                )
                principal = MobileDevicePrincipal(
                    device_id=cleaned_id,
                    device_name=_clean_device_name(
                        str(row["device_name"] or "Hermes Pocket")
                    ),
                )
            _restrict_live_store_files(_db_path())
            return principal
        finally:
            conn.close()


def _reset_for_tests() -> None:
    """Test-only: clear process-local pairing codes and persisted devices."""
    with _lock:
        _pairing_codes.clear()
        for path in (
            _db_path(),
            Path(str(_db_path()) + "-wal"),
            Path(str(_db_path()) + "-shm"),
            _legacy_json_path(),
            _legacy_backup_path(),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
