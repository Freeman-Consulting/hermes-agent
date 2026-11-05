"""session.create sources: runtime.describe and the canonical validator share one table."""
from types import SimpleNamespace
from typing import get_args

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_policy import SURFACES
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.canonical_sessions import CanonicalCreateParams
from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems


@pytest.fixture
def owner(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(
            SimpleNamespace(_draining=False), profile_id="owned",
            instance_id="current", db=db, epoch=epoch,
        )
        authority.sessions["s"] = LiveSession(None, "route")
        yield AuthorityConnection(authority, object(), {"user_id": "owner"}), db


def test_describe_sources_and_canonical_literal_share_create_sources():
    """A source runtime.describe advertises must be a CanonicalCreateParams.source value.

    Drift here is the class of bug that made inherited automation labels (kanban, cron)
    fail session.create with invalid_params on ``source``.
    """
    from gateway.session_create_sources import CREATE_SOURCES

    advertised = tuple(sorted(SURFACES))
    assert advertised == tuple(sorted(CREATE_SOURCES))
    literal = get_args(CanonicalCreateParams.model_fields["source"].annotation)
    assert set(literal) == set(CREATE_SOURCES) == set(SURFACES)

    contract = CANONICAL_METHODS["session.create"]
    for source in CREATE_SOURCES:
        assert canonical_param_problems(contract, {"request_id": "r", "source": source}) == [], source
    assert canonical_param_problems(contract, {"request_id": "r", "source": "oneshot"}) == []
    for inherited in ("kanban", "cron", "a2a"):
        assert "source" in canonical_param_problems(
            contract, {"request_id": "r", "source": inherited}
        ), inherited


@pytest.mark.asyncio
async def test_session_create_dispatch_refuses_unadvertised_source(owner):
    """The owner dispatch path refuses source='kanban' at canonical_param_problems, before the handler."""
    connection, _ = owner
    try:
        refused = await connection.dispatch({
            "id": 2, "method": "session.create",
            "params": {"request_id": "r-kanban", "source": "kanban"},
        })
        assert refused["error"]["code"] == 4001
        assert refused["error"]["data"]["reason"] == "invalid_params"
        assert "source" in refused["error"]["data"]["fields"]
    finally:
        await connection.close()
