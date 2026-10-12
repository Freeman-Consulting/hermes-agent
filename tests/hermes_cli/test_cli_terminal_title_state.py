"""Classic CLI publishes agent state in the OSC 2 title (⚠ / ⏳ / ✓).

Contract: terminal multiplexers (Herdr's Hermes manifest rules osc_title_*) read the
title marker as the authoritative state. Regression guarded: a busy turn reported as
idle once the user types a draft over the composer placeholder, because the only
working signal was the placeholder text."""

from types import SimpleNamespace

from cli import HermesCLI


class _Out:
    def __init__(self):
        self.raw = []

    def write_raw(self, s):
        self.raw.append(s)

    def flush(self):
        pass


def _cli(**state):
    c = HermesCLI.__new__(HermesCLI)
    for k in ("_approval_state", "_clarify_state", "_sudo_state", "_secret_state", "_slash_confirm_state"):
        setattr(c, k, None)
    c._agent_running = c._command_running = False
    for k, v in state.items():
        setattr(c, k, v)
    return c


def test_title_tracks_busy_blocked_idle_and_dedupes():
    c = _cli()
    app = SimpleNamespace(output=_Out())

    c._flush_terminal_title(app)
    c._agent_running = True
    c._flush_terminal_title(app)
    c._flush_terminal_title(app)  # unchanged -> no duplicate write
    c._approval_state = {"command": "rm -rf x"}
    c._flush_terminal_title(app)
    c._approval_state = None
    c._agent_running = False
    c._flush_terminal_title(app)

    assert app.output.raw == [
        "\x1b]2;✓ Hermes\x07",
        "\x1b]2;⏳ Hermes\x07",
        "\x1b]2;⚠ Hermes\x07",
        "\x1b]2;✓ Hermes\x07",
    ]
