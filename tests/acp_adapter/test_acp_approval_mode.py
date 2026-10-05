"""``acp.approval_mode`` / ``acp.default_mode``: editor-only approval relaxation.

Contract: ``acp.approval_mode: off`` stops command prompts for ACP sessions only (gateway/CLI
session keys keep prompting), hardline blocks still win, and flipping the setting back
restores prompts on the next turn. ``acp.default_mode`` seeds the edit-approval mode for
sessions the editor never switched."""

import pytest

import acp_adapter.server as server
import tools.approval as approval
from tools import approval_context


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_session_yolo", set())
    monkeypatch.setattr(server, "_ACP_BYPASS_SESSIONS", set())
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")


def _cfg(monkeypatch, acp):
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config_readonly", lambda: {"acp": acp})


@pytest.mark.parametrize(
    "acp, expected",
    [
        ({}, False),
        ({"approval_mode": "manual"}, False),
        ({"approval_mode": "smart"}, False),
        ({"approval_mode": "off"}, True),
        ({"approval_mode": " OFF "}, True),
        ({"approval_mode": False}, True),  # YAML 1.1 bare `off`
        ({"approval_mode": True}, False),
    ],
)
def test_approval_mode_parsing(monkeypatch, acp, expected):
    _cfg(monkeypatch, acp)
    assert server._acp_command_approvals_off() is expected


def _guard(command, session_key, monkeypatch):
    """Run the real guard chain as an interactive session; record whether a prompt fired."""
    prompts = []

    def _cb(cmd, desc, **_):
        prompts.append(cmd)
        return "deny"

    token = approval_context._approval_session_key.set(session_key)
    interactive = approval_context.set_hermes_interactive_context(True)  # as the ACP turn binds
    try:
        result = approval.check_all_command_guards(command, "local", approval_callback=_cb)
    finally:
        approval_context.reset_hermes_interactive_context(interactive)
        approval_context._approval_session_key.reset(token)
    return result, prompts


def test_off_bypasses_prompts_for_acp_session_only(monkeypatch):
    _cfg(monkeypatch, {"approval_mode": "off"})
    server._sync_acp_command_bypass("acp-sess")

    acp_result, acp_prompts = _guard("rm -rf ./build", "acp-sess", monkeypatch)
    assert acp_result["approved"] is True and acp_prompts == []

    other_result, other_prompts = _guard("rm -rf ./build", "signal:group:x", monkeypatch)
    assert other_result["approved"] is False and other_prompts == ["rm -rf ./build"]


def test_hardline_still_blocks_when_off(monkeypatch):
    _cfg(monkeypatch, {"approval_mode": "off"})
    server._sync_acp_command_bypass("acp-sess")
    result, prompts = _guard("rm -rf /", "acp-sess", monkeypatch)
    assert result["approved"] is False and prompts == []


def test_turning_setting_back_restores_prompts(monkeypatch):
    _cfg(monkeypatch, {"approval_mode": "off"})
    server._sync_acp_command_bypass("acp-sess")
    _cfg(monkeypatch, {})
    server._sync_acp_command_bypass("acp-sess")
    result, prompts = _guard("rm -rf ./build", "acp-sess", monkeypatch)
    assert result["approved"] is False and prompts == ["rm -rf ./build"]


def test_sync_reapplies_after_session_teardown(monkeypatch):
    _cfg(monkeypatch, {"approval_mode": "off"})
    server._sync_acp_command_bypass("acp-sess")
    approval.clear_session("acp-sess")
    server._sync_acp_command_bypass("acp-sess")
    assert approval.is_session_yolo_enabled("acp-sess")


def test_unset_never_disables_a_bypass_acp_did_not_set(monkeypatch):
    approval.enable_session_yolo("acp-sess")  # e.g. set by another surface
    _cfg(monkeypatch, {})
    server._sync_acp_command_bypass("acp-sess")
    assert approval.is_session_yolo_enabled("acp-sess")


class _State:
    def __init__(self, mode=None, cwd="/tmp/proj"):
        self.mode, self.cwd = mode, cwd


@pytest.mark.parametrize(
    "acp, state_mode, expected_mode",
    [
        ({}, None, "default"),
        ({"default_mode": "dont_ask"}, None, "dont_ask"),
        ({"default_mode": "accept_edits"}, None, "accept_edits"),
        ({"default_mode": "bogus"}, None, "default"),
        ({"default_mode": "dont_ask"}, "default", "default"),  # explicit editor choice wins
    ],
)
def test_default_mode_seeds_unswitched_sessions(monkeypatch, acp, state_mode, expected_mode):
    _cfg(monkeypatch, acp)
    agent = server.HermesACPAgent.__new__(server.HermesACPAgent)
    state = _State(state_mode)
    assert agent._session_modes(state).current_mode_id == expected_mode
    policy, _cwd = agent._edit_approval_policy_for_state(state)
    assert policy == agent._MODE_TO_EDIT_APPROVAL_POLICY[expected_mode]
