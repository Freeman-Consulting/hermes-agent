"""Sliding-window rate limiter for mobile public credential endpoints.

Per-client and per-device monotonic sliding windows using ``time.monotonic()``.
No external dependencies. Thread-safe via ``threading.Lock``.

Design rules:
- Rate-limit BEFORE credential validation so invalid guesses consume budget.
- Do not trust ``X-Forwarded-For``; use only the ASGI-normalized
  ``request.client.host``.
- Pair creation, admin list, and admin revoke are NOT rate-limited here.
- Deterministic clock injection is provided for tests without weakening
  production behavior.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Budgets (attempts per window)
# ---------------------------------------------------------------------------

# Pair redemption: 10 per 60s per client host
PAIR_MAX_PER_CLIENT = 10
PAIR_WINDOW_SEC = 60

# Ticket mint: 60 per 60s per normalized device id, 120 per 60s per client host
TICKET_MAX_PER_DEVICE = 60
TICKET_MAX_PER_CLIENT = 120
TICKET_WINDOW_SEC = 60

# Credential rotation: 5 per 3600s per normalized device id, 10 per 3600s per client host
ROTATE_MAX_PER_DEVICE = 5
ROTATE_MAX_PER_CLIENT = 10
ROTATE_WINDOW_SEC = 3600


class _Clock:
    """Thin clock wrapper so tests can inject deterministic time."""

    @staticmethod
    def now() -> float:
        return time.monotonic()


_clock: _Clock = _Clock()


def _set_clock_for_tests(clock: _Clock) -> None:
    """Replace the global clock with ``clock`` for deterministic tests."""
    global _clock  # noqa: PLW0603
    _clock = clock


def _reset_clock_for_tests() -> None:
    """Restore the default monotonic clock."""
    global _clock  # noqa: PLW0603
    _clock = _Clock()


# ---------------------------------------------------------------------------
# Sliding-window bucket
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# key -> deque of monotonic timestamps
# Namespaced by operation so pair/ticket/rotation budgets are independent.
_client_buckets_pair: Dict[str, Deque[float]] = defaultdict(deque)
_client_buckets_ticket: Dict[str, Deque[float]] = defaultdict(deque)
_client_buckets_rotate: Dict[str, Deque[float]] = defaultdict(deque)

_device_buckets_ticket: Dict[str, Deque[float]] = defaultdict(deque)
_device_buckets_rotate: Dict[str, Deque[float]] = defaultdict(deque)


def _prune(bucket: Deque[float], now: float, window: float) -> None:
    """Remove timestamps older than ``window`` seconds."""
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def _check_and_record(
    bucket: Deque[float],
    now: float,
    window: float,
    max_attempts: int,
) -> Tuple[bool, int]:
    """Check budget and record the attempt if allowed.

    Returns ``(allowed, retry_after_seconds)``.
    When allowed is True, retry_after_seconds is 0.
    When allowed is False, retry_after_seconds is the integer number of
    seconds until the oldest timestamp in the window expires (minimum 1).
    """
    _prune(bucket, now, window)
    if len(bucket) >= max_attempts:
        # Over budget — compute retry-after from the oldest entry.
        oldest = bucket[0]
        retry_after = max(1, int(oldest + window - now) + 1)
        return False, retry_after
    bucket.append(now)
    return True, 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_pair_redemption(
    client_host: str,
) -> Tuple[bool, int]:
    """Check pair redemption rate limit per client host.

    Returns ``(allowed, retry_after)``.
    """
    now = _clock.now()
    key = client_host or "_unknown_"
    with _lock:
        return _check_and_record(_client_buckets_pair[key], now, PAIR_WINDOW_SEC, PAIR_MAX_PER_CLIENT)


def check_ticket_mint(
    client_host: str,
    device_id: str,
) -> Tuple[bool, int]:
    """Check ticket mint rate limit per client host AND per device id.

    Returns ``(allowed, retry_after)``.  The request is rejected if EITHER
    bucket is over budget.  When both are within budget, the attempt is
    recorded in both.
    """
    now = _clock.now()
    client_key = client_host or "_unknown_"
    device_key = device_id or "_unknown_"
    with _lock:
        # Check both buckets first without recording.
        client_bucket = _client_buckets_ticket[client_key]
        device_bucket = _device_buckets_ticket[device_key]
        _prune(client_bucket, now, TICKET_WINDOW_SEC)
        _prune(device_bucket, now, TICKET_WINDOW_SEC)

        client_over = len(client_bucket) >= TICKET_MAX_PER_CLIENT
        device_over = len(device_bucket) >= TICKET_MAX_PER_DEVICE

        if client_over or device_over:
            # Compute worst-case retry-after.
            retry_after = 1
            if client_over and client_bucket:
                retry_after = max(retry_after, max(1, int(client_bucket[0] + TICKET_WINDOW_SEC - now) + 1))
            if device_over and device_bucket:
                retry_after = max(retry_after, max(1, int(device_bucket[0] + TICKET_WINDOW_SEC - now) + 1))
            return False, retry_after

        # Both within budget -- record in both.
        client_bucket.append(now)
        device_bucket.append(now)
        return True, 0


def check_credential_rotation(
    client_host: str,
    device_id: str,
) -> Tuple[bool, int]:
    """Check credential rotation rate limit per client host AND per device id.

    Returns ``(allowed, retry_after)``.
    """
    now = _clock.now()
    client_key = client_host or "_unknown_"
    device_key = device_id or "_unknown_"
    with _lock:
        client_bucket = _client_buckets_rotate[client_key]
        device_bucket = _device_buckets_rotate[device_key]
        _prune(client_bucket, now, ROTATE_WINDOW_SEC)
        _prune(device_bucket, now, ROTATE_WINDOW_SEC)

        client_over = len(client_bucket) >= ROTATE_MAX_PER_CLIENT
        device_over = len(device_bucket) >= ROTATE_MAX_PER_DEVICE

        if client_over or device_over:
            retry_after = 1
            if client_over and client_bucket:
                retry_after = max(retry_after, max(1, int(client_bucket[0] + ROTATE_WINDOW_SEC - now) + 1))
            if device_over and device_bucket:
                retry_after = max(retry_after, max(1, int(device_bucket[0] + ROTATE_WINDOW_SEC - now) + 1))
            return False, retry_after

        client_bucket.append(now)
        device_bucket.append(now)
        return True, 0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Test-only: clear all rate-limit buckets and restore the default clock."""
    with _lock:
        _client_buckets_pair.clear()
        _client_buckets_ticket.clear()
        _client_buckets_rotate.clear()
        _device_buckets_ticket.clear()
        _device_buckets_rotate.clear()
    _reset_clock_for_tests()
