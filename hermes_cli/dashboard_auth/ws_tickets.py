"""WS-upgrade auth credentials for gated mode.

Browsers cannot set ``Authorization`` on a WebSocket upgrade. In loopback
mode the legacy ``?token=<_SESSION_TOKEN>`` query param works because the
token is injected into the SPA bundle. In gated mode there is no injected
token — so this module provides two credential shapes:

1. **Single-use browser tickets** (``mint_ticket`` / ``consume_ticket``).
   The SPA gets a fresh ticket via the authenticated REST endpoint
   ``POST /api/auth/ws-ticket`` and passes it as ``?ticket=`` on the WS
   upgrade. Single-use, TTL = 30 seconds — a leaked ticket is uninteresting.

2. **A process-lifetime internal credential** (``internal_ws_credential`` /
   ``consume_internal_credential``). This authenticates *server-spawned*
   WS clients — specifically the embedded-TUI PTY child, which attaches to
   ``/api/ws`` (JSON-RPC gateway) and ``/api/pub`` (event sidecar) over
   loopback. A single-use 30s ticket is the wrong shape for that link: the
   child reads its attach URL once at startup and **reuses it on every
   reconnect**, and on a slow cold boot the child may not dial within 30s.
   The internal credential is minted once per process, never expires, is
   multi-use, and — critically — is **never injected into any HTML/SPA**:
   it only ever leaves the process via the spawned child's environment, so
   browser-side XSS cannot read it. A leaked internal credential grants no
   more than a single-use ticket already does (the same two internal WS
   endpoints), and the same Origin / host guards still apply downstream.

In-memory; the dashboard is a single process so no distributed coordination
is needed. The module exposes a small functional API rather than a class so
tests can patch ``time.time`` cleanly.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

#: Time-to-live for newly-minted tickets in seconds. 30 s is long enough
#: that the SPA can call ``getWsTicket()`` and immediately open the WS,
#: short enough that a leaked ticket is uninteresting.
TTL_SECONDS = 30

_lock = threading.Lock()
_tickets: Dict[str, Tuple[int, Dict[str, Any]]] = {}  # ticket -> (expires_at, info)
# Short-lived tombstones distinguish a genuine replay from an arbitrary
# unknown ticket without retaining the ticket indefinitely.
_consumed_tickets: Dict[str, int] = {}  # ticket -> tombstone expiry

#: The process-lifetime internal credential (see module docstring). Lazily
#: minted on first ``internal_ws_credential()`` call and stable for the life
#: of the process. Guarded by ``_lock``.
_internal_credential: Optional[str] = None

#: Identity recorded for connections that authenticate via the internal
#: credential, so audit logs distinguish them from browser-initiated tickets.
INTERNAL_USER_ID = "server-internal"
INTERNAL_PROVIDER = "server-internal"


class TicketInvalid(Exception):
    """Ticket missing, expired, or already consumed."""

    def __init__(self, message: str, *, reason_code: str = "") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def mint_ticket(*, user_id: str, provider: str, audience: Optional[str] = None) -> str:
    """Generate a one-shot ticket bound to this user identity.

    The returned token is base64url, 43 bytes of entropy (32-byte random
    seed). Stash returns the ``info`` dict to the caller on consume so the
    WS handler can carry the identity forward into its session log.

    ``audience`` optionally narrows where the ticket can be consumed. Browser
    session tickets omit it and remain valid for the dashboard WS family;
    mobile-device tickets set ``audience="/api/ws"`` so a phone credential
    cannot mint a ticket for PTY/console/event sockets.
    """
    ticket = secrets.token_urlsafe(32)
    info = {
        "user_id": user_id,
        "provider": provider,
        "minted_at": int(time.time()),
    }
    if audience:
        info["audience"] = audience
    with _lock:
        _tickets[ticket] = (int(time.time()) + TTL_SECONDS, info)
        _gc_expired_locked()
    return ticket


def consume_ticket(ticket: str, *, audience: Optional[str] = None) -> Dict[str, Any]:
    """Validate and consume. Raises :class:`TicketInvalid` on missing/expired/used.

    Single-use semantics: a successful consume immediately removes the
    ticket from the store, so a second call with the same value raises
    ``TicketInvalid("unknown ticket: …")``. Audience mismatches also consume
    the ticket; presenting a ticket to the wrong WS endpoint should not leave
    it reusable.
    """
    now = int(time.time())
    with _lock:
        entry = _tickets.pop(ticket, None)
        if entry is None:
            # Truncate ticket value in the error so misuse never logs the
            # secret in full.
            truncated = (ticket[:8] + "…") if ticket else "<empty>"
            reason_code = "replayed" if ticket in _consumed_tickets else "unknown"
            raise TicketInvalid(
                f"unknown ticket: {truncated}",
                reason_code=reason_code,
            )
        expires_at, info = entry
        if expires_at < now:
            raise TicketInvalid("expired", reason_code="expired")
        expected_audience = info.get("audience")
        if audience and expected_audience and expected_audience != audience:
            raise TicketInvalid("audience mismatch", reason_code="wrong_audience")
        _consumed_tickets[ticket] = now + TTL_SECONDS
        return info


def _gc_expired_locked() -> None:
    """Drop expired tickets. Caller must hold ``_lock``."""
    now = int(time.time())
    expired = [t for t, (exp, _) in _tickets.items() if exp < now]
    for t in expired:
        _tickets.pop(t, None)
    expired_tombstones = [t for t, exp in _consumed_tickets.items() if exp < now]
    for t in expired_tombstones:
        _consumed_tickets.pop(t, None)


def internal_ws_credential() -> str:
    """Return the process-lifetime internal WS credential, minting it once.

    Used by the server to authenticate WS clients it spawns itself (the
    embedded-TUI PTY child). The value is stable for the life of the process,
    multi-use, and never expires — so a server-spawned child can reconnect
    its ``/api/ws`` / ``/api/pub`` sockets indefinitely without re-minting.

    The credential is never injected into the SPA HTML or returned over any
    REST endpoint; it is only ever passed to a child process via its
    environment. See the module docstring for the threat-model rationale.
    """
    global _internal_credential
    with _lock:
        if _internal_credential is None:
            _internal_credential = secrets.token_urlsafe(32)
        return _internal_credential


def consume_internal_credential(value: str) -> Dict[str, Any]:
    """Validate an internal credential. Raises :class:`TicketInvalid` on mismatch.

    Unlike :func:`consume_ticket` this is **not** single-use — the value is
    not removed on success, so a server-spawned child can present it on every
    (re)connect. Returns the fixed server-internal identity ``info`` dict
    (``{user_id, provider}``), mirroring the ``info`` shape ``consume_ticket``
    returns, so a caller that wants to record the connecting identity can; the
    current ``_ws_auth_ok`` caller validates for the boolean outcome only and
    discards the dict.

    A constant-time compare against the (lazily-minted) credential avoids
    leaking length / prefix information on mismatch. If no internal
    credential has been minted yet, any value is rejected.
    """
    with _lock:
        expected = _internal_credential
    if not value or expected is None:
        raise TicketInvalid("no internal credential")
    if not secrets.compare_digest(value.encode(), expected.encode()):
        raise TicketInvalid("internal credential mismatch")
    return {
        "user_id": INTERNAL_USER_ID,
        "provider": INTERNAL_PROVIDER,
    }


def purge_mobile_tickets(*, user_id: str) -> int:
    """Remove outstanding in-process tickets for a specific mobile user_id.

    Preserves browser/internal tickets: only tickets whose info user_id
    matches the given value are purged. Returns the count of purged tickets.
    """
    with _lock:
        to_remove = [
            ticket for ticket, (_exp, info) in _tickets.items()
            if info.get("user_id") == user_id
        ]
        for ticket in to_remove:
            _tickets.pop(ticket, None)
        return len(to_remove)


def _reset_for_tests() -> None:
    """Test-only: drop all tickets and the internal credential."""
    global _internal_credential
    with _lock:
        _tickets.clear()
        _consumed_tickets.clear()
        _internal_credential = None
