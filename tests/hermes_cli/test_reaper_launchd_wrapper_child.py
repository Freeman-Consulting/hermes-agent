"""The orphan reaper must never SIGTERM a launchd-supervised gateway's child process.

On macOS the gateway LaunchAgent execs ``python -m hermes_cli.stderr_timestamp -- python -m hermes_cli.main
gateway run``: launchd's service PID is the stderr_timestamp WRAPPER, and the real gateway is its child.
``_get_service_pids`` therefore names only the wrapper. Before the fix the child stayed a reap candidate,
so any Desktop-owned backend started from a different HERMES_HOME (the Desktop pool, the idle-proof test)
SIGTERM'd the live gateway on startup and launchd respawned it.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hermes_cli import gateway as gateway_cli

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-tree semantics")


@pytest.fixture
def supervised_tree():
    """A real wrapper process with a real child, mirroring the launchd wrapper -> gateway layout."""
    wrapper = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "print(c.pid, flush=True)\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True,
    )
    child_pid = int(wrapper.stdout.readline().strip())  # type: ignore[union-attr]
    try:
        yield wrapper.pid, child_pid
    finally:
        for pid in (child_pid, wrapper.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        wrapper.wait(timeout=5)


def test_service_wrapper_descendants_are_excluded(monkeypatch, supervised_tree):
    wrapper_pid, child_pid = supervised_tree
    monkeypatch.setattr(gateway_cli, "_get_service_pids", lambda all_profiles=False: {wrapper_pid})
    own = gateway_cli._reaper_exclusion_pids(None)
    assert wrapper_pid in own
    assert child_pid in own, "the launchd wrapper's gateway child must be exempt from the reaper"


def test_reaper_does_not_signal_supervised_child(monkeypatch, supervised_tree):
    wrapper_pid, child_pid = supervised_tree
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_windows", lambda: False)
    monkeypatch.setattr(gateway_cli, "_get_service_pids", lambda all_profiles=False: {wrapper_pid})
    # The process scan sees the child as a gateway, exactly as `ps -Aww` matches `gateway run`.
    monkeypatch.setattr(
        gateway_cli, "find_gateway_pids",
        lambda exclude_pids=None, all_profiles=False: [p for p in (child_pid,) if p not in (exclude_pids or set())],
    )
    sent = []
    import signal as _signal
    real_kill = os.kill

    def _record_kill(pid, sig):
        if pid == child_pid and sig == _signal.SIGTERM:
            sent.append((pid, sig))
            return None
        return real_kill(pid, sig)

    monkeypatch.setattr(gateway_cli.os, "kill", _record_kill)
    try:
        assert gateway_cli._reap_unsupervised_gateway_orphans() is False
    finally:
        monkeypatch.setattr(gateway_cli.os, "kill", real_kill)
    assert sent == []
