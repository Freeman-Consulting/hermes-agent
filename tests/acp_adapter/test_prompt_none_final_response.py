"""A turn cancelled mid-tool returns ``final_response: None``; prompt() must end
with stop_reason=cancelled instead of raising (surfaced to editors as
"Internal error: 'NoneType' object has no attribute 'startswith'")."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import acp
from acp.schema import TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_none_final_response_does_not_crash(cancel):
    manager = SessionManager(agent_factory=lambda: MagicMock(name="MockAIAgent"))
    agent = HermesACPAgent(session_manager=manager)
    resp = await agent.new_session(cwd=".")
    state = manager.get_session(resp.session_id)

    def _run(*args, **kwargs):
        if cancel and state.cancel_event:
            state.cancel_event.set()
        return {"final_response": None, "interrupted": cancel, "messages": []}

    state.agent.run_conversation = _run
    state.agent.model = "test-model"
    state.agent.provider = "openrouter"
    conn = MagicMock(spec=acp.Client)
    conn.session_update = AsyncMock()
    agent._conn = conn

    out = await agent.prompt(prompt=[TextContentBlock(type="text", text="hi")], session_id=resp.session_id)

    assert out is not None
    if cancel:
        assert str(out.stop_reason) in {"cancelled", "StopReason.cancelled"}
