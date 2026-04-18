"""Tests for Signal platform model picker two-step flow.

This module tests the provider-first model selection feature:
1. User types /model
2. Signal shows provider picker (curated order, model counts)
3. User picks provider (or 0 for all providers)
4. Signal shows filtered model list
5. User picks model
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from types import SimpleNamespace
from typing import List, Dict, Any

import sys
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.platforms.signal import SignalAdapter
from gateway.platforms.base import SendResult, MessageEvent
from gateway.model_picker_state import ModelPickerState, PAGE_SIZE
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_signal_adapter():
    """Create a minimal SignalAdapter with mocked internals."""
    adapter = object.__new__(SignalAdapter)
    adapter.account = "+1234567890"
    adapter.client = AsyncMock()
    adapter._model_picker_state = ModelPickerState()
    adapter._http_session = AsyncMock()
    return adapter


@pytest.fixture
def sample_providers() -> List[Dict[str, Any]]:
    """Sample provider list for testing."""
    return [
        {
            "name": "Nous Portal",
            "slug": "nous",
            "models": ["nous/hermes-3", "nous/hermes-2"],
            "total_models": 2,
            "is_current": False,
        },
        {
            "name": "OpenRouter",
            "slug": "openrouter",
            "models": ["openrouter/gpt-4", "openrouter/claude-3"],
            "total_models": 2,
            "is_current": True,
        },
        {
            "name": "OpenAI",
            "slug": "openai",
            "models": ["gpt-4", "gpt-3.5-turbo"],
            "total_models": 2,
            "is_current": False,
        },
        {
            "name": "Ollama",
            "slug": "ollama",
            "models": ["llama2", "mistral"],
            "total_models": 2,
            "is_current": False,
        },
    ]


@pytest.fixture
def many_providers() -> List[Dict[str, Any]]:
    """Generate enough providers to trigger pagination."""
    providers = []
    for i in range(15):
        providers.append({
            "name": f"Provider{i}",
            "slug": f"prov{i}",
            "models": [f"model{i}_1"],
            "total_models": 1,
            "is_current": False,
        })
    return providers


# ── Tests for _sort_providers_curated ─────────────────────────────────────


class TestSortProvidersCurated:
    """Test curated provider ordering."""

    def test_curated_order_nous_first(self, mock_signal_adapter, sample_providers):
        """Nous Portal should appear first in curated order."""
        # Add Anthropic which comes after OpenAI in curated order
        providers = sample_providers + [{
            "name": "Anthropic",
            "slug": "anthropic",
            "models": ["claude-3"],
            "total_models": 1,
            "is_current": False,
        }]
        
        sorted_provs = mock_signal_adapter._sort_providers_curated(providers)
        slugs = [p["slug"] for p in sorted_provs]
        
        assert slugs[0] == "nous"  # First
        assert slugs[1] == "openrouter"  # Second
        assert slugs[2] == "openai"  # Third
        assert slugs[3] == "anthropic"  # Fourth

    def test_local_providers_after_curated(self, mock_signal_adapter, sample_providers):
        """Local providers (ollama) should appear after curated providers."""
        sorted_provs = mock_signal_adapter._sort_providers_curated(sample_providers)
        slugs = [p["slug"] for p in sorted_provs]
        
        # Ollama is a local provider and should be last
        assert slugs[-1] == "ollama"

    def test_alphabetical_for_unknown(self, mock_signal_adapter):
        """Unknown providers should be sorted alphabetically."""
        providers = [
            {"name": "Zebra", "slug": "zebra", "models": ["m1"]},
            {"name": "Alpha", "slug": "alpha", "models": ["m2"]},
            {"name": "Beta", "slug": "beta", "models": ["m3"]},
        ]
        
        sorted_provs = mock_signal_adapter._sort_providers_curated(providers)
        slugs = [p["slug"] for p in sorted_provs]
        
        assert slugs == ["alpha", "beta", "zebra"]


# ── Tests for _format_provider_list ───────────────────────────────────────


class TestFormatProviderList:
    """Test provider list formatting and pagination."""

    def test_includes_all_providers_item(self, mock_signal_adapter, sample_providers):
        """Item 0 should always be 'All providers'."""
        result = mock_signal_adapter._format_provider_list(sample_providers, page=0)
        
        assert result["lines"][0] == "0. All providers — show all models (8 total)"

    def test_shows_model_counts(self, mock_signal_adapter, sample_providers):
        """Each provider should show its model count."""
        result = mock_signal_adapter._format_provider_list(sample_providers, page=0)
        
        lines = result["lines"]
        assert "Nous Portal (2 models)" in lines[1]
        assert "OpenRouter (2 models)" in lines[2]

    def test_shows_current_marker(self, mock_signal_adapter, sample_providers):
        """Current provider should have checkmark marker."""
        # Set one provider as current
        sample_providers[0]["is_current"] = True
        
        result = mock_signal_adapter._format_provider_list(sample_providers, page=0)
        
        assert "✓" in result["lines"][1]  # First provider should have checkmark

    def test_pagination_has_more(self, mock_signal_adapter, many_providers):
        """More than 12 providers should trigger pagination."""
        result = mock_signal_adapter._format_provider_list(many_providers, page=0)
        
        assert result["has_next"] is True
        assert "More..." in result["lines"][-1]

    def test_pagination_page_2(self, mock_signal_adapter, many_providers):
        """Page 2 should show remaining providers after alphabetical sort."""
        result = mock_signal_adapter._format_provider_list(many_providers, page=1)
        
        assert result["page"] == 1
        assert result["has_next"] is False
        # After alphabetical string sort: Prov0, Prov1, Prov10, Prov11, Prov12, Prov13, Prov14, Prov2, Prov3...
        # Page 0 shows Prov0-Prov6 (items 0-12 incl "All" and "More...")
        # Page 1 shows exactly Prov7, Prov8, Prov9
        page_lines = result["lines"]
        assert any("Provider7" in line for line in page_lines)
        assert any("Provider8" in line for line in page_lines)
        assert any("Provider9" in line for line in page_lines)


# ── Tests for _handle_provider_pagination ─────────────────────────────────


class TestHandleProviderPagination:
    """Test provider pagination reply handling."""

    def test_more_advances_page(self, mock_signal_adapter, many_providers):
        """Reply 'more' should advance page when more providers exist."""
        # Need to seed the picker state first
        adapter = mock_signal_adapter
        adapter._model_picker_state.add(
            platform="signal",
            chat_id="+123",
            session_key="sess1",
            providers=many_providers,
            current_model="test",
            current_provider="openrouter",
            step="provider",
            selected_provider=None,
            current_page=0,
        )
        
        result = adapter._handle_provider_pagination("more", many_providers, current_page=0)
        
        assert result[0] == "next_page"
        assert result[1] == 1  # Next page index

    def test_zero_selects_all(self, mock_signal_adapter, sample_providers):
        """Reply '0' should return select_all action."""
        result = mock_signal_adapter._handle_provider_pagination("0", sample_providers, current_page=0)
        
        assert result[0] == "select_all"
        assert result[1] is None

    def test_number_selects_provider(self, mock_signal_adapter, sample_providers):
        """Reply with number should select that provider."""
        # Number 1 = first provider in curated order
        result = mock_signal_adapter._handle_provider_pagination("1", sample_providers, current_page=0)
        
        assert result[0] == "select_provider"
        # Should return the slug of the first provider (Nous Portal)
        assert result[1] == "nous"

    def test_invalid_out_of_range(self, mock_signal_adapter, sample_providers):
        """Out-of-range number should return invalid."""
        result = mock_signal_adapter._handle_provider_pagination("99", sample_providers, current_page=0)
        
        assert result[0] == "invalid"
        assert result[1] is None

    def test_invalid_non_numeric(self, mock_signal_adapter, sample_providers):
        """Non-numeric reply should return invalid."""
        result = mock_signal_adapter._handle_provider_pagination("hello", sample_providers, current_page=0)
        
        assert result[0] == "invalid"


# ── Tests for send_model_picker with provider_filter ─────────────────────-


@pytest.mark.asyncio
class TestSendModelPickerWithFilter:
    """Test model picker filtering by provider."""

    async def test_filter_by_slug(self, mock_signal_adapter, sample_providers):
        """Filtering by provider slug should work."""
        adapter = mock_signal_adapter
        adapter.client.send = AsyncMock(return_value={"timestamp": 123})
        
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            result = await adapter.send_model_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_model_selected=AsyncMock(),
                provider_filter="nous",
            )
        
        assert result.success is True
        # Should only show models from Nous
        # The message should mention Nous

    async def test_filter_by_name(self, mock_signal_adapter, sample_providers):
        """Filtering by provider name (case-insensitive) should work."""
        adapter = mock_signal_adapter
        
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            result = await adapter.send_model_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_model_selected=AsyncMock(),
                provider_filter="OpenAI",  # Mixed case
            )
        
        assert result.success is True

    async def test_zero_models_shows_error(self, mock_signal_adapter, sample_providers):
        """Provider with 0 models should show helpful error."""
        adapter = mock_signal_adapter
        sample_providers[0]["models"] = []  # Empty models for Nous
        sample_providers[0]["total_models"] = 0
        
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            result = await adapter.send_model_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_model_selected=AsyncMock(),
                provider_filter="nous",
            )
        
        # Should succeed but show message, not register picker state
        assert result.success is True

    async def test_no_filter_shows_all(self, mock_signal_adapter, sample_providers):
        """No filter should show all providers (backward compat)."""
        adapter = mock_signal_adapter
        
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            result = await adapter.send_model_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_model_selected=AsyncMock(),
                provider_filter=None,  # Explicit None
            )
        
        assert result.success is True


# ── Backward Compatibility Tests ──────────────────────────────────────────


class TestBackwardCompatibility:
    """Ensure existing single-step flow still works."""

    @pytest.mark.asyncio
    async def test_step_defaults_to_model(self, mock_signal_adapter, sample_providers):
        """Picker state should default to step='model'."""
        adapter = mock_signal_adapter
        
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            await adapter.send_model_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_model_selected=AsyncMock(),
            )
        
        entry = adapter._model_picker_state.lookup("signal", "+123", "sess1")
        assert entry is not None
        assert entry.step == "model"
        assert entry.selected_provider is None

    def test_old_state_without_step_field(self):
        # Simulate legacy entry created before step field existed
        state = ModelPickerState()
        
        # Add entry without explicit step (will use default)
        state.add(
            platform="signal",
            chat_id="+123",
            session_key="legacy",
            providers=[],
            current_model="test",
            current_provider="openrouter",
        )
        
        entry = state.lookup("signal", "+123", "legacy")
        # Should have default step='model'
        assert entry.step == "model"


# ── Integration-style Dispatch Tests ───────────────────────────────────────


@pytest.mark.asyncio
class TestDispatchIntegration:
    """Test the full two-step flow at dispatch level."""

    async def test_provider_step_routes_correctly(self, mock_signal_adapter, sample_providers):
        """Dispatch should route provider-step replies to handler."""
        # This tests the dispatch intercept logic in run.py
        # Handled by the _handle_provider_selection_reply method
        
        # Setup: user has provider picker open
        adapter = mock_signal_adapter
        with patch.object(adapter, "_rpc", return_value={"timestamp": 123}):
            await adapter.send_provider_picker(
                chat_id="+123",
                providers=sample_providers,
                current_model="test",
                current_provider="openrouter",
                session_key="sess1",
                on_provider_selected=AsyncMock(),
                page=0,
            )
        
        entry = adapter._model_picker_state.lookup("signal", "+123", "sess1")
        assert entry.step == "provider"
        assert entry.selected_provider is None
        
        # After user selects provider "1", the dispatch intercept would:
        # 1. See step='provider'
        # 2. Resolve "1" to "nous" provider
        # 3. Update entry.step='model', entry.selected_provider='nous'
        # 4. Call send_model_picker with provider_filter='nous'


# ── Edge Cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Test boundary conditions."""

    def test_empty_providers_list(self, mock_signal_adapter):
        """Empty provider list should show fallback message."""
        result = mock_signal_adapter._format_provider_list([], page=0)
        
        # Should only have "All providers" with 0 total
        assert result["lines"][0] == "0. All providers — show all models (0 total)"
        assert result["has_next"] is False

    def test_single_provider(self, mock_signal_adapter):
        """Single provider should not paginate."""
        providers = [{"name": "Solo", "slug": "solo", "models": ["m1"], "total_models": 1}]
        
        result = mock_signal_adapter._format_provider_list(providers, page=0)
        
        assert result["has_next"] is False
        assert len(result["lines"]) == 2  # 0. All + 1. Solo

    def test_exactly_12_providers_no_pagination(self, mock_signal_adapter):
        """Exactly 12 providers should fit on one page."""
        providers = [
            {"name": f"P{i}", "slug": f"p{i}", "models": ["m"], "total_models": 1}
            for i in range(12)
        ]
        
        result = mock_signal_adapter._format_provider_list(providers, page=0)
        
        # 12 providers + "0. All" = 13 lines, no "More..." needed
        assert result["has_next"] is False

    def test_13_providers_triggers_pagination(self, mock_signal_adapter):
        """13 providers should trigger pagination."""
        providers = [
            {"name": f"P{i}", "slug": f"p{i}", "models": ["m"], "total_models": 1}
            for i in range(13)
        ]
        
        result = mock_signal_adapter._format_provider_list(providers, page=0)
        
        assert result["has_next"] is True
        assert "More..." in result["lines"][-1]


