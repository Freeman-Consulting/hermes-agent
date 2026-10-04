"""Anthropic's CLI setup picker and /model share a discoverable catalog."""

from unittest.mock import patch

from hermes_cli.models import _PROVIDER_MODELS


def test_anthropic_setup_picker_includes_model_switch_catalog(monkeypatch):
    from hermes_cli.model_setup_flows import _model_flow_anthropic
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Isolate discovery from the user's credentials, disk cache, and network.
    monkeypatch.setattr("hermes_cli.auth.get_anthropic_key", lambda: "test-key")
    monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)
    monkeypatch.setattr("agent.anthropic_adapter._is_oauth_token", lambda _: False)
    monkeypatch.setattr("agent.models_dev.list_agentic_models", lambda provider: ["claude-opus-5-5"] if provider == "anthropic" else [])
    monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", lambda provider, **kwargs: list(_PROVIDER_MODELS[provider]))
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    captured = {}

    def capture_picker(models, **kwargs):
        captured["models"] = models
        return None

    with (
        patch("hermes_cli.model_setup_flows._prompt_auth_credentials_choice", return_value="use"),
        patch("hermes_cli.auth._prompt_model_selection", side_effect=capture_picker),
    ):
        _model_flow_anthropic({}, current_model="")

    rows = list_authenticated_providers(current_provider="anthropic")
    switch_models = next(row["models"] for row in rows if row["slug"] == "anthropic")
    assert "claude-opus-5-5" in switch_models
    assert set(switch_models) <= set(captured["models"])
    # Upstream puts newly discovered agentic models first; only catalog parity
    # matters, not preserving the older hard-coded list's ordering.
    assert set(_PROVIDER_MODELS["anthropic"]) <= set(captured["models"])
