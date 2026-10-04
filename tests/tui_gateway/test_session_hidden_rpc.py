"""RPC-level tests for the generic hidden-session surface (tui_gateway).

Covers the two seams Bot Mode's "sessions are always hidden" policy leans on:

* ``session.set_hidden`` resolves a DURABLE stored session id when no live
  runtime session matches — plugins reconciling sessions they own (Bot Mode's
  hide sweep) hold stored ids for chats that aren't live right now. The old
  live-only lookup failed those with 4001 and the sweep silently no-opped.
* ``session.list`` honors ``include_hidden`` so owning surfaces (the Bots
  pane's per-profile browser) can still enumerate the rows they hid, while
  every default caller keeps the hidden rows dropped.
"""

import json

import pytest

import tui_gateway.server as srv
import tui_gateway.methods_session  # noqa: F401  (registers the RPC methods)
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


def _seed(db, sid: str) -> None:
    db.create_session(sid, source="desktop")
    db._conn.execute("UPDATE sessions SET message_count = 1 WHERE id = ?", (sid,))
    db._conn.commit()


def test_set_hidden_resolves_stored_id_without_live_session(db):
    """A stored (non-live) session id must be hideable — the sweep path."""
    _seed(db, "stored-chat")
    assert srv._find_live_session_by_key("stored-chat") is None

    envelope = _call("session.set_hidden", {"session_id": "stored-chat", "hidden": True})
    assert "error" not in envelope, envelope
    assert envelope["result"]["hidden"] is True
    assert db.get_session("stored-chat")["hidden"] == 1

    # And back — unhide through the same durable path.
    envelope = _call("session.set_hidden", {"session_id": "stored-chat", "hidden": False})
    assert "error" not in envelope, envelope
    assert db.get_session("stored-chat")["hidden"] == 0


def test_set_hidden_unknown_id_still_errors(db):
    envelope = _call("session.set_hidden", {"session_id": "no-such-session", "hidden": True})
    assert envelope.get("error"), envelope




def test_session_list_include_hidden(db):
    _seed(db, "plain-chat")
    _seed(db, "bot-chat")
    assert db.set_session_hidden("bot-chat", True) is True

    default_rows = _call("session.list", {})["result"]["sessions"]
    assert {s["id"] for s in default_rows} == {"plain-chat"}

    all_rows = _call("session.list", {"include_hidden": True})["result"]["sessions"]
    assert {s["id"] for s in all_rows} == {"plain-chat", "bot-chat"}


def test_mobile_artifact_display_metadata_merges_without_changing_content(db):
    _seed(db, "artifact-chat")
    db.append_message("artifact-chat", "assistant", "plain model response",
                      display_metadata={"reaction": "star"})
    assert db.merge_latest_message_display_metadata(
        "artifact-chat", role="assistant", display_metadata={"attachments": []}
    )
    row = db._conn.execute(
        "SELECT content, display_metadata FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        ("artifact-chat",),
    ).fetchone()
    assert row[0] == "plain model response"
    assert json.loads(row[1]) == {"reaction": "star", "attachments": []}


@pytest.mark.parametrize("source", ["oneshot", "kanban", "tool"])
def test_session_list_hides_internal_sources(db, source):
    """Finite one-shot runs (`hermes -z`, `chat -q`) and other non-conversation rows never reach the
    human picker; interactive rows stay (#112550)."""
    _seed(db, "plain-chat")
    db.create_session("internal-run", source=source)
    db._conn.execute("UPDATE sessions SET message_count = 1 WHERE id = ?", ("internal-run",))
    db._conn.commit()

    rows = _call("session.list", {})["result"]["sessions"]
    assert {s["id"] for s in rows} == {"plain-chat"}

def test_session_list_projects_authoritative_recency_and_list_flags(db):
    _seed(db, "mobile-chat")
    db._conn.execute(
        "UPDATE sessions SET last_activity_at = ?, pinned = 1 WHERE id = ?",
        (222.0, "mobile-chat"),
    )
    db._conn.commit()

    rows = _call("session.list", {})["result"]["sessions"]
    row = next(item for item in rows if item["id"] == "mobile-chat")

    assert row["last_active"] == 222.0
    assert row["pinned"] is True
    assert row["archived"] is False


def test_session_history_reads_durable_session_without_live_resume(db):
    _seed(db, "durable-chat")
    db.append_message("durable-chat", "user", "latest durable question")
    db.append_message("durable-chat", "assistant", "latest durable answer")

    envelope = _call("session.history", {"session_id": "durable-chat"})

    assert envelope.get("error") is None, envelope
    result = envelope["result"]
    assert result["session_id"] == "durable-chat"
    assert [row["role"] for row in result["messages"]] == ["user", "assistant"]
    assert [row.get("content") or row.get("text") for row in result["messages"]] == [
        "latest durable question",
        "latest durable answer",
    ]


def test_mobile_session_create_persists_empty_durable_row(db):
    envelope = _call(
        "session.create",
        {"cols": 80, "source": "ios-pocket", "title": "Gerard"},
    )

    assert envelope.get("error") is None, envelope
    result = envelope["result"]
    stored_id = result["stored_session_id"]
    assert db.get_session(stored_id) is not None

    rows = _call("session.list", {})["result"]["sessions"]
    created = next(row for row in rows if row["id"] == stored_id)
    assert created["title"] == "Gerard"
    assert created["message_count"] == 0

    history = _call("session.history", {"session_id": stored_id})
    assert history.get("error") is None, history
    assert history["result"]["session_id"] == stored_id
    assert history["result"]["messages"] == []
