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


class _CaptureClient:
    """Stands in for the resolved wire client: records the request call_llm actually sends."""

    def __init__(self):
        self.sent = None
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.sent = kwargs
                from types import SimpleNamespace as NS
                msg = NS(content="summary", tool_calls=None, reasoning_content=None, refusal=None)
                return NS(choices=[NS(message=msg, finish_reason="stop", index=0)], usage=None, model="m")

        self.chat = type("C", (), {"completions": _Completions()})()
        self.base_url = "https://api.anthropic.com"
        self.api_key = "x"


def _call_with_explicit_provider(monkeypatch, provider):
    monkeypatch.setattr(ac, "_get_auxiliary_task_config", lambda task: dict(TASK_CFG))
    client = _CaptureClient()
    monkeypatch.setattr(
        ac, "_resolve_call_client",
        lambda task, **kw: (client, kw.get("model") or "m", provider, provider),
    )
    ac.call_llm(task="compression", provider=provider, model="claude-opus-5-5",
                messages=[{"role": "user", "content": "summarize"}])
    return (client.sent or {}).get("extra_body") or {}


def test_explicit_main_model_retry_drops_primary_extras(monkeypatch):
    """Compression's own main-model fallback (aux model 404 -> retry naming the main provider)
    bypasses the fallback chain; it must not carry vLLM chat_template_kwargs to Anthropic."""
    assert "chat_template_kwargs" not in _call_with_explicit_provider(monkeypatch, "anthropic")


def test_explicit_primary_provider_keeps_extras(monkeypatch):
    extras = _call_with_explicit_provider(monkeypatch, "gb10-qwen")
    assert extras.get("chat_template_kwargs") == {"enable_thinking": False}
