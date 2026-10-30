"""RPC-level tests for ``session.pin`` and ``session.list include_pinned`` (tui_gateway).

Desktop pins through ``PATCH /api/sessions/{id}`` and its REST list back-fills pins past the
recency window. Mobile lists through ``session.list`` 20 rows at a time, so a pin older than the
window never reached it, and it had no way to pin at all.
"""

import pytest

import tui_gateway.server as srv
import tui_gateway.methods_session  # registers the handlers
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def _call(method: str, params: dict) -> dict:
    return srv._methods[method](1, params)


def _seed(db, sid: str, started: float) -> None:
    db.create_session(sid, source="desktop")
    db._conn.execute(
        "UPDATE sessions SET message_count = 1, started_at = ? WHERE id = ?", (started, sid)
    )
    db._conn.commit()


def test_pin_and_unpin_a_stored_session(db):
    _seed(db, "chat", 1000)
    envelope = _call("session.pin", {"session_id": "chat", "pinned": True})
    assert "error" not in envelope, envelope
    assert envelope["result"] == {"pinned": True, "session_key": "chat"}
    assert db.get_session("chat")["pinned"] == 1

    envelope = _call("session.pin", {"session_id": "chat", "pinned": False})
    assert envelope["result"]["pinned"] is False
    assert db.get_session("chat")["pinned"] == 0


def test_pin_requires_the_flag_and_a_known_session(db):
    _seed(db, "chat", 1000)
    assert _call("session.pin", {"session_id": "chat"})["error"]["code"] == 4021
    assert db.get_session("chat")["pinned"] == 0
    assert "error" in _call("session.pin", {"session_id": "nope", "pinned": True})


def test_list_include_pinned_backfills_pins_past_the_window(db):
    """Five newer chats fill a limit-3 window; the old pin is appended only when asked for."""
    _seed(db, "old-pinned", 1000)
    for index in range(5):
        _seed(db, f"new-{index}", 2000 + index)
    db.set_session_pinned("old-pinned", True)

    plain = _call("session.list", {"limit": 3})["result"]["sessions"]
    assert [row["id"] for row in plain] == ["new-4", "new-3", "new-2"]

    with_pins = _call("session.list", {"limit": 3, "include_pinned": True})["result"]["sessions"]
    assert [row["id"] for row in with_pins] == ["new-4", "new-3", "new-2", "old-pinned"]
    assert with_pins[-1]["pinned"] is True
