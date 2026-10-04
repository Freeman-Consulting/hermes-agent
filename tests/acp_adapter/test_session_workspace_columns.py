"""ACP sessions must persist cwd / git repo root / branch into first-class columns.

Regression: ACP stored cwd only inside ``model_config`` JSON, leaving the
``cwd`` / ``git_repo_root`` / ``git_branch`` columns empty, so Cursor/Zed
sessions could not be grouped or found by project from other surfaces.
"""

import subprocess
import time
from unittest.mock import MagicMock

from acp_adapter.session import SessionManager
from hermes_state import SessionDB


def _git_repo(path, branch="feature-x"):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    return path


def test_new_acp_session_records_workspace_and_git(tmp_path):
    repo = _git_repo(tmp_path / "myrepo")
    db = SessionDB(db_path=tmp_path / "state.db")
    manager = SessionManager(agent_factory=lambda: MagicMock(name="agent"), db=db)

    state = manager.create_session(cwd=str(repo))
    state.history.append({"role": "user", "content": "hello"})
    manager.save_session(state.session_id)
    manager.update_cwd(state.session_id, str(repo))  # metadata probe is asynchronous

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = db.get_session(state.session_id)
        if row and row["git_branch"] == "feature-x":
            break
        time.sleep(0.02)
    row = db.get_session(state.session_id)
    assert row["cwd"] == str(repo)
    assert row["git_repo_root"] and row["git_repo_root"].rstrip("/").endswith("myrepo")
    assert row["git_branch"] == "feature-x"


def test_non_git_cwd_still_records_cwd(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    db = SessionDB(db_path=tmp_path / "state.db")
    manager = SessionManager(agent_factory=lambda: MagicMock(name="agent"), db=db)

    state = manager.create_session(cwd=str(plain))
    state.history.append({"role": "user", "content": "hello"})
    manager.save_session(state.session_id)

    row = db.get_session(state.session_id)
    assert row["cwd"] == str(plain)
    assert not row["git_repo_root"]
