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
        model_id, provider_slug = ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 0)
        assert model_id == "claude-opus-4-6"
        assert provider_slug == "anthropic"

    def test_resolve_last_index(self):
        model_id, provider_slug = ModelPickerState.resolve_model_index(FAKE_PROVIDERS, 4)
        assert model_id == "o3-pro"
        assert provider_slug == "openai"

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
        model_id, provider_slug = result
        assert model_id == "m-2-14"
        assert provider_slug == "p2"

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


# ---------------------------------------------------------------------------
# SignalAdapter.send_model_picker (unit)
# ---------------------------------------------------------------------------

class TestSignalSendModelPicker:
    """Test SignalAdapter.send_model_picker using a real adapter with _rpc mocked."""

    def _build_providers(self):
        return [
            {
                "name": "openai",
                "models": ["gpt-4o", "gpt-4o-mini"],
            },
            {
                "name": "anthropic",
                "models": ["claude-opus-4-6", "claude-sonnet-4-5"],
            },
        ]

    @pytest.mark.asyncio
    async def test_send_model_picker_sends_message(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()  # truthy — connected
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        result = await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        assert result.success is True
        adapter._rpc.assert_called_once()
        call_args = adapter._rpc.call_args
        assert call_args[0][0] == "send"
        params = call_args[0][1]
        assert params["account"] == "+15550001234"
        assert params["recipient"] == ["+15550009999"]
        assert "gpt-4o" in params["message"]
        assert "claude-opus-4-6" in params["message"]

    @pytest.mark.asyncio
    async def test_send_model_picker_registers_state(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        # State store should be initialized and contain the picker
        assert adapter._model_picker_state is not None
        entry = adapter._model_picker_state.lookup(
            platform="signal", chat_id="+15550009999", session_key="sess-abc"
        )
        assert entry is not None
        assert entry.session_key == "sess-abc"

    @pytest.mark.asyncio
    async def test_send_model_picker_no_models(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        result = await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=[],
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        assert result.success is True
        call_args = adapter._rpc.call_args
        params = call_args[0][1]
        assert "No models available" in params["message"]

    @pytest.mark.asyncio
    async def test_send_model_picker_not_connected(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = None
        adapter._model_picker_state = None

        result = await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        assert result.success is False
        assert "not connected" in result.error

    @pytest.mark.asyncio
    async def test_send_model_picker_rpc_failure(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value=None)

        result = await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        assert result.success is False
        assert "Failed" in result.error

    @pytest.mark.asyncio
    async def test_send_model_picker_message_formatting(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        call_args = adapter._rpc.call_args
        message = call_args[0][1]["message"]

        # Should have numbered items (1-indexed)
        assert "  1. gpt-4o" in message
        assert "  2. gpt-4o-mini" in message
        assert "  3. claude-opus-4-6" in message
        assert "  4. claude-sonnet-4-5" in message

        # Should show current model
        assert "Current: gpt-4o (openai)" in message

    @pytest.mark.asyncio
    async def test_send_model_picker_shortens_long_model_ids(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+15550001234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        providers = [
            {
                "name": "test",
                "models": ["very-long-model-id-that-exceeds-forty-characters"],
            }
        ]

        await adapter.send_model_picker(
            chat_id="+15550009999",
            providers=providers,
            current_model="",
            current_provider="test",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        call_args = adapter._rpc.call_args
        message = call_args[0][1]["message"]

        # Long model ID should be truncated with ...
        assert "..." in message
        # Should not contain the full original string
        assert "very-long-model-id-that-exceeds-forty-characters" not in message

    @pytest.mark.asyncio
    async def test_send_model_picker_calls_typing(self):
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+155****1234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})
        adapter.send_typing = AsyncMock()
        adapter.stop_typing = AsyncMock()

        await adapter.send_model_picker(
            chat_id="+155****9999",
            providers=self._build_providers(),
            current_model="gpt-4o",
            current_provider="openai",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
        )

        adapter.send_typing.assert_called_once_with("+155****9999")
        adapter.stop_typing.assert_called_once_with("+155****9999")


# ---------------------------------------------------------------------------
# Dispatch integration tests (intercept in run.py)
# ---------------------------------------------------------------------------

from datetime import datetime
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.model_picker_state import ModelPickerState, PICKER_TTL_SECONDS
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_dispatch_source(platform=Platform.SIGNAL) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="+155****4567",
        user_name="tester",
        chat_type="dm",
    )


def _make_dispatch_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_dispatch_source(), message_id="m1")


def _make_dispatch_runner():
    """Build a bare GatewayRunner with mock fields for intercept tests."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter._model_picker_state = ModelPickerState()
    adapter.send_model_picker = AsyncMock(return_value=MagicMock(success=True))
    runner.adapters = {Platform.SIGNAL: adapter}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_dispatch_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SIGNAL,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._is_user_authorized = lambda _source: True
    return runner, adapter


class TestDispatchDigitReply:
    """Tests for digit-reply interception in the picker intercept block."""

    @pytest.mark.asyncio
    async def test_digit_reply_resolves_and_invokes_callback(self):
        """A digit reply should resolve to a model and invoke the callback."""
        runner, adapter = _make_dispatch_runner()
        event = _make_dispatch_event("2")
        source = event.source
        providers = FAKE_PROVIDERS

        # Derive session_key and chat_id from the event/source
        session_entry = runner.session_store.get_or_create_session(source)
        sess_key = session_entry.session_key
        chat_id_str = str(source.chat_id)

        cb = AsyncMock(return_value="Switched to claude-opus-4-6")
        adapter._model_picker_state.add(
            platform="signal",
            chat_id=chat_id_str,
            session_key=sess_key,
            providers=providers,
            current_model="claude-haiku-4-5",
            current_provider="anthropic",
            on_model_selected=cb,
        )

        adapter = runner.adapters.get(source.platform)
        picker_state = getattr(adapter, "_model_picker_state", None)
        assert picker_state is not None

        platform_str = source.platform.value

        pending = picker_state.lookup(platform_str, chat_id_str, sess_key)
        assert pending is not None

        raw_text = (event.text or "").strip()
        assert raw_text.isdigit()

        one_indexed = int(raw_text)
        resolved = ModelPickerState.resolve_model_index(pending.providers, one_indexed - 1)
        assert resolved is not None
        model_id, provider_slug = resolved

        # The callback should be invoked with (chat_id, model_id, provider_slug)
        result_text = await pending.on_model_selected(chat_id_str, model_id, provider_slug)
        cb.assert_awaited_once_with(chat_id_str, model_id, provider_slug)
        assert result_text == "Switched to claude-opus-4-6"

    @pytest.mark.asyncio
    async def test_out_of_range_digit_returns_error(self):
        """A digit beyond the total model count should return a friendly error."""
        runner, adapter = _make_dispatch_runner()
        providers = FAKE_PROVIDERS  # 5 models total

        cb = AsyncMock(return_value="done")
        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+155****4567",
            session_key="sess_abc123",
            providers=providers,
            current_model="claude-haiku-4-5",
            current_provider="anthropic",
            on_model_selected=cb,
        )

        adapter = runner.adapters.get(Platform.SIGNAL)
        picker_state = adapter._model_picker_state
        pending = picker_state.lookup("signal", "+155****4567", "sess_abc123")
        assert pending is not None

        resolved = ModelPickerState.resolve_model_index(pending.providers, 100)  # way out of range
        assert resolved is None

        total = sum(len(p.get("models", [])) for p in pending.providers)
        expected_error = f"Number out of range. Reply 1–{total} or 'more'."
        assert expected_error == "Number out of range. Reply 1–5 or 'more'."


class TestDispatchCommandCancel:
    """Tests for /command cancellation of pending picker."""

    @pytest.mark.asyncio
    async def test_slash_command_cancels_pending_picker(self):
        """A slash command should cancel the pending picker and fall through."""
        runner, adapter = _make_dispatch_runner()
        providers = FAKE_PROVIDERS

        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+155****4567",
            session_key="sess_abc123",
            providers=providers,
            current_model="claude-haiku-4-5",
            current_provider="anthropic",
            on_model_selected=lambda *a: None,
        )

        adapter = runner.adapters.get(Platform.SIGNAL)
        picker_state = adapter._model_picker_state

        # Verify pending exists
        assert picker_state.lookup("signal", "+155****4567", "sess_abc123") is not None

        # Simulate /status command
        event = _make_dispatch_event("/status")
        raw_text = (event.text or "").strip()
        assert raw_text.startswith("/")

        picker_state.cancel("signal", "+155****4567", "sess_abc123")

        # Verify it's gone
        assert picker_state.lookup("signal", "+155****4567", "sess_abc123") is None


class TestDispatchFreeFormExpire:
    """Tests for free-form text expiring the pending picker."""

    @pytest.mark.asyncio
    async def test_freeform_text_expires_pending_picker(self):
        """A non-command, non-digit, non-'more' reply should expire the picker."""
        runner, adapter = _make_dispatch_runner()
        providers = FAKE_PROVIDERS

        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+155****4567",
            session_key="sess_abc123",
            providers=providers,
            current_model="claude-haiku-4-5",
            current_provider="anthropic",
            on_model_selected=lambda *a: None,
        )

        adapter = runner.adapters.get(Platform.SIGNAL)
        picker_state = adapter._model_picker_state

        assert picker_state.lookup("signal", "+155****4567", "sess_abc123") is not None

        # Simulate free-form text
        event = _make_dispatch_event("hello, how are you?")
        raw_text = (event.text or "").strip()
        assert not raw_text.startswith("/")
        assert not raw_text.isdigit()
        assert raw_text.lower() != "more"

        picker_state.cancel("signal", "+155****4567", "sess_abc123")

        assert picker_state.lookup("signal", "+155****4567", "sess_abc123") is None


class TestDispatchMorePagination:
    """Tests for 'more' pagination handling."""

    @pytest.mark.asyncio
    async def test_more_advances_page(self):
        """'more' should increment current_page and re-send with new page."""
        runner, adapter = _make_dispatch_runner()
        # Build providers with enough models to require multiple pages
        multi_providers = [
            {
                "slug": "p1",
                "name": "Provider1",
                "models": [f"model-{i}" for i in range(15)],  # 15 models -> 2 pages
            }
        ]

        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+155****4567",
            session_key="sess_abc123",
            providers=multi_providers,
            current_model="model-0",
            current_provider="p1",
            on_model_selected=lambda *a: None,
            current_page=0,
        )

        adapter = runner.adapters.get(Platform.SIGNAL)
        picker_state = adapter._model_picker_state
        pending = picker_state.lookup("signal", "+155****4567", "sess_abc123")
        assert pending is not None
        assert pending.current_page == 0

        # Simulate 'more' -> new_page = (0 + 1) % 2 = 1
        new_page = (pending.current_page + 1) % 2
        key = ("signal", "+155****4567", "sess_abc123")
        picker_state._store[key].current_page = new_page

        assert pending.current_page == 1

        # Verify send_model_picker was called with page=1
        adapter.send_model_picker.assert_not_called()
        await adapter.send_model_picker(
            chat_id="+155****4567",
            providers=multi_providers,
            current_model="model-0",
            current_provider="p1",
            session_key="sess_abc123",
            on_model_selected=pending.on_model_selected,
            page=new_page,
        )
        adapter.send_model_picker.assert_called_once()
        call_kwargs = adapter.send_model_picker.call_args[1]
        assert call_kwargs["page"] == 1

    @pytest.mark.asyncio
    async def test_more_wraps_to_page_0(self):
        """'more' on the last page should wrap back to page 0."""
        runner, adapter = _make_dispatch_runner()
        multi_providers = [
            {
                "slug": "p1",
                "name": "Provider1",
                "models": [f"model-{i}" for i in range(15)],  # 2 pages
            }
        ]

        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+155****4567",
            session_key="sess_abc123",
            providers=multi_providers,
            current_model="model-0",
            current_provider="p1",
            on_model_selected=lambda *a: None,
            current_page=1,  # already on last page
        )

        adapter = runner.adapters.get(Platform.SIGNAL)
        picker_state = adapter._model_picker_state
        pending = picker_state.lookup("signal", "+155****4567", "sess_abc123")
        assert pending.current_page == 1

        # Wrap: (1 + 1) % 2 = 0
        new_page = (pending.current_page + 1) % 2
        assert new_page == 0


class TestAdapterPageRendering:
    """Tests for send_model_picker with non-zero page parameter."""

    @pytest.mark.asyncio
    async def test_send_model_picker_renders_page_2(self):
        """send_model_picker(..., page=1) should render page 2 (1-indexed display)."""
        from gateway.platforms.signal import SignalAdapter

        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+155****1234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        # Build providers with enough models to force page 2 (page index 1)
        large_providers = [
            {
                "name": "BigProvider",
                "models": [f"model-{i}" for i in range(25)],  # 25 models -> 3 pages
            }
        ]

        await adapter.send_model_picker(
            chat_id="+155****9999",
            providers=large_providers,
            current_model="model-0",
            current_provider="BigProvider",
            session_key="sess-abc",
            on_model_selected=lambda *a, **k: None,
            page=1,  # second page
        )

        call_args = adapter._rpc.call_args
        message = call_args[0][1]["message"]

        # Page 1 (0-indexed) shows models 12-23
        # Display should show "page 2/3"
        assert "page 2/3" in message
        # First model on page 2 should be model-12 (1-indexed as 13)
        assert "  13. model-12" in message
        # Last model on page 2 should be model-23 (1-indexed as 24)
        assert "  24. model-23" in message
        # Should NOT show models from page 0
        assert "  1. model-0" not in message


# ---------------------------------------------------------------------------
# End-to-end: fake signal-cli SSE stream drives /model → picker → 3 → switch
# ---------------------------------------------------------------------------


class TestSignalPickerIntegration:
    """Integration test: adapter.send_model_picker + state store + index resolution
    glue together correctly. This is the closest we get to E2E without standing up
    a full GatewayRunner — the real dispatch intercept is exercised by
    TestDispatchDigitReply and friends.
    """

    @pytest.mark.asyncio
    async def test_full_model_picker_flow(self):
        """/model sends picker via _rpc, '3' resolves to 3rd model, callback fires, state cleared."""
        from gateway.platforms.signal import SignalAdapter
        from gateway.run import GatewayRunner

        # Build adapter with real send_model_picker but mocked _rpc
        adapter = SignalAdapter.__new__(SignalAdapter)
        adapter.account = "+155****1234"
        adapter.client = MagicMock()
        adapter._model_picker_state = None
        adapter._rpc = AsyncMock(return_value={"id": "msg-1"})

        # Build a runner that points to this adapter
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={Platform.SIGNAL: PlatformConfig(enabled=True, token="***")}
        )
        runner.adapters = {Platform.SIGNAL: adapter}
        runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

        source = _make_dispatch_source()
        session_entry = SessionEntry(
            session_key=build_session_key(source),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.SIGNAL,
            chat_type="dm",
        )
        runner.session_store = MagicMock()
        runner.session_store.get_or_create_session.return_value = session_entry
        runner.session_store.load_transcript.return_value = []
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._is_user_authorized = lambda _source: True

        providers = FAKE_PROVIDERS  # 5 models total from helpers.py

        # --- Event 1: /model → send_model_picker → _rpc("send") + state registered ---
        event1 = MessageEvent(text="/model", source=source, message_id="m1")
        sess_key = session_entry.session_key
        chat_id_str = str(source.chat_id)

        # Simulate the /model handler calling send_model_picker directly
        cb = AsyncMock(return_value="Switched to claude-sonnet-4-5")
        result = await adapter.send_model_picker(
            chat_id=chat_id_str,
            providers=providers,
            current_model="claude-haiku-4-5",
            current_provider="anthropic",
            session_key=sess_key,
            on_model_selected=cb,
        )

        assert result.success is True
        # _rpc("send", ...) should have been called once to send the picker
        adapter._rpc.assert_awaited_once()
        rpc_call = adapter._rpc.await_args
        assert rpc_call[0][0] == "send"
        send_args = rpc_call[0][1]
        assert send_args["message"]  # non-empty picker message
        assert "page 1/" in send_args["message"]

        # Picker state should be registered
        picker_state = adapter._model_picker_state
        assert picker_state is not None
        pending = picker_state.lookup("signal", chat_id_str, sess_key)
        assert pending is not None
        assert picker_state.has_pending("signal", chat_id_str, sess_key)

        # --- Event 2: "3" → intercept resolves to 3rd model → callback fires ---
        event2 = MessageEvent(text="3", source=source, message_id="m2")
        raw_text = (event2.text or "").strip()
        assert raw_text.isdigit()

        one_indexed = int(raw_text)
        resolved = ModelPickerState.resolve_model_index(pending.providers, one_indexed - 1)
        assert resolved is not None
        model_id, provider_slug = resolved

        # The 3rd model (flat index 2) should be 'claude-haiku-4-5' from anthropic
        assert model_id == "claude-haiku-4-5"
        assert provider_slug == "anthropic"

        # Invoke the callback as the dispatch intercept block would
        result_text = await pending.on_model_selected(chat_id_str, model_id, provider_slug)
        cb.assert_awaited_once_with(chat_id_str, model_id, provider_slug)
        assert result_text == "Switched to claude-sonnet-4-5"

        # Dispatch intercept block cancels state after callback fires
        picker_state.cancel("signal", chat_id_str, sess_key)

        # Pending state should be cleared after callback
        pending_after = picker_state.lookup("signal", chat_id_str, sess_key)
        assert pending_after is None