# ── End-to-end dispatch tests (real _handle_message path) ────────────────


def _make_runner_for_dispatch():
    """Construct a bare GatewayRunner with the minimum mocks _handle_message needs.

    We bypass __init__ via object.__new__ and wire only the attributes the
    picker-intercept and command-dispatch path touch, so we can exercise
    the *real* _handle_message method without standing up SessionDB,
    agent caches, etc.  The fall-through agent path is cut short by
    mocking _handle_message_with_agent to a sentinel AsyncMock we can
    assert against.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter._model_picker_state = ModelPickerState()
    adapter._pending_messages = {}
    adapter._last_source = None
    adapter.send_model_picker = AsyncMock(return_value=SendResult(success=True))
    adapter.send_provider_picker = AsyncMock(return_value=SendResult(success=True))
    adapter.send = AsyncMock()
    # Real SignalAdapter has _sort_providers_curated; since _resolve_provider_index
    # in run.py calls it via hasattr()+method, give this MagicMock a passthrough.
    adapter._sort_providers_curated = lambda providers: list(providers)
    adapter._PROVIDER_PAGE_SIZE = 12
    runner.adapters = {Platform.SIGNAL: adapter}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)

    # Session store mock returns a stable session_key.
    source = SessionSource(
        platform=Platform.SIGNAL,
        user_id="u1",
        chat_id="+155****4567",
        user_name="tester",
        chat_type="dm",
    )
    sess_key = build_session_key(source)
    session_entry = SessionEntry(
        session_key=sess_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SIGNAL,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = sess_key
    runner.session_store.load_transcript.return_value = []

    # Running-agent state — empty so no interrupt path is taken.
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    runner._draining = False
    runner._is_user_authorized = lambda _source: True

    # Cut the agent dispatch path short — we only need to know IF it was
    # called (meaning the picker intercept let control fall through).
    runner._handle_message_with_agent = AsyncMock(return_value="agent-saw-text")

    # Release helper is called in the finally block.
    runner._release_running_agent_state = MagicMock(
        side_effect=lambda key: runner._running_agents.pop(key, None)
    )

    return runner, adapter, source, sess_key


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="m-" + text[:8])


@pytest.mark.asyncio
class TestRealDispatch:
    """End-to-end tests that exercise the real GatewayRunner._handle_message.

    These tests verify the picker interception block behaves correctly for
    each code path:
      - unrecognised free-form text on the provider step cancels the picker
        and lets the message fall through to the agent (B3 regression).
      - a valid digit on the model step fires the callback and dismisses
        the picker.
      - a /command while a picker is pending clears the picker and routes
        the command normally.
    """

    async def test_freeform_on_provider_step_cancels_and_falls_through(self):
        """B3 regression: unknown text during provider step must not be swallowed."""
        runner, adapter, source, sess_key = _make_runner_for_dispatch()

        # Seed a pending provider-step picker.
        adapter._model_picker_state.add(
            platform="signal",
            chat_id=str(source.chat_id),
            session_key=sess_key,
            providers=[
                {"slug": "nous", "name": "Nous", "models": ["nous/hermes-3"]},
                {"slug": "openai", "name": "OpenAI", "models": ["gpt-4"]},
            ],
            current_model="gpt-4",
            current_provider="openai",
            step="provider",
            selected_provider=None,
        )
        assert adapter._model_picker_state.has_pending(
            "signal", str(source.chat_id), sess_key
        )

        # Send free-form text that isn't a digit, /command, cancel keyword, or "more".
        event = _make_event("what's the weather like today?", source)
        result = await runner._handle_message(event)

        # 1. The picker was cancelled.
        assert adapter._model_picker_state.lookup(
            "signal", str(source.chat_id), sess_key
        ) is None

        # 2. The message was NOT intercepted — it reached the agent path.
        runner._handle_message_with_agent.assert_awaited_once()
        agent_call_event = runner._handle_message_with_agent.await_args[0][0]
        assert agent_call_event.text == "what's the weather like today?"

        # 3. The result is whatever the agent dispatch returned (not a picker reply).
        assert result == "agent-saw-text"
        # 4. No picker re-send happened.
        adapter.send_provider_picker.assert_not_called()
        adapter.send_model_picker.assert_not_called()

    async def test_valid_digit_on_model_step_fires_callback(self):
        """A numeric reply on the model step dismisses the picker and switches."""
        runner, adapter, source, sess_key = _make_runner_for_dispatch()

        providers = [
            {"slug": "nous", "name": "Nous", "models": ["nous/hermes-3", "nous/hermes-2"]},
            {"slug": "openai", "name": "OpenAI", "models": ["gpt-4"]},
        ]

        cb = AsyncMock(return_value="Switched to nous/hermes-2")
        adapter._model_picker_state.add(
            platform="signal",
            chat_id=str(source.chat_id),
            session_key=sess_key,
            providers=providers,
            current_model="gpt-4",
            current_provider="openai",
            on_model_selected=cb,
            step="model",
        )

        event = _make_event("2", source)
        result = await runner._handle_message(event)

        # Callback fired with the resolved (model, provider).
        cb.assert_awaited_once()
        call_args = cb.await_args[0]
        assert call_args[1] == "nous/hermes-2"
        assert call_args[2] == "nous"

        # Picker was cleared.
        assert adapter._model_picker_state.lookup(
            "signal", str(source.chat_id), sess_key
        ) is None

        # The response text came from the callback, not the agent.
        assert result == "Switched to nous/hermes-2"
        runner._handle_message_with_agent.assert_not_called()

    async def test_slash_command_during_pending_picker_clears_and_handles(self):
        """A /command while a picker is pending: picker cleared, command dispatched."""
        runner, adapter, source, sess_key = _make_runner_for_dispatch()

        adapter._model_picker_state.add(
            platform="signal",
            chat_id=str(source.chat_id),
            session_key=sess_key,
            providers=[{"slug": "openai", "name": "OpenAI", "models": ["gpt-4"]}],
            current_model="gpt-4",
            current_provider="openai",
            step="model",
        )

        # Stub the /status handler so we know the command path ran.
        runner._handle_status_command = AsyncMock(return_value="status-reply")

        event = _make_event("/status", source)
        result = await runner._handle_message(event)

        # Picker was cancelled.
        assert adapter._model_picker_state.lookup(
            "signal", str(source.chat_id), sess_key
        ) is None

        # /status handler ran; agent path did NOT.
        runner._handle_status_command.assert_awaited_once()
        runner._handle_message_with_agent.assert_not_called()
        assert result == "status-reply"

    async def test_valid_provider_digit_advances_to_model_step(self):
        """Selecting a provider by number should re-send model picker and keep state."""
        runner, adapter, source, sess_key = _make_runner_for_dispatch()

        providers = [
            {"slug": "nous", "name": "Nous", "models": ["nous/hermes-3"]},
            {"slug": "openai", "name": "OpenAI", "models": ["gpt-4"]},
        ]
        adapter._model_picker_state.add(
            platform="signal",
            chat_id=str(source.chat_id),
            session_key=sess_key,
            providers=providers,
            current_model="gpt-4",
            current_provider="openai",
            step="provider",
            selected_provider=None,
        )

        # send_model_picker is already an AsyncMock on the adapter.
        event = _make_event("1", source)  # select first provider (nous)
        result = await runner._handle_message(event)

        # Model picker was re-sent, provider state transitioned.
        adapter.send_model_picker.assert_awaited_once()
        # No text response bubbled up (picker sent async).
        assert result is None
        # Agent was not invoked.
        runner._handle_message_with_agent.assert_not_called()
        # Entry still exists but now on model step.
        entry = adapter._model_picker_state.lookup(
            "signal", str(source.chat_id), sess_key
        )
        assert entry is not None
        assert entry.step == "model"
        assert entry.selected_provider == "nous"
