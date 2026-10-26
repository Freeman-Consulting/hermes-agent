"""Confine real holder-scan tests to the test's own SQLite child process.

The pytest live-home guard forbids metadata probes of the operator's Hermes
files; an unrestricted psutil walk inspects unrelated processes' open logs.
The scanner still receives real open_files() data for the target holder.
"""

from types import SimpleNamespace

import psutil


def scan_only_child(monkeypatch, child):
    def process_iter(_attrs):
        if child.poll() is not None:
            return []
        try:
            opened = psutil.Process(child.pid).open_files()
        except psutil.NoSuchProcess:
            return []
        return [SimpleNamespace(info={"pid": child.pid, "open_files": opened})]

    monkeypatch.setattr("hermes_state_holders.psutil.process_iter", process_iter)
