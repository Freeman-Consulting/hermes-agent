"""ACP picker ids are ``provider:model``; an explicit prefix must be authoritative.

Regression: selecting ``openai-codex:gpt-6-sol`` while the session was already on
openai-codex re-routed to OpenRouter (``openai/gpt-6-sol``) via catalog
auto-detection, producing ``HTTP 401: User not found`` from OpenRouter.
"""

import pytest

from acp_adapter.server import HermesACPAgent


@pytest.mark.parametrize("current", ["nous", "openai-codex", "openrouter", ""])
@pytest.mark.parametrize(
    "choice, expected",
    [
        ("openai-codex:gpt-6-sol", ("openai-codex", "gpt-6-sol")),
        ("openai-codex:gpt-5.6-sol", ("openai-codex", "gpt-5.6-sol")),
        ("nous:openai/gpt-6-sol", ("nous", "openai/gpt-6-sol")),
        ("openrouter:openai/gpt-6-sol", ("openrouter", "openai/gpt-6-sol")),
    ],
)
def test_explicit_provider_prefix_is_authoritative(current, choice, expected):
    assert HermesACPAgent._resolve_model_selection(choice, current) == expected


def test_encoded_picker_choice_round_trips_for_same_provider():
    choice = HermesACPAgent._encode_model_choice("openai-codex", "gpt-6-sol")
    assert HermesACPAgent._resolve_model_selection(choice, "openai-codex") == (
        "openai-codex",
        "gpt-6-sol",
    )
