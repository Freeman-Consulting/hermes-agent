"""Task-level ``auxiliary.<task>.extra_body`` must not follow a call onto a strict fallback.

Regression: compression pinned to a vLLM backend with ``chat_template_kwargs``; when that
backend was down the main-agent (Anthropic) fallback received the same key and 400'd with
"chat_template_kwargs: Extra inputs are not permitted", so compression aborted.
"""

from agent import auxiliary_client as ac

TASK_CFG = {
    "provider": "gb10-qwen",
    "model": "qwen38-flash-next-tf-fp4",
    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
}


def _kwargs(monkeypatch, destination, fallback_entry=None):
    monkeypatch.setattr(ac, "_get_auxiliary_task_config", lambda task: dict(TASK_CFG))
    return ac._fallback_request_kwargs(
        destination,
        task="compression",
        messages=[{"role": "user", "content": "summarize"}],
        tools=None,
        temperature=None,
        max_tokens=256,
        effective_timeout=30.0,
        effective_extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        reasoning_config=None,
        fallback_entry=fallback_entry or {},
        task_config={},
        apply_fast_lane=False,
    )


def _wire_extras(kwargs):
    return kwargs.get("extra_body") or {}


def test_primary_backend_extras_dropped_on_anthropic_fallback(monkeypatch):
    dest = ac._FallbackDestination("anthropic", "", "anthropic_messages", "claude-opus-5-5")
    assert "chat_template_kwargs" not in _wire_extras(_kwargs(monkeypatch, dest))


def test_primary_backend_keeps_its_extras(monkeypatch):
    dest = ac._FallbackDestination("gb10-qwen", "http://127.0.0.1:18009/v1", "chat_completions",
                                   "qwen38-flash-next-tf-fp4")
    assert _wire_extras(_kwargs(monkeypatch, dest))["chat_template_kwargs"] == {"enable_thinking": False}


def test_fallback_entry_own_extras_still_apply(monkeypatch):
    dest = ac._FallbackDestination("anthropic", "", "anthropic_messages", "claude-opus-5-5")
    extras = _wire_extras(_kwargs(monkeypatch, dest, {"extra_body": {"service_tier": "auto"}}))
    assert extras.get("service_tier") == "auto"
    assert "chat_template_kwargs" not in extras
