# Permanent command_allowlist can be ignored per-run (ACP editor sessions);
# session-scoped approvals and other contexts are unaffected.
import contextvars

from tools import approval as ap


def _with_allowlist(monkeypatch, entries):
    monkeypatch.setattr(ap, "_permanent_approved", set(entries))
    monkeypatch.setattr(ap, "_session_approved", {})


def test_default_honors_permanent_allowlist(monkeypatch):
    _with_allowlist(monkeypatch, {"recursive delete", "rm -rf build"})
    assert ap.is_approved("s1", "recursive delete") is True
    assert ap._command_matches_permanent_allowlist("rm -rf build") is True


def test_ignore_flag_disables_permanent_allowlist(monkeypatch):
    _with_allowlist(monkeypatch, {"recursive delete", "rm -rf build"})
    token = ap.set_ignore_permanent_allowlist(True)
    try:
        assert ap.is_approved("s1", "recursive delete") is False
        assert ap._command_matches_permanent_allowlist("rm -rf build") is False
    finally:
        ap.reset_ignore_permanent_allowlist(token)
    assert ap.is_approved("s1", "recursive delete") is True


def test_session_approval_still_applies_when_ignoring(monkeypatch):
    _with_allowlist(monkeypatch, {"recursive delete"})
    ap._session_approved["s1"] = {"recursive delete"}
    token = ap.set_ignore_permanent_allowlist(True)
    try:
        assert ap.is_approved("s1", "recursive delete") is True
        assert ap.is_approved("s2", "recursive delete") is False
    finally:
        ap.reset_ignore_permanent_allowlist(token)


def test_flag_is_context_isolated(monkeypatch):
    _with_allowlist(monkeypatch, {"recursive delete"})
    ctx = contextvars.copy_context()
    ctx.run(ap.set_ignore_permanent_allowlist, True)
    assert ctx.run(ap.is_approved, "s1", "recursive delete") is False
    assert ap.is_approved("s1", "recursive delete") is True


def test_check_dangerous_command_prompts_when_ignoring(monkeypatch):
    _with_allowlist(monkeypatch, {"recursive delete"})
    import tools.approval_context as approval_context
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    asked = []

    def cb(command, description, **kw):
        asked.append(command)
        return "deny"

    token = ap.set_ignore_permanent_allowlist(True)
    itoken = ap.set_hermes_interactive_context(True)  # what ACP sets per run
    try:
        res = ap.check_dangerous_command("rm -rf ./tmpdir", "local", approval_callback=cb)
    finally:
        ap.reset_hermes_interactive_context(itoken)
        ap.reset_ignore_permanent_allowlist(token)
    assert res.get("approved") is False
    assert asked, "approval callback must be consulted"

