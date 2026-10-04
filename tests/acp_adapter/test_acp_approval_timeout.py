"""Editor approval cards wait ``acp.approval_timeout`` (else ``approvals.timeout``)
before failing closed, instead of a hard-coded 60s."""

import asyncio
from concurrent.futures import Future

import pytest

import acp_adapter.server as server


def _cfg(monkeypatch, cfg):
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)


@pytest.mark.parametrize(
    "cfg, expected",
    [
        ({}, 300.0),  # approvals.timeout default
        ({"approvals": {"timeout": 120}}, 120.0),  # shared knob honoured
        ({"approvals": {"timeout": 120}, "acp": {"approval_timeout": 900}}, 900.0),
        ({"acp": {"approval_timeout": "1800"}}, 1800.0),
        ({"acp": {"approval_timeout": 0}}, 300.0),  # non-positive -> fallback
        ({"acp": {"approval_timeout": "soon"}}, 300.0),  # garbage -> fallback
    ],
)
def test_acp_approval_timeout_resolution(monkeypatch, cfg, expected):
    _cfg(monkeypatch, cfg)
    assert server._acp_approval_timeout() == expected


def test_acp_approval_timeout_is_platform_clamped(monkeypatch):
    from agent.deadline import MAX_SAFE_TIMEOUT_S

    _cfg(monkeypatch, {"acp": {"approval_timeout": 10**15}})
    assert server._acp_approval_timeout() == float(MAX_SAFE_TIMEOUT_S)


def test_human_wait_ceiling_covers_acp_timeout(monkeypatch):
    from tools.approval_human_wait import HUMAN_WAIT_MARGIN_S, human_wait_ceiling

    _cfg(monkeypatch, {"approvals": {"timeout": 300}, "acp": {"approval_timeout": 900}})
    assert human_wait_ceiling() == 900 + HUMAN_WAIT_MARGIN_S


def _capture_wait(monkeypatch, module):
    """Make the pending ACP request never answer; record the timeout used."""
    seen = {}

    class _Never(Future):
        def result(self, timeout=None):
            seen["timeout"] = timeout
            from concurrent.futures import TimeoutError as FT

            raise FT()

    def _schedule(coro, loop, **_):
        coro.close()
        return _Never()

    import agent.async_utils as au

    monkeypatch.setattr(au, "safe_schedule_threadsafe", _schedule)
    return seen


async def _req(**_):
    return None


def test_command_approval_uses_configured_timeout(monkeypatch):
    from acp_adapter.permissions import make_approval_callback

    seen = _capture_wait(monkeypatch, None)
    cb = make_approval_callback(_req, asyncio.new_event_loop(), "s1", timeout=900.0)
    assert cb("rm -rf x", "recursive delete") == "timeout"  # still fails closed
    assert seen["timeout"] == 900.0


def test_edit_approval_uses_configured_timeout_and_counts_as_human_wait(monkeypatch):
    from acp_adapter.edit_approval import make_acp_edit_approval_requester
    import tools.approval_human_wait as approval

    seen = _capture_wait(monkeypatch, None)
    windows = []
    real = approval.human_wait_window

    def _spy(*a, **k):
        windows.append(1)
        return real(*a, **k)

    monkeypatch.setattr(approval, "human_wait_window", _spy)
    monkeypatch.setattr(
        "acp_adapter.edit_approval.build_acp_edit_tool_call", lambda p: {"title": "edit"}
    )
    req = make_acp_edit_approval_requester(_req, asyncio.new_event_loop(), "s1", timeout=900.0)
    assert req(object()) is False  # unanswered card still denies the edit
    assert seen["timeout"] == 900.0
    assert windows == [1]
