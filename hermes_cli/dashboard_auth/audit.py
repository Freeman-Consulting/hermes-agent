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


def _resolve_log_path() -> Path:
    """``$HERMES_HOME/logs/dashboard-auth.log`` with the standard fallback.

    Mirrors ``hermes_constants.get_hermes_home`` semantics: env var wins,
    else ``~/.hermes``. A local copy avoids an import cycle with the
    middleware which lives below ``hermes_cli``.
    """
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "logs" / "dashboard-auth.log"


def _normalize_field_name(name: str) -> str:
    """Canonicalise a field name for redaction matching.

    Lowercase and replace hyphens with underscores so that
    'Authorization', 'SET-COOKIE', 'deviceSecret' all map to their
    canonical stems before comparison.
    """
    return name.lower().replace("-", "_")


def audit_log(event: AuditEvent, **fields: Any) -> None:
    """Append one event to the audit log.

    Token-like fields are dropped. Missing log directory is created.
    Write failures are logged at WARNING but never raise — auth must not
    fail because the audit logger broke.
    Audit log files are created with mode 0600.
    """
    safe_fields = {
        k: v for k, v in fields.items()
        if _normalize_field_name(k) not in _REDACTED_STEMS
    }
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event.value,
        **safe_fields,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Restrictive parent directory mode.
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        # Ensure owner-only mode on the log file itself.
        if path.exists():
            try:
                path.chmod(0o600)
            except OSError:
                pass
        with _write_lock:
            # Use os.open with explicit mode so umask cannot leave
            # a world-readable audit log. Single ownership: os.fdopen
            # owns the fd and closes it on exit.
            fd = os.open(
                str(path),
                os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                0o600,
            )
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        _log.warning("dashboard-auth audit log write failed: %s", e)


def ticket_fingerprint(ticket: str) -> str:
    """One-way SHA-256-derived fingerprint for a ticket value.

    Returns the first 16 hex characters of the SHA-256 hash — a short
    deterministic identifier that can be logged to correlate mint and
    acceptance events without exposing the raw ticket.
    """
    import hashlib
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()[:16]
