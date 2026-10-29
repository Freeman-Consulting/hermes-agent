"""``session.list`` with ``preview: "latest"`` shows where each conversation IS.

Hermes Mobile's conversation list showed every row's first prompt, so a long-running
conversation always read like its opening question. The opt-in mode returns the newest
user/assistant text (same helper as the profile roster); the default stays the first prompt.
"""

import pytest

import tui_gateway.methods_session  # noqa: F401  (registers session.* methods)
import tui_gateway.server as srv
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def _previews(params: dict) -> dict:
    rows = srv._methods["session.list"](1, params)["result"]["sessions"]
    return {row["id"]: row["preview"] for row in rows}


def _seed(db) -> None:
    db.create_session("s1", "cli")
    db.append_message("s1", "user", "how do I rotate the relay token")
    db.append_message("s1", "assistant", "Run the rotate script, then restart.")
    db.append_message("s1", "tool", "tool output is never a preview")
    db.create_session("s2", "cli")
    db.append_message("s2", "user", "only one message here")
    db._conn.commit()


def test_latest_mode_returns_newest_user_or_assistant_text(db):
    _seed(db)
    previews = _previews({"preview": "latest"})
    assert previews["s1"] == "Run the rotate script, then restart."
    assert previews["s2"] == "only one message here"


def test_default_mode_keeps_first_prompt(db):
    _seed(db)
    assert _previews({})["s1"].startswith("how do I rotate the relay token")
