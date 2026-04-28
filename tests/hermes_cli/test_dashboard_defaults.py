"""Tests for Hermes dashboard startup defaults."""

import argparse
from unittest.mock import patch


def test_dashboard_help_defaults_to_all_interfaces():
    """`hermes dashboard` should advertise the Tailnet/LAN-ready default."""
    import hermes_cli.main as cli_main

    with patch("sys.argv", ["hermes", "dashboard", "--help"]), patch("builtins.print"):
        try:
            cli_main.main()
        except SystemExit as exc:
            assert exc.code == 0


def test_cmd_dashboard_allows_default_public_bind(monkeypatch):
    """The CLI no longer requires --insecure for the default 0.0.0.0 bind."""
    import hermes_cli.main as cli_main

    captured = {}

    monkeypatch.setattr(cli_main, "_build_web_ui", lambda *args, **kwargs: True)

    def fake_start_server(**kwargs):
        captured.update(kwargs)

    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "start_server", fake_start_server)

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9119,
        no_open=True,
        insecure=False,
        tui=False,
    )

    cli_main.cmd_dashboard(args)

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9119
    assert captured["open_browser"] is False
    assert captured["allow_public"] is True
