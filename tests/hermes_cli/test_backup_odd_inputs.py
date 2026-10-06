"""Full backups must not fail on data the zip format or SQLite snapshot cannot take as-is.

Regression (2026-10-05 nightly full backup, exit 1): a vendored toolchain cache with
pre-1980 mtimes ("ZIP does not support timestamps before 1980") and Xcode's XML
``TestResults/metadata.db`` ("SQLite safe copy failed") made every run "incomplete".
"""
import os
import sqlite3
import zipfile
from argparse import Namespace
from pathlib import Path


def _home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: x\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_pre_1980_mtime_is_archived_not_an_error(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    old = home / "projects" / "spike" / ".hermit" / "LICENSE"
    old.parent.mkdir(parents=True)
    old.write_text("license\n")
    os.utime(old, (0, 0))  # 1970, as shipped in crate tarballs

    from hermes_cli.backup import run_backup
    out = tmp_path / "b.zip"
    assert run_backup(Namespace(output=str(out))) is True
    with zipfile.ZipFile(out) as zf:
        assert zf.read("projects/spike/.hermit/LICENSE") == b"license\n"


def test_non_sqlite_dot_db_is_archived_as_plain_file(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    xml = home / "projects" / "app" / "TestResults" / "metadata.db"
    xml.parent.mkdir(parents=True)
    xml.write_bytes(b'<?xml version="1.0"?><plist/>\n')
    bplist = home / "projects" / "app" / "TestResults" / "other.db"
    bplist.write_bytes(b"bplist00\xd1\x01\x02")
    real = home / "projects" / "app" / "real.db"
    con = sqlite3.connect(real)
    con.execute("create table t(x)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()

    from hermes_cli.backup import run_backup
    out = tmp_path / "b.zip"
    assert run_backup(Namespace(output=str(out))) is True
    with zipfile.ZipFile(out) as zf:
        assert zf.read("projects/app/TestResults/metadata.db") == xml.read_bytes()
        assert zf.read("projects/app/TestResults/other.db") == bplist.read_bytes()
        restored = tmp_path / "restored.db"
        restored.write_bytes(zf.read("projects/app/real.db"))
    assert sqlite3.connect(restored).execute("select x from t").fetchone() == (42,)


def test_garbage_state_db_still_fails_the_backup(tmp_path, monkeypatch):
    """Only positive foreign magic takes the plain-copy path; unknown bytes keep failing."""
    home = _home(tmp_path, monkeypatch)
    (home / "state.db").write_bytes(b"not-a-database")

    from hermes_cli.backup import run_backup
    assert run_backup(Namespace(output=str(tmp_path / "b.zip"))) is False


def test_corrupt_sqlite_still_fails_the_backup(tmp_path, monkeypatch):
    """A file that claims to be SQLite but cannot be snapshotted is still a real failure."""
    home = _home(tmp_path, monkeypatch)
    bad = home / "projects" / "bad.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 200)

    from hermes_cli.backup import run_backup
    assert run_backup(Namespace(output=str(tmp_path / "b.zip"))) is False


def test_file_deleted_mid_backup_is_skipped_not_an_error(tmp_path, monkeypatch):
    """A file pruned between scan and write (cron output retention) is gone, not lost."""
    home = _home(tmp_path, monkeypatch)
    out_dir = home / "cron" / "output" / "job"
    out_dir.mkdir(parents=True)
    keep = out_dir / "keep.md"
    keep.write_text("keep\n")
    doomed = out_dir / "doomed.md"
    doomed.write_text("doomed\n")

    import hermes_cli.backup as b
    real_iter = b._iter_backup_files

    def scan_then_prune(*a, **kw):
        files = list(real_iter(*a, **kw))
        doomed.unlink()
        return iter(files)

    monkeypatch.setattr(b, "_iter_backup_files", scan_then_prune)
    out = tmp_path / "b.zip"
    assert b.run_backup(Namespace(output=str(out))) is True
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "cron/output/job/keep.md" in names
    assert "cron/output/job/doomed.md" not in names


def test_unreadable_file_still_fails_the_backup(tmp_path, monkeypatch):
    """Only a vanished file is forgiven; a present-but-unreadable file is still data at risk."""
    home = _home(tmp_path, monkeypatch)
    f = home / "locked.txt"
    f.write_text("secret\n")
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):
            import pytest
            pytest.skip("running as a user that bypasses file modes")
        from hermes_cli.backup import run_backup
        assert run_backup(Namespace(output=str(tmp_path / "b.zip"))) is False
    finally:
        f.chmod(0o600)
