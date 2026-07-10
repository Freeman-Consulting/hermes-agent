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
`$HERMES_HOME/dashboard/mobile-devices.json` so paired phones survive dashboard
restarts without writing raw secrets to disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from typing import Any, Optional

from hermes_cli.config import get_hermes_home

PAIRING_TTL_SECONDS = 10 * 60
PAIRING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEVICE_ID_PREFIX = "ios_"
STORE_VERSION = 1

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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> Path:
    return get_hermes_home() / "dashboard" / "mobile-devices.json"


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


def _read_store_unlocked() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"version": STORE_VERSION, "devices": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Fail closed for verification, but keep pairing from crashing on an
        # unreadable/corrupt empty dev file by treating it as no devices.
        return {"version": STORE_VERSION, "devices": []}
    if not isinstance(data, dict):
        return {"version": STORE_VERSION, "devices": []}
    devices = data.get("devices")
    if not isinstance(devices, list):
        devices = []
    return {"version": int(data.get("version") or STORE_VERSION), "devices": devices}


def _write_store_unlocked(store: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _gc_pairing_codes_unlocked(now: Optional[int] = None) -> None:
    current = int(time.time()) if now is None else now
    expired = [key for key, entry in _pairing_codes.items() if int(entry.get("expires_at", 0)) < current]
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


def complete_pairing(*, code: str, device_name: Optional[str] = None) -> MobileDeviceCredential:
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
        store = _read_store_unlocked()
        existing_ids = {str(d.get("device_id")) for d in store.get("devices", []) if isinstance(d, dict)}
        while device_id in existing_ids:
            device_id = _new_device_id()
        store.setdefault("devices", []).append({
            "device_id": device_id,
            "device_name": final_name,
            "secret_sha256": _hash_secret(device_secret),
            "created_at": created_at,
            "last_used_at": None,
            "disabled": False,
        })
        store["version"] = STORE_VERSION
        _write_store_unlocked(store)

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
        store = _read_store_unlocked()
        devices = store.get("devices", [])
        for record in devices:
            if not isinstance(record, dict):
                continue
            if record.get("device_id") != cleaned_id:
                continue
            if record.get("disabled") is True:
                raise DeviceAuthInvalid("device credential disabled")
            expected_hash = str(record.get("secret_sha256") or "")
            if not expected_hash or not hmac.compare_digest(provided_hash, expected_hash):
                raise DeviceAuthInvalid("invalid device credential")
            record["last_used_at"] = _utc_now_iso()
            _write_store_unlocked(store)
            return MobileDevicePrincipal(
                device_id=cleaned_id,
                device_name=_clean_device_name(str(record.get("device_name") or "Hermes Pocket")),
            )
    raise DeviceAuthInvalid("invalid device credential")


def _reset_for_tests() -> None:
    """Test-only: clear process-local pairing codes and persisted devices."""
    with _lock:
        _pairing_codes.clear()
        path = _store_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
