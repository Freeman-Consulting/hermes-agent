"""Authenticated mobile-operations read model for the Dashboard.

Provides a versioned, secret-safe aggregation of mobile device health,
SQLite registry state, and bounded audit-event counters. Designed to be
consumed by the Dashboard operator panel and never to expose raw
credentials, credential digests, tickets, pairing codes, cookies, or
authorization material.

Schema versioning allows the client to detect contract changes without
breaking.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_cli.dashboard_auth.audit import AuditEvent
from hermes_cli.dashboard_auth.mobile_devices import (
    DeviceStoreCorrupt,
    DeviceStoreError,
    _db_path,
    _ensure_dashboard_dir,
    list_devices,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Schema version bump when response shape changes
# ---------------------------------------------------------------------------
OPS_STATUS_SCHEMA_VERSION = 2

# Bounded audit log window: only read the last N bytes to bound I/O.
# ~5000 lines at ~300 bytes each = ~1.5 MB max read.
_MAX_AUDIT_BYTES = 1_500_000

# 24-hour event window: discard events older than this.
_EVENT_WINDOW_HOURS = 24

# SQLite integrity timeout (seconds) — bounded so the route never blocks
# normal chat indefinitely.
_INTEGRITY_TIMEOUT = 5.0

# Runtime commit/version — cached at module import time.
_runtime_version: Optional[str] = None
_runtime_commit: Optional[str] = None
try:
    from hermes_cli import __version__ as _version
    _runtime_version = _version
except (ImportError, AttributeError):
    pass
try:
    import subprocess as _sub
    _commit_out = _sub.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=5,
    )
    if _commit_out.returncode == 0:
        _runtime_commit = _commit_out.stdout.strip()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Response schema (safe fields only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MobileOpsStatus:
    """Versioned mobile-operations read model.

    All fields are safe — no raw credentials, digests, tickets, pairing
    codes, cookies, or authorization material.
    """

    # Schema metadata
    schema_version: int = OPS_STATUS_SCHEMA_VERSION

    # Overall health summary
    overall_health: str = "healthy"  # healthy | degraded | unavailable

    # Runtime identification (cached at import time)
    runtime_version: Optional[str] = None
    runtime_commit: Optional[str] = None

    # Registry state
    registry_backend: str = "sqlite"
    registry_schema_version: int = SCHEMA_VERSION
    integrity_ok: bool = True
    integrity_check_ts: Optional[str] = None
    migration_state: str = "current"

    # Device counts
    active_device_count: int = 0
    revoked_device_count: int = 0

    # Bounded audit event counters (from the bounded window, 24h)
    recent_mint_count: int = 0
    recent_mint_reject_count: int = 0
    recent_expired_ticket_count: int = 0
    recent_replayed_ticket_count: int = 0
    recent_wrong_audience_count: int = 0
    recent_revoked_device_reject_count: int = 0
    recent_malformed_count: int = 0
    recent_oversized_count: int = 0
    recent_ws_accept_count: int = 0
    recent_pairing_count: int = 0
    recent_revocation_count: int = 0
    recent_rotation_count: int = 0
    recent_rotation_reject_count: int = 0
    recent_rate_limit_count: int = 0

    # Latest safe timestamps
    latest_mint_ts: Optional[str] = None
    latest_ws_accept_ts: Optional[str] = None

    # Direct /api/ws acceptance evidence
    direct_ws_accepted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Bounded audit aggregation
# ---------------------------------------------------------------------------


def _read_audit_window() -> List[Dict[str, Any]]:
    """Read the last _MAX_AUDIT_BYTES from the audit log.

    Returns an empty list if the log does not exist or is unreadable.
    Never raises — degraded state is returned by the caller.
    Bounded by bytes read: at most _MAX_AUDIT_BYTES are inspected.
    """
    from hermes_cli.dashboard_auth.audit import _resolve_log_path

    log_path = _resolve_log_path()
    if not log_path.exists():
        return []
    try:
        file_size = log_path.stat().st_size
        read_size = min(file_size, _MAX_AUDIT_BYTES)
        with open(log_path, "rb") as f:
            if read_size < file_size:
                f.seek(file_size - read_size)
                # Find the start of a line so we don't truncate mid-line
                f.read(1)  # skip the first (potentially partial) byte
            tail_bytes = f.read()
        lines = tail_bytes.decode("utf-8", errors="replace").strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    if not lines:
        return []
    result: List[Dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            try:
                result.append(json.loads(stripped))
            except (json.JSONDecodeError, ValueError):
                continue
    return result


def _aggregate_audit_events(
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Count mobile audit events and extract latest timestamps.

    Only counts events within _EVENT_WINDOW_HOURS of now.
    Returns a dict with all the counter/timestamp fields for
    MobileOpsStatus. Never leaks raw event payloads.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_EVENT_WINDOW_HOURS)
    counters: Dict[str, int] = {
        "recent_mint_count": 0,
        "recent_mint_reject_count": 0,
        "recent_expired_ticket_count": 0,
        "recent_replayed_ticket_count": 0,
        "recent_wrong_audience_count": 0,
        "recent_revoked_device_reject_count": 0,
        "recent_malformed_count": 0,
        "recent_oversized_count": 0,
        "recent_ws_accept_count": 0,
        "recent_pairing_count": 0,
        "recent_revocation_count": 0,
        "recent_rotation_count": 0,
        "recent_rotation_reject_count": 0,
        "recent_rate_limit_count": 0,
    }
    latest_mint_ts: Optional[str] = None
    latest_ws_accept_ts: Optional[str] = None
    direct_ws_accepted = False

    event_map = {
        "mobile_ticket_minted": "recent_mint_count",
        "mobile_ticket_mint_rejected": "recent_mint_reject_count",
        "mobile_ticket_expired": "recent_expired_ticket_count",
        "mobile_ticket_replayed": "recent_replayed_ticket_count",
        "mobile_ticket_wrong_audience": "recent_wrong_audience_count",
        "mobile_revoked_device_rejected": "recent_revoked_device_reject_count",
        "mobile_request_malformed": "recent_malformed_count",
        "mobile_request_oversized": "recent_oversized_count",
        "mobile_ws_accepted": "recent_ws_accept_count",
        "mobile_pairing_redeemed": "recent_pairing_count",
        "mobile_device_revoked": "recent_revocation_count",
        "mobile_credential_rotated": "recent_rotation_count",
        "mobile_credential_rotation_rejected": "recent_rotation_reject_count",
        "mobile_rate_limit_rejected": "recent_rate_limit_count",
    }

    for evt in events:
        # 24-hour window: parse ts and skip old events
        evt_ts_str = evt.get("ts")
        if evt_ts_str:
            try:
                evt_dt = datetime.fromisoformat(evt_ts_str)
                # Handle naive timestamps (treat as UTC)
                if evt_dt.tzinfo is None:
                    evt_dt = evt_dt.replace(tzinfo=timezone.utc)
                if evt_dt < cutoff:
                    continue
            except (ValueError, TypeError):
                # If we can't parse the timestamp, skip the event
                continue
        else:
            # No timestamp — skip for 24h window
            continue

        evt_name = evt.get("event", "")
        counter_key = event_map.get(evt_name)
        if counter_key:
            counters[counter_key] += 1

        # Track latest timestamps for mint and ws_accept
        if evt_name == "mobile_ticket_minted" and evt_ts_str:
            latest_mint_ts = evt_ts_str
        if evt_name == "mobile_ws_accepted" and evt_ts_str:
            latest_ws_accept_ts = evt_ts_str
            direct_ws_accepted = True

    return {
        **counters,
        "latest_mint_ts": latest_mint_ts,
        "latest_ws_accept_ts": latest_ws_accept_ts,
        "direct_ws_accepted": direct_ws_accepted,
    }


# ---------------------------------------------------------------------------
# SQLite integrity check (bounded)
# ---------------------------------------------------------------------------


def _check_integrity() -> tuple[bool, str]:
    """Run a bounded SQLite integrity check on the mobile devices DB.

    Uses a progress handler to enforce a hard execution deadline.
    Returns (ok, check_timestamp_iso). If the DB does not exist or is
    corrupt, returns (False, timestamp) rather than raising.
    """
    check_ts = datetime.now(timezone.utc).isoformat()
    db_path = _db_path()
    if not db_path.exists():
        return False, check_ts

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=_INTEGRITY_TIMEOUT)
        conn.enable_load_extension(False)
        conn.set_progress_handler(
            lambda: (_ for _ in ()).throw(TimeoutError()), 100_000
        )
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            try:
                conn.set_progress_handler(None, 0)
            except (sqlite3.ProgrammingError, TypeError):
                pass
        result = str(row[0]).lower() == "ok" if row else False
        return result, check_ts
    except (sqlite3.Error, OSError, TimeoutError):
        return False, check_ts
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def get_mobile_ops_status() -> MobileOpsStatus:
    """Build the full ops status read model.

    Safe against missing DB, corrupt DB, and large audit logs.
    Never raises DeviceStoreError to the caller — degraded state is
    returned instead. Uses cached runtime metadata (no git per request).
    """
    # Integrity check first — before list_devices() potentially creates one.
    integrity_ok, integrity_check_ts = _check_integrity()

    # Device counts
    active = 0
    revoked = 0
    try:
        devices = list_devices()
        active = sum(1 for d in devices if not d.is_revoked)
        revoked = sum(1 for d in devices if d.is_revoked)
    except DeviceStoreError:
        pass

    # Migration state
    migration_state = "unknown"
    try:
        db_path = _db_path()
        legacy_path = db_path.parent / "mobile-devices.json"
        if legacy_path.exists() and not db_path.exists():
            migration_state = "legacy-json"
        elif db_path.exists():
            migration_state = "current"
        else:
            migration_state = "no-store"
    except Exception:
        pass

    # Bounded audit aggregation
    audit_events = _read_audit_window()
    audit_agg = _aggregate_audit_events(audit_events)

    # Compute overall health
    if not integrity_ok:
        overall_health = "unavailable"
    elif migration_state != "current":
        overall_health = "degraded"
    else:
        overall_health = "healthy"

    return MobileOpsStatus(
        schema_version=OPS_STATUS_SCHEMA_VERSION,
        overall_health=overall_health,
        runtime_version=_runtime_version,
        runtime_commit=_runtime_commit,
        registry_backend="sqlite",
        registry_schema_version=SCHEMA_VERSION,
        integrity_ok=integrity_ok,
        integrity_check_ts=integrity_check_ts,
        migration_state=migration_state,
        active_device_count=active,
        revoked_device_count=revoked,
        recent_mint_count=audit_agg["recent_mint_count"],
        recent_mint_reject_count=audit_agg["recent_mint_reject_count"],
        recent_expired_ticket_count=audit_agg["recent_expired_ticket_count"],
        recent_replayed_ticket_count=audit_agg["recent_replayed_ticket_count"],
        recent_wrong_audience_count=audit_agg["recent_wrong_audience_count"],
        recent_revoked_device_reject_count=audit_agg[
            "recent_revoked_device_reject_count"
        ],
        recent_malformed_count=audit_agg["recent_malformed_count"],
        recent_oversized_count=audit_agg["recent_oversized_count"],
        recent_ws_accept_count=audit_agg["recent_ws_accept_count"],
        recent_pairing_count=audit_agg["recent_pairing_count"],
        recent_revocation_count=audit_agg["recent_revocation_count"],
        recent_rotation_count=audit_agg["recent_rotation_count"],
        recent_rotation_reject_count=audit_agg[
            "recent_rotation_reject_count"
        ],
        recent_rate_limit_count=audit_agg["recent_rate_limit_count"],
        latest_mint_ts=audit_agg["latest_mint_ts"],
        latest_ws_accept_ts=audit_agg["latest_ws_accept_ts"],
        direct_ws_accepted=audit_agg["direct_ws_accepted"],
    )
