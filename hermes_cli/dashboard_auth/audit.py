"""Audit log for dashboard-auth events.

Profile-aware location: ``$HERMES_HOME/logs/dashboard-auth.log``.
Format: one JSON object per line. Token-like fields are stripped before
serialisation to avoid leaking refresh tokens or JWTs to disk.

This module deliberately keeps a minimal dependency surface — no imports
from ``hermes_constants`` or other hermes_cli modules — so it can be
imported safely from middleware code that loads early in the startup
sequence.
"""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_write_lock = threading.Lock()
_MAX_FIELD_LENGTH = 256
_TRUNCATION_MARKER = "...[truncated]"

# Size-based rotation matching hermes_logging's agent.log / errors.log convention
# (maxBytes=5MB, backupCount=3, stdlib RotatingFileHandler ``.1``/``.2``/``.3`` naming).
# Implemented locally (check-and-rename, stdlib only) rather than importing
# hermes_logging/_ManagedRotatingFileHandler: that would pull in hermes_constants
# (and lazily hermes_cli.config) and break this module's import-light guarantee —
# it is imported by middleware that runs very early in startup.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# Canonical field-name stems that must never appear in the log raw.
# Matching is case-insensitive. Includes both hyphen and underscore
# variants where relevant (e.g., 'set_cookie' and 'set-cookie').
# Includes mobile secret-bearing names so defence remains effective
# even if a caller accidentally supplies one.
_REDACTED_STEMS: frozenset = frozenset({
     "access_token", "refresh_token", "refreshtoken",
     "code", "code_verifier", "codeverifier",
     "state", "ticket", "cookie", "authorization",
     # Mobile secret-bearing fields (both snake_case and camelCase forms)
     "pairing_code", "pairingcode", "device_secret", "devicesecret",
     "secret_sha256", "raw_ticket", "rawticket",
     "secret_hash", "set_cookie",
     "auth_value", "authheader",
 })


class AuditEvent(enum.Enum):
    """Event types written to dashboard-auth.log.

    Values are the literal ``event`` field on the JSON line.
    """

    LOGIN_START = "login_start"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    REFRESH_SUCCESS = "refresh_success"
    REFRESH_FAILURE = "refresh_failure"
    REVOKE = "revoke"
    SESSION_VERIFY_FAILURE = "session_verify_failure"
    SESSION_REJECTED = "session_rejected"
    WS_TICKET_MINTED = "ws_ticket_minted"
    WS_TICKET_REJECTED = "ws_ticket_rejected"
    TOKEN_AUTH_SUCCESS = "token_auth_success"
    TOKEN_AUTH_FAILURE = "token_auth_failure"
    # RFC 8252 native-app (system-browser + loopback + PKCE) flow.
    NATIVE_AUTHORIZE_START = "native_authorize_start"
    NATIVE_CODE_ISSUED = "native_code_issued"
    NATIVE_TOKEN_SUCCESS = "native_token_success"
    NATIVE_TOKEN_FAILURE = "native_token_failure"
    # Mobile security events (Phase 4)
    MOBILE_PAIRING_CODE_CREATED = "mobile_pairing_code_created"
    MOBILE_PAIRING_REDEEMED = "mobile_pairing_redeemed"
    MOBILE_PAIRING_REJECTED = "mobile_pairing_rejected"
    MOBILE_TICKET_MINTED = "mobile_ticket_minted"
    MOBILE_TICKET_MINT_REJECTED = "mobile_ticket_mint_rejected"
    MOBILE_WS_ACCEPTED = "mobile_ws_accepted"
    MOBILE_DEVICE_REVOKED = "mobile_device_revoked"
    MOBILE_CREDENTIAL_ROTATED = "mobile_credential_rotated"
    MOBILE_CREDENTIAL_ROTATION_REJECTED = "mobile_credential_rotation_rejected"
    MOBILE_RATE_LIMIT_REJECTED = "mobile_rate_limit_rejected"
    # Structured mobile ticket rejection reasons (Phase 2 correction)
    MOBILE_TICKET_EXPIRED = "mobile_ticket_expired"
    MOBILE_TICKET_REPLAYED = "mobile_ticket_replayed"
    MOBILE_TICKET_WRONG_AUDIENCE = "mobile_ticket_wrong_audience"
    MOBILE_REVOKED_DEVICE_REJECTED = "mobile_revoked_device_rejected"
    MOBILE_REQUEST_MALFORMED = "mobile_request_malformed"
    MOBILE_REQUEST_OVERSIZED = "mobile_request_oversized"
    MOBILE_ARTIFACT_DOWNLOADED = "mobile_artifact_downloaded"
    MOBILE_ARTIFACT_DOWNLOAD_REJECTED = "mobile_artifact_download_rejected"


def _resolve_log_path() -> Path:
    """``$HERMES_HOME/logs/dashboard-auth.log``.

    Uses ``hermes_constants.get_hermes_home()`` (a leaf module — no import
    cycle) so profile overrides and the native-Windows ``%LOCALAPPDATA%``
    fallback are honored.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "dashboard-auth.log"


def _normalize_field_name(name: str) -> str:
    return name.lower().replace("-", "_")


def _bounded_value(value: Any) -> Any:
    """Bound strings, including nested values and non-JSON object representations."""
    if isinstance(value, dict):
        return {k: _bounded_value(v) for k, v in value.items()
                if isinstance(k, str) and _normalize_field_name(k) not in _REDACTED_STEMS}
    if isinstance(value, (list, tuple)):
        return [_bounded_value(v) for v in value]
    if not isinstance(value, (str, int, float, bool, type(None))):
        value = repr(value)
    if isinstance(value, str) and len(value) > _MAX_FIELD_LENGTH:
        return value[:_MAX_FIELD_LENGTH - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    return value


def _rotate_if_needed(path: Path, pending_bytes: int) -> None:
    """Rotate ``path`` to ``.1``/…/``.<_BACKUP_COUNT>`` once appending *pending_bytes* would
    reach ``_MAX_BYTES``. Mirrors stdlib ``RotatingFileHandler``'s naming; must be called
    with ``_write_lock`` held. Rotation errors must not break the write path."""
    try:
        if path.stat().st_size + pending_bytes < _MAX_BYTES:
            return
    except OSError:
        return  # missing/vanished mid-check; the append itself surfaces real errors
    try:
        for i in range(_BACKUP_COUNT - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass  # a log that cannot be rotated is still a working (if overlong) log


def audit_log(event: AuditEvent, **fields: Any) -> None:
    """Append one event; token-like fields dropped, log dir created, size-capped rotation
    (``_MAX_BYTES`` with ``_BACKUP_COUNT`` backups). Write failures are logged at WARNING
    but never raise — auth must not fail because the audit logger broke."""
    try:
        entry = {
            "ts": _dt.datetime.now(_dt.UTC).isoformat(),
            "event": event.value,
            **{k: _bounded_value(v) for k, v in fields.items() if _normalize_field_name(k) not in _REDACTED_STEMS}}
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        line_bytes = line.encode("utf-8")
        path = _resolve_log_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _write_lock:
            path.parent.chmod(0o700)
            if path.exists():
                path.chmod(0o600)
            _rotate_if_needed(path, len(line_bytes))
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "ab") as f:
                f.write(line_bytes)
    except Exception as e:
        _log.warning("dashboard-auth audit log write failed: %s", e)


def ticket_fingerprint(ticket: str) -> str:
    import hashlib
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()[:16]
