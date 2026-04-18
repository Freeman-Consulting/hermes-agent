"""Tests for the 'Leave Unchanged' cancel-keyword flow on the Signal /model picker.

The picker supports a set of keyword replies that cancel the pending picker
and keep the user's current model unchanged. Works on both the provider
selection step and the model selection step.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gateway.model_picker_state import ModelPickerState
from gateway.platforms.signal import SignalAdapter
from gateway.run import _PICKER_CANCEL_KEYWORDS


# ---------------------------------------------------------------------------
# Keyword constant shape
# ---------------------------------------------------------------------------

class TestCancelKeywordConstant:
    """Guard the cancel-keyword set — shape and content."""

    def test_is_frozenset(self):
        assert isinstance(_PICKER_CANCEL_KEYWORDS, frozenset)

    def test_contains_expected_keywords(self):
        expected = {"cancel", "leave", "keep", "x", "nevermind", "never mind", "no"}
        assert expected <= _PICKER_CANCEL_KEYWORDS

    def test_all_lowercase(self):
        # Constant must match against raw_text.lower() — every entry must be lowercase.
        for kw in _PICKER_CANCEL_KEYWORDS:
            assert kw == kw.lower(), f"keyword {kw!r} is not lowercase"

    def test_does_not_collide_with_numeric_or_more(self):
        # Must not swallow valid picker inputs.
        assert "more" not in _PICKER_CANCEL_KEYWORDS
        for n in range(100):
            assert str(n) not in _PICKER_CANCEL_KEYWORDS


# ---------------------------------------------------------------------------
# Footer mentions the cancel idiom
# ---------------------------------------------------------------------------

@pytest.fixture
def signal_adapter():
    """Return a Signal adapter with a live ModelPickerState and a mocked RPC."""
    cfg = MagicMock(
        platform="signal",
        account="+15551234567",
        service_url="http://signal.test",
        enable_typing=False,
    )
    adapter = SignalAdapter(cfg)
    adapter._rpc = AsyncMock(return_value={"timestamp": 1})
    adapter._model_picker_state = ModelPickerState()
    adapter.client = True  # non-None → "connected" sentinel
    return adapter


@pytest.fixture
def sample_providers():
    return [
        {
            "slug": "nous",
            "name": "Nous",
            "models": ["hermes-4-70b", "hermes-4-405b"],
            "total_models": 2,
        },
        {
            "slug": "anthropic",
            "name": "Anthropic",
            "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
            "total_models": 2,
        },
    ]


class TestFooterMentionsCancel:
    """User-facing copy must advertise the cancel keyword on both pickers."""

    @pytest.mark.asyncio
    async def test_model_picker_footer_has_cancel(self, signal_adapter, sample_providers):
        await signal_adapter.send_model_picker(
            chat_id="+1999",
            providers=sample_providers,
            current_model="claude-opus-4-7",
            current_provider="anthropic",
            session_key="sess-model",
            on_model_selected=AsyncMock(),
        )

        # Last send call carries the rendered message.
        send_calls = [c for c in signal_adapter._rpc.call_args_list if c.args[0] == "send"]
        assert send_calls, "adapter did not send a model picker message"
        msg = send_calls[-1].args[1]["message"]
        assert 'Reply "cancel" to keep current model.' in msg

    @pytest.mark.asyncio
    async def test_provider_picker_footer_has_cancel(self, signal_adapter, sample_providers):
        await signal_adapter.send_provider_picker(
            chat_id="+1999",
            providers=sample_providers,
            current_model="claude-opus-4-7",
            current_provider="anthropic",
            session_key="sess-prov",
            on_provider_selected=AsyncMock(),
        )

        send_calls = [c for c in signal_adapter._rpc.call_args_list if c.args[0] == "send"]
        assert send_calls, "adapter did not send a provider picker message"
        msg = send_calls[-1].args[1]["message"]
        assert 'Reply "cancel" to keep current model.' in msg
