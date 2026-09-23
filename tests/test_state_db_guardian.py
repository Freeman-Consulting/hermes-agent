"""Operational contracts for the independent state database guardian."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import state_db_guardian as guard


def _source(path: Path, *, rooms: bool = False) -> None:
    conn = sqlite3.connect(path)
    if rooms:
        conn.executescript("CREATE TABLE hosted_rooms (room_id TEXT); CREATE TABLE hosted_room_events (event_id TEXT);")
        conn.execute("INSERT INTO hosted_rooms VALUES ('room-1')")
    else:
        conn.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY); CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id));")
        conn.execute("INSERT INTO sessions VALUES ('session-1')")
        conn.execute("INSERT INTO messages VALUES (1, 'session-1')")
    conn.commit()
    conn.close()


def test_online_backup_is_consistent_and_verified(tmp_path):
    source = tmp_path / "state.db"
    _source(source)
    output = tmp_path / "rolling"
    output.mkdir()
    info = guard.backup(source, output, keep=2, kind="sessions")
    assert info["sessions"] == info["messages"] == info["max_message_id"] == 1
    with guard.connection(Path(info["path"]), readonly=True) as saved:
        assert saved.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert saved.execute("SELECT session_id FROM messages").fetchone() == ("session-1",)
    assert json.loads((output / (Path(info["path"]).name + ".json")).read_text())["sha256"] == info["sha256"]


def test_shared_room_store_is_backed_up_without_sessions(tmp_path):
    source = tmp_path / "shared-state.db"
    _source(source, rooms=True)
    output = tmp_path / "rolling"
    output.mkdir()
    info = guard.backup(source, output, keep=2, kind="rooms")
    assert info["rooms"] == 1 and info["events"] == 0


def test_corrupt_source_does_not_replace_last_good_backup(tmp_path, monkeypatch):
    source = tmp_path / "state.db"
    _source(source)
    output = tmp_path / "rolling"
    output.mkdir()
    with guard.connection(source, readonly=True) as opened:
        assert guard.check_db(opened, full=False, kind="sessions")["messages"] == 1
    good = guard.backup(source, output, keep=2, kind="sessions")
    with source.open("r+b") as damaged:
        damaged.write(b"BROKEN")
    monkeypatch.setattr("sys.argv", ["guardian", "--db", str(source), "--root", str(output), "--force-backup", "--skip-log-watch"])
    assert guard.main() == 1
    assert Path(good["path"]).exists()
    assert len(list(output.glob("state-*.db"))) == 1


def test_new_log_error_is_reported_once(tmp_path):
    log = tmp_path / "errors.log"
    cursor = {}
    _, cursor = guard.new_log_errors(log, cursor)
    log.write_text("WARNING Session DB append_message failed: constraint failed\n")
    errors, cursor = guard.new_log_errors(log, cursor)
    assert len(errors) == 1
    assert guard.new_log_errors(log, cursor)[0] == []


def test_repeated_failure_notification_is_throttled(tmp_path, monkeypatch):
    delivered = []
    monkeypatch.setattr(guard, "alert", lambda message, root, notify: delivered.append(message))
    state = {}
    assert guard.maybe_alert("probe_failure", "broken", tmp_path, state, notify=True)
    assert not guard.maybe_alert("probe_failure", "still broken", tmp_path, state, notify=True)
    assert delivered == ["broken"]
