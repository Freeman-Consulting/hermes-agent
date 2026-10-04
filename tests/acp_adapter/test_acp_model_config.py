"""config.yaml ``acp.models`` trims the editor model picker; ``acp.default_model``
sets the model for brand-new ACP sessions only."""

from types import SimpleNamespace

import pytest

import acp_adapter.server as server
from acp_adapter.session import SessionManager


def _m(mid):
    return SimpleNamespace(model_id=mid, name=mid)


PICKER = [_m("nous:anthropic/claude-opus-5.5"), _m("nous:openai/gpt-6-sol"),
          _m("openai-codex:gpt-6-sol"), _m("openai-codex:gpt-6-astra"),
          _m("openrouter:openai/gpt-6-sol"), _m("main-llm:qwen38-27b")]


def _cfg(monkeypatch, acp):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"model": {"default": "anthropic/claude-opus-5.5", "provider": "nous"}, "acp": acp})


def test_no_allowlist_keeps_everything(monkeypatch):
    _cfg(monkeypatch, {})
    assert server._apply_acp_model_allowlist(PICKER) == PICKER


def test_allowlist_filters_and_orders_with_globs(monkeypatch):
    _cfg(monkeypatch, {"models": ["openai-codex:*", "nous:anthropic/claude-opus-5.5"]})
    ids = [m.model_id for m in server._apply_acp_model_allowlist(PICKER)]
    assert ids == ["openai-codex:gpt-6-sol", "openai-codex:gpt-6-astra", "nous:anthropic/claude-opus-5.5"]


def test_current_model_always_visible(monkeypatch):
    _cfg(monkeypatch, {"models": ["openai-codex:gpt-6-sol"]})
    ids = [m.model_id for m in server._apply_acp_model_allowlist(PICKER, "main-llm:qwen38-27b")]
    assert ids == ["main-llm:qwen38-27b", "openai-codex:gpt-6-sol"]


class _Captured(Exception):
    pass


@pytest.mark.parametrize("acp,model,provider,expected", [
    ({"default_model": "openai-codex:gpt-6-sol"}, None, None, ("gpt-6-sol", "openai-codex")),
    ({"default_model": "gpt-6-sol", "default_provider": "openai-codex"}, None, None, ("gpt-6-sol", "openai-codex")),
    ({}, None, None, ("anthropic/claude-opus-5.5", "nous")),
    # explicit choices (restore / picker switch) are never overridden
    ({"default_model": "openai-codex:gpt-6-sol"}, "anthropic/claude-opus-5.5", "nous", ("anthropic/claude-opus-5.5", "nous")),
])
def test_default_model_applies_only_to_new_sessions(monkeypatch, tmp_path, acp, model, provider, expected):
    _cfg(monkeypatch, acp)
    seen = {}
    import hermes_cli.runtime_provider as rp

    def fake_resolve(requested=None, **_k):
        seen["provider"] = requested
        raise _Captured()  # stop before building a real agent; model kwarg is checked below

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    import run_agent

    class FakeAgent:
        def __init__(self, **kw):
            seen["model"] = kw.get("model")

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    mgr = SessionManager(db=None)
    monkeypatch.setattr(mgr, "_get_db", lambda: None)
    try:
        mgr._make_agent(session_id="s1", cwd=str(tmp_path), model=model, requested_provider=provider)
    except Exception:
        pass
    assert (seen.get("model"), seen.get("provider")) == expected
