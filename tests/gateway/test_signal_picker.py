"""Unit tests for ``gateway.model_picker_state.ModelPickerState``.

Tests cover: add/lookup, TTL expiry, cancel/expiry, pagination helpers,
and index resolution.  No adapter or gateway runner is involved.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from gateway.model_picker_state import (
    PAGE_SIZE,
    PICKER_TTL_SECONDS,
    ModelPickerState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_PROVIDERS = [
    {
        "slug": "anthropic",
        "name": "Anthropic",
        "models": ["claude-opus-4-6", "claude-sonnet-4-0", "claude-haiku-4-5"],
    },
    {
        "slug": "openai",
        "name": "OpenAI",
        "models": ["gpt-4o", "o3-pro"],
    },
]

PLATFORM = "signal"
CHAT_ID = "+15551234567"
SESSION_KEY = "sess_abc123"


@pytest.fixture()
def store():
    """Return a fresh ModelPickerState for each test."""
    return ModelPickerState()


@pytest.fixture()
def callback():
    cb = MagicMock(return_value="done")
    return cb


# ---------------------------------------------------------------------------
# Add / Lookup
# ---------------------------------------------------------------------------

class TestAddLookup:
    def test_add_returns_canonical_key(self, store):
        key = store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "claude-opus-4-6", "anthropic")
        assert key == (PLATFORM, CHAT_ID, SESSION_KEY)

    def test_lookup_returns_entry(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "claude-opus-4-6", "anthropic", on_model_selected=callback)
        entry = store.lookup(PLATFORM, CHAT_ID, SESSION_KEY)
        assert entry is not None
        assert entry.current_model == "claude-opus-4-6"
        assert entry.current_provider == "anthropic"
        assert entry.providers is FAKE_PROVIDERS

    def test_lookup_missing_returns_none(self, store):
        assert store.lookup(PLATFORM, CHAT_ID, SESSION_KEY) is None

    def test_overwrite_replaces_entry(self, store, callback):
        cb1 = MagicMock()
        cb2 = MagicMock()
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "model-a", "p1", on_model_selected=cb1)
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "model-b", "p2", on_model_selected=cb2)
        entry = store.lookup(PLATFORM, CHAT_ID, SESSION_KEY)
        assert entry.current_model == "model-b"
        assert entry.current_provider == "p2"

    def test_has_pending_true(self, store):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p")
        assert store.has_pending(PLATFORM, CHAT_ID, SESSION_KEY) is True

    def test_has_pending_false_missing(self, store):
        assert store.has_pending(PLATFORM, CHAT_ID, SESSION_KEY) is False


# ---------------------------------------------------------------------------
# TTL Expiry (lazy eviction)
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_lookup_expired_returns_none(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        # Advance past TTL using monotonic clock (matches production)
        store._store[(PLATFORM, CHAT_ID, SESSION_KEY)].created_at = time.monotonic() - PICKER_TTL_SECONDS - 1
        assert store.lookup(PLATFORM, CHAT_ID, SESSION_KEY) is None

    def test_expired_entry_removed_from_store(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        store._store[(PLATFORM, CHAT_ID, SESSION_KEY)].created_at = time.monotonic() - PICKER_TTL_SECONDS - 1
        store.lookup(PLATFORM, CHAT_ID, SESSION_KEY)
        assert (PLATFORM, CHAT_ID, SESSION_KEY) not in store._store

    def test_non_expired_still_valid(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        # created_at is default (now), so definitely not expired
        assert store.lookup(PLATFORM, CHAT_ID, SESSION_KEY) is not None

    def test_multiple_entries_independent_expiry(self, store, callback):
        key_a = store.add("signal", "chat_a", "sess_a", FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        key_b = store.add("signal", "chat_b", "sess_b", FAKE_PROVIDERS, "m", "p", on_model_selected=callback)

        # Expire only A
        store._store[key_a].created_at = time.monotonic() - PICKER_TTL_SECONDS - 1

        assert store.lookup("signal", "chat_a", "sess_a") is None
        assert store.lookup("signal", "chat_b", "sess_b") is not None

    def test_add_then_immediate_lookup_not_expired(self, store, callback):
        """Real-time test: add, lookup without any time manipulation, expect entry."""
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        entry = store.lookup(PLATFORM, CHAT_ID, SESSION_KEY)
        assert entry is not None
        assert not entry.is_expired()


# ---------------------------------------------------------------------------
# Cancel / Expire
# ---------------------------------------------------------------------------

class TestCancelExpire:
    def test_cancel_returns_entry(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        evicted = store.cancel(PLATFORM, CHAT_ID, SESSION_KEY)
        assert evicted is not None
        assert evicted.current_model == "m"

    def test_cancel_on_missing_returns_none(self, store):
        assert store.cancel(PLATFORM, CHAT_ID, SESSION_KEY) is None

    def test_cancel_removes_from_store(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        store.cancel(PLATFORM, CHAT_ID, SESSION_KEY)
        assert (PLATFORM, CHAT_ID, SESSION_KEY) not in store._store

    def test_expire_returns_entry(self, store, callback):
        store.add(PLATFORM, CHAT_ID, SESSION_KEY, FAKE_PROVIDERS, "m", "p", on_model_selected=callback)
        evicted = store.expire(PLATFORM, CHAT_ID, SESSION_KEY)
        assert evicted is not None

    def test_expire_on_missing_returns_none(self, store):
        assert store.expire(PLATFORM, CHAT_ID, SESSION_KEY) is None


# ---------------------------------------------------------------------------
# Pagination helpers (static)
# ---------------------------------------------------------------------------

class TestPagination:
    def test_paginate_models_basic(self):
        result = ModelPickerState.paginate_models(FAKE_PROVIDERS, page=0)
        assert result["total_models"] == 5  # 3 + 2
        assert result["total_pages"] == 1   # 5 models, PAGE_SIZE=12 → 1 page
        assert len(result["models"]) == 5
        assert result["has_next"] is False
        assert result["has_prev"] is False

    def test_paginate_models_clamps_page(self):
        result = ModelPickerState.paginate_models(FAKE_PROVIDERS, page=999)
        assert result["page"] == 0  # clamped to last valid page

    def test_paginate_models_negative_page(self):
        result = ModelPickerState.paginate_models(FAKE_PROVIDERS, page=-1)
        assert result["page"] == 0

    def test_paginate_models_empty_providers(self):
        result = ModelPickerState.paginate_models([], page=0)
        assert result["total_models"] == 0
        assert result["total_pages"] == 1
        assert len(result["models"]) == 0

    def test_paginate_models_with_pagination_needed(self):
        big_providers = [
            {"slug": f"p{i}", "name": f"Provider {i}", "models": [f"model-{i}-{j}" for j in range(15)]}
            for i in range(3)
        ]
        # 45 models, PAGE_SIZE=12 → 4 pages
        result = ModelPickerState.paginate_models(big_providers, page=0)
        assert result["total_pages"] == 4
        assert len(result["models"]) == 12
        assert result["has_next"] is True

        result_p1 = ModelPickerState.paginate_models(big_providers, page=1)
        assert result_p1["page"] == 1
        assert len(result_p1["models"]) == 12
        assert result_p1["has_prev"] is True
        assert result_p1["has_next"] is True

    def test_paginate_models_last_page(self):
        big_providers = [
            {"slug": f"p{i}", "name": f"Provider {i}", "models": [f"model-{i}-{j}" for j in range(15)]}
            for i in range(3)
        ]
        result_last = ModelPickerState.paginate_models(big_providers, page=3)
        assert result_last["page"] == 3
        # 45 - 36 = 9 models on last page
        assert len(result_last["models"]) == 9
        assert result_last["has_next"] is False


# ---------------------------------------------------------------------------
# Index resolution
# ---------------------------------------------------------------------------

class TestResolveModelIndex:
    def test_resolve_valid_index(self):
        model_id, provider_name = ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 0)
        assert model_id == "claude-opus-4-6"
        assert provider_name == "Anthropic"

    def test_resolve_last_index(self):
        model_id, provider_name = ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 4)
        assert model_id == "o3-pro"
        assert provider_name == "OpenAI"

    def test_resolve_out_of_range_returns_none(self):
        assert ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 5) is None
        assert ModelPickerState.resolve_model_index(FAKE_PROVIDERS, -1) is None

    def test_resolve_empty_providers(self):
        assert ModelPickerState.resolve_model_index([], 0) is None

    def test_resolve_model_index_past_first_page(self):
        """Regression: flat index >= PAGE_SIZE must resolve correctly.

        Before the fix, resolve_model_index only checked page 0's models,
        so any index >= 12 returned None even though more models existed.
        """
        big_providers = [
            {"slug": f"p{i}", "name": f"Provider {i}",
             "models": [f"m-{i}-{j}" for j in range(15)]}
            for i in range(3)
        ]
        # Flat index 13 should resolve to the 14th model across all providers
        result = ModelPickerState.resolve_model_index(big_providers, 13)
        assert result is not None
        model_id, _ = result
        assert model_id == "m-0-13"  # 14th model overall (index 13)

    def test_resolve_model_index_last_model(self):
        """Flat index at the very end of the list."""
        big_providers = [
            {"slug": f"p{i}", "name": f"Provider {i}",
             "models": [f"m-{i}-{j}" for j in range(15)]}
            for i in range(3)
        ]
        # Total 45 models, last index is 44
        result = ModelPickerState.resolve_model_index(big_providers, 44)
        assert result is not None
        model_id, provider_name = result
        assert model_id == "m-2-14"
        assert provider_name == "Provider 2"

    def test_resolve_model_index_boundary_zero(self):
        """Index 0 should work (not just positive indices)."""
        result = ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 0)
        assert result is not None
        model_id, _ = result
        assert model_id == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Default callback
# ---------------------------------------------------------------------------

class TestDefaultCallback:
    def test_default_callback_returns_string(self):
        result = ModelPickerState._default_callback("+123", "model-x", "provider-y")
        assert result == "Selected model-x (provider-y)"


# ---------------------------------------------------------------------------
# Typing indicator context manager (run.py)
# ---------------------------------------------------------------------------


class TestTypingDuringCommand:
    """Tests for ``typing_during_command`` async context manager."""

    @pytest_asyncio.fixture
    async def adapter_with_typing(self):
        """Adapter that exposes send_typing and stop_typing."""
        adapter = MagicMock(spec_set=["send_typing", "stop_typing"])
        adapter.send_typing = AsyncMock()
        adapter.stop_typing = AsyncMock()
        return adapter

    @pytest_asyncio.fixture
    async def adapter_without_typing(self):
        """Adapter with no typing methods at all."""
        adapter = MagicMock(spec_set=[])
        return adapter

    @pytest_asyncio.fixture
    async def adapter_with_partial_typing(self):
        """Adapter that has send_typing but not stop_typing."""
        adapter = MagicMock(spec_set=["send_typing"])
        adapter.send_typing = AsyncMock()
        return adapter

    @pytest.mark.asyncio
    async def test_calls_send_and_stop(self, adapter_with_typing):
        cm = self._import_cm()(adapter_with_typing, "+123")
        async with cm:
            pass
        adapter_with_typing.send_typing.assert_called_once_with("+123")
        adapter_with_typing.stop_typing.assert_called_once_with("+123")

    @pytest.mark.asyncio
    async def test_noop_when_no_typing_methods(self, adapter_without_typing):
        """No crash when adapter has no send/stop typing."""
        cm = self._import_cm()(adapter_without_typing, "+123")
        async with cm:
            pass  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_only_send_typing(self, adapter_with_partial_typing):
        """No crash when adapter has send_typing but no stop_typing."""
        cm = self._import_cm()(adapter_with_partial_typing, "+123")
        async with cm:
            pass
        adapter_with_partial_typing.send_typing.assert_called_once_with("+123")

    @pytest.mark.asyncio
    async def test_send_typing_exception_suppressed(self):
        """A failing send_typing does not kill the command."""
        adapter = MagicMock(spec_set=["send_typing", "stop_typing"])
        adapter.send_typing = AsyncMock(side_effect=RuntimeError("oops"))
        adapter.stop_typing = AsyncMock()

        cm = self._import_cm()(adapter, "+123")
        async with cm:
            pass  # should not raise

    @pytest.mark.asyncio
    async def test_stop_typing_exception_suppressed(self):
        """A failing stop_typing does not kill the command."""
        adapter = MagicMock(spec_set=["send_typing", "stop_typing"])
        adapter.send_typing = AsyncMock()
        adapter.stop_typing = AsyncMock(side_effect=RuntimeError("oops"))

        cm = self._import_cm()(adapter, "+123")
        async with cm:
            pass  # should not raise

    @staticmethod
    def _import_cm():
        """Lazy import to avoid importing run.py at module load time."""
        from gateway.run import typing_during_command
        return typing_during_command


# ---------------------------------------------------------------------------
# Fuzzy command suggestion helper (run.py)
# ---------------------------------------------------------------------------


class TestSuggestCommand:
    """Tests for ``suggest_command`` fuzzy matching helper."""

    @staticmethod
    def _import_fn():
        from gateway.run import suggest_command
        return suggest_command

    def test_typo_suggests_model(self):
        fn = self._import_fn()
        result = fn("/modle")
        assert result == "/model"

    def test_typo_suggests_help(self):
        fn = self._import_fn()
        result = fn("/hlep")
        # "hlep" is close enough to "help" with default threshold
        assert result == "/help"

    def test_garbage_returns_none(self):
        fn = self._import_fn()
        result = fn("xyzzyfoobar")
        assert result is None

    def test_correct_command_returns_itself(self):
        fn = self._import_fn()
        result = fn("/help")
        assert result == "/help"

    def test_empty_string_returns_none(self):
        fn = self._import_fn()
        assert fn("") is None

    def test_whitespace_only_returns_none(self):
        fn = self._import_fn()
        assert fn("   ") is None

    def test_no_slash_prefix_still_matches(self):
        fn = self._import_fn()
        result = fn("modle")
        assert result == "/model"

    def test_high_threshold_stricter(self):
        fn = self._import_fn()
        # With a very high threshold, "modle" might not match "model"
        result = fn("/modle", threshold=0.95)
        # difflib.get_close_matches cutoff=0.95 — "modle" vs "model"
        # ratio ≈ 0.8, so this should return None
        assert result is None
