"""``session.list`` by title reports the same ``last_active`` as the listing it resolves into."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    mod = importlib.import_module("tui_gateway.server")
    db = SessionDB(db_path=home / "state.db")
    monkeypatch.setattr(mod, "_get_db", lambda: db)
    yield mod, db
    mod._sessions.clear()
    mod._db = None


def _by_title(mod, title):
    return mod._methods["session.list"](1, {"title": title})["result"]["sessions"][0]


def _listed(mod, session_id):
    rows = mod._methods["session.list"](1, {"limit": 50})["result"]["sessions"]
    return next(row for row in rows if row["id"] == session_id)


def test_title_lookup_recency_equals_listing(server):
    mod, db = server
    db.create_session("solo", source="tui")
    db.set_session_title("solo", "Solo")
    db.append_message("solo", "user", "hi", timestamp=7000.0)

    assert _by_title(mod, "Solo")["last_active"] == _listed(mod, "solo")["last_active"]


def test_title_lookup_recency_follows_compression_tip(server):
    mod, db = server
    db.create_session("root", source="tui")
    db.set_session_title("root", "Lineage")
    db.append_message("root", "user", "old", timestamp=1000.0)
    db.end_session("root", "compression")
    db.create_session("tip", source="tui", parent_session_id="root")
    db.append_message("tip", "user", "new", timestamp=9000.0)

    row = _by_title(mod, "Lineage")
    assert row["resolved_id"] == "tip"
    assert row["last_active"] == _listed(mod, "tip")["last_active"]
