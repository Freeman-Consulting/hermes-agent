"""Concrete pending-picker state store for the /model command.

This module owns the in-memory state for active model pickers on chat
platforms that support interactive selection (Signal, Telegram, Discord).
It is a plain data class -- no adapter coupling, no async I/O -- so it
can be unit-tested in isolation without spinning up a GatewayRunner.

Keyed by (platform, chat_id, session_key).  Each entry has a 120-second
TTL with lazy eviction: expired entries are returned by ``expire()`` and
removed on next ``lookup()`` or ``cancel()``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

PICKER_TTL_SECONDS: int = 120
"""How long a pending picker lives before expiring (seconds)."""

PAGE_SIZE: int = 12
"""Number of models rendered per picker page."""


# ---------------------------------------------------------------------------
# Core data class
# ---------------------------------------------------------------------------

@dataclass
class _PickerEntry:
    """Single pending-picker entry, keyed by platform+chat+session."""

    providers: list
    """Flattened provider/model list as returned by ``list_authenticated_providers``."""
    current_model: str
    """Model the user is currently running."""
    current_provider: str
    """Provider slug the user is currently on."""
    session_key: str
    """Hermes session key so we can look up the agent cache later."""
    on_model_selected: Callable[[str, str, str], Any]
    """Callback invoked when the user picks a model (index or name)."""
    current_base_url: Optional[str] = None
    """Base URL for the current provider (if custom)."""
    current_api_key: Optional[str] = None
    """API key for the current provider."""
    created_at: float = field(default_factory=time.monotonic)
    """Monotonic timestamp when this picker was created."""
    current_page: int = 0
    """Current page index (0-based) for pagination. Updated on 'more' replies."""

    def is_expired(self) -> bool:
        """Return True if this entry has exceeded PICKER_TTL_SECONDS."""
        return (time.monotonic() - self.created_at) >= PICKER_TTL_SECONDS


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------

# Key type: (platform_name_str, chat_id, session_key)
_PickerKey = Tuple[str, str, str]


class ModelPickerState:
    """In-memory store for active /model pickers.

    Thread-unsafe by design -- the GatewayRunner is single-threaded (asyncio),
    so no locks are needed.  If a sync runner ever uses this, add a lock.
    """

    def __init__(self) -> None:
        self._store: Dict[_PickerKey, _PickerEntry] = {}

    # -- Lifecycle ----------------------------------------------------------

    def add(
        self,
        platform: str,
        chat_id: str,
        session_key: str,
        providers: list,
        current_model: str,
        current_provider: str,
        current_base_url: Optional[str] = None,
        current_api_key: Optional[str] = None,
        on_model_selected: Optional[Callable] = None,
        current_page: int = 0,
    ) -> _PickerKey:
        """Register a new pending picker.

        Returns the canonical key so callers can track it.
        Overwrites any existing entry for the same (platform, chat_id, session_key).
        """
        key: _PickerKey = (platform, chat_id, session_key)
        self._store[key] = _PickerEntry(
            providers=providers,
            current_model=current_model,
            current_provider=current_provider,
            current_base_url=current_base_url,
            current_api_key=current_api_key,
            session_key=session_key,
            on_model_selected=(
                on_model_selected
                if on_model_selected is not None
                else self._default_callback
            ),
            current_page=current_page,
        )
        return key

    def lookup(
        self, platform: str, chat_id: str, session_key: str
    ) -> Optional[_PickerEntry]:
        """Return the picker entry or None.

        Lazily evicts expired entries on lookup.
        """
        key: _PickerKey = (platform, chat_id, session_key)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[key]
            return None
        return entry

    def expire(self, platform: str, chat_id: str, session_key: str) -> Optional[_PickerEntry]:
        """Force-expire and remove a pending picker.

        Returns the evicted entry (or None if it didn't exist).
        """
        key: _PickerKey = (platform, chat_id, session_key)
        return self._store.pop(key, None)

    def cancel(self, platform: str, chat_id: str, session_key: str) -> Optional[_PickerEntry]:
        """Cancel a pending picker (user sent a /command or explicit cancel).

        Same behaviour as expire -- returns the evicted entry or None.
        """
        return self.expire(platform, chat_id, session_key)

    def has_pending(
        self, platform: str, chat_id: str, session_key: str
    ) -> bool:
        """Return True if a non-expired picker exists for this triple."""
        return self.lookup(platform, chat_id, session_key) is not None

    # -- Expiry detection ---------------------------------------------------

    def check_expired(
        self, platform: str, chat_id: str, session_key: str
    ) -> Optional[_PickerEntry]:
        """Return the entry if it exists AND is expired. Otherwise None. Evicts."""
        key: _PickerKey = (platform, chat_id, session_key)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[key]
            return entry
        return None

    # -- Pagination helpers -------------------------------------------------

    @staticmethod
    def paginate_models(
        providers: list, page: int = 0
    ) -> dict:
        """Split a flat provider list into numbered models for display.

        Returns a dict with keys:
            - models: list of (index, model_id, provider_name) tuples for this page
            - page: current page number (0-indexed)
            - total_pages: total number of pages needed
            - total_models: total count across all providers
            - has_next: whether a next page exists
            - has_prev: whether a previous page exists
        """
        # Flatten to (index, model_id, provider_name) tuples
        flat: list[tuple[int, str, str]] = []
        for prov in providers:
            pname = prov.get("name", prov.get("slug", "unknown"))
            for mid in prov.get("models", []):
                flat.append((len(flat), mid, pname))

        total = len(flat)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start = page * PAGE_SIZE
        end = min(start + PAGE_SIZE, total)
        page_models = flat[start:end]

        return {
            "models": page_models,
            "page": page,
            "total_pages": total_pages,
            "total_models": total,
            "has_next": page + 1 < total_pages,
            "has_prev": page > 0,
        }

    @staticmethod
    def resolve_model_index(
        providers: list, index: int
    ) -> Optional[tuple[str, str]]:
        """Resolve a flat index back to (model_id, provider_name).

        The ``index`` parameter is **0-indexed** against the full flattened
        model list across all providers.  This means if page 0 shows models
        0–11 and page 1 shows models 12–23, a user reply of "3" from page 1
        (meaning the 3rd model on that page) resolves to flat index 14
        (12 + 2).

        Returns ``(model_id, provider_slug)`` or ``None`` if index is out
        of range.

        .. note:: Returns the **slug** (not display name) because callers
           pass this to ``_switch_model(explicit_provider=...)`` which
           requires a config-level provider slug.
        """
        flat: list[tuple[str, str]] = []
        for prov in providers:
            slug = prov.get("slug", prov.get("name", "unknown"))
            for mid in prov.get("models", []):
                flat.append((mid, slug))
        if 0 <= index < len(flat):
            return flat[index]
        return None

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _default_callback(chat_id: str, model_id: str, provider_slug: str) -> str:
        """No-op default callback when none is provided."""
        return f"Selected {model_id} ({provider_slug})"
