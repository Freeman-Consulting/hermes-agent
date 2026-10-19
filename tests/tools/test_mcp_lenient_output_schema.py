"""A server that adds a field to its structured output must not take the tool down.

Contract: validate_tool_result on the ClientSession Hermes builds treats
additionalProperties:false in a tool's OUTPUT schema as advisory. Unknown fields pass;
genuinely wrong shapes (bad type, missing required) still raise.

Regression: Robinhood added `agent_initiated` to order objects (2026-10-19). Every
place_equity_order / get_equity_orders call then failed client-side AFTER the broker had
acted. A placed, unconfirmable order is worse than an over-strict schema.
"""

import asyncio

import pytest

from tools import mcp_tool_transport as _transport


SCHEMA = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "state": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["orders"],
    "additionalProperties": False,
}


def _session_with_schema():
    """Real SDK ClientSession, streams unused: only validate_tool_result is exercised."""
    import anyio
    from mcp import types
    send, recv = anyio.create_memory_object_stream(0)
    session = _transport.MCPServerTransportMixin._make_client_session(recv, send)
    session._tool_output_schemas = {"get_orders": SCHEMA}
    return session, types


def _result(types, structured):
    return types.CallToolResult(content=[], structuredContent=structured, isError=False)


def test_unknown_field_in_output_is_tolerated():
    session, types = _session_with_schema()
    extra = {"orders": [{"id": "abc", "state": "filled", "agent_initiated": True}]}
    asyncio.run(session.validate_tool_result("get_orders", _result(types, extra)))


def test_wrong_type_still_fails():
    session, types = _session_with_schema()
    bad = {"orders": [{"id": 123}]}
    with pytest.raises(RuntimeError, match="Invalid structured content"):
        asyncio.run(session.validate_tool_result("get_orders", _result(types, bad)))


def test_missing_required_still_fails():
    session, types = _session_with_schema()
    bad = {"orders": [{"state": "filled"}]}
    with pytest.raises(RuntimeError, match="Invalid structured content"):
        asyncio.run(session.validate_tool_result("get_orders", _result(types, bad)))
