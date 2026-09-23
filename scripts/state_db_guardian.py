#!/Users/openclaw/.hermes/hermes-agent/venv/bin/python
"""Independent read-only state.db probe and validated online backup.

Launchd invokes this every 15 minutes; it never opens the source writable.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

MATCHES = (
    "session db append_message failed: constraint failed",
    "session db append_message failed: database disk image is malformed",
    "state.db fts indexes remain corrupt",
    "database disk image is malformed",
)


def atomic_json(path: Path, value: dict) -> None:
    stage = path.with_name(path.name + ".tmp")
    with stage.open("w") as out:
        json.dump(value, out, sort_keys=True, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(stage, path)


def connection(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60)
    else:
        conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def check_db(conn: sqlite3.Connection, *, full: bool, kind: str) -> dict:
    result = conn.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"SQLite {'integrity' if full else 'quick'} check failed: {str(result)[:200]}")
    fk = conn.execute("SELECT * FROM pragma_foreign_key_check LIMIT 1").fetchone()
    if fk is not None:
        raise RuntimeError("SQLite foreign_key_check found a violation")
    if kind == "rooms":
        return {
            "rooms": conn.execute("SELECT count(*) FROM hosted_rooms").fetchone()[0],
            "events": conn.execute("SELECT count(*) FROM hosted_room_events").fetchone()[0],
        }
    return {
        "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
        "messages": conn.execute("SELECT count(*) FROM messages").fetchone()[0],
        "max_message_id": conn.execute("SELECT max(id) FROM messages").fetchone()[0],
    }


def new_log_errors(path: Path, cursor: dict, *, scan_existing: bool = False) -> tuple[list[str], dict]:
    if not path.exists():
        return [], {"inode": 0, "offset": 0, "initialized": True}
    stat = path.stat()
    inode = stat.st_ino
    if cursor.get("inode") != inode or cursor.get("offset", 0) > stat.st_size:
        offset = 0 if scan_existing or cursor.get("initialized") else stat.st_size
    else:
        offset = cursor.get("offset", 0)
    with path.open("rb") as f:
        f.seek(offset)
        raw = f.read(min(stat.st_size - offset, 2 * 1024 * 1024))
        pos = f.tell()
    if pos < stat.st_size:  # catch up next tick rather than silently drop a long burst
        cursor = {"inode": inode, "offset": pos}
    else:
        cursor = {"inode": inode, "offset": stat.st_size}
    lines = raw.decode("utf-8", errors="replace").splitlines()
    matches = [line[:300] for line in lines if any(token in line.lower() for token in MATCHES)]
    return matches[:5], cursor


def alert(message: str, root: Path, *, notify: bool) -> None:
    print("STATE_DB_ALERT: " + message, flush=True)
    with (root / "alerts.log").open("a") as out:
        out.write(datetime.now(timezone.utc).isoformat() + " " + message + "\n")
    if notify:
        # OS notification is independent of Hermes's possibly-broken DB/gateway.
        safe = message[:160].replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe}" with title "Hermes state.db alert"'],
            capture_output=True, timeout=10, check=False,
        )

def maybe_alert(key: str, message: str, root: Path, state: dict, *, notify: bool) -> bool:
    """Alert immediately on a new fault, then at most every six hours."""
    now = time.time()
    previous = state.setdefault("last_alert_at", {}).get(key, 0)
    if now - previous < 6 * 3600:
        return False
    alert(message, root, notify=notify)
    state["last_alert_at"][key] = now
    return True


def backup(db: Path, root: Path, *, keep: int, kind: str) -> dict:
    if shutil.disk_usage(root).free < max(db.stat().st_size * 2, 10 * 1024**3):
        raise RuntimeError("not enough free space for an online backup")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"state-{stamp}.db"
    stage = root / (target.name + ".incomplete")
    if target.exists() or stage.exists():
        raise RuntimeError("backup filename already exists")
    try:
        with closing(connection(db, readonly=True)) as source, closing(connection(stage, readonly=False)) as dest:
            source.backup(dest, pages=256, sleep=0.05)
        with closing(connection(stage, readonly=True)) as check:
            counts = check_db(check, full=True, kind=kind)
        digest = hashlib.sha256()
        with stage.open("rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        os.replace(stage, target)
        with target.open("rb") as f:
            os.fsync(f.fileno())
        info = {"path": str(target), "sha256": digest.hexdigest(), "bytes": target.stat().st_size,
                "created_at": datetime.now(timezone.utc).isoformat(), **counts}
        atomic_json(root / (target.name + ".json"), info)
        backups = sorted(root.glob("state-*.db"), key=lambda p: p.name, reverse=True)
        for old in backups[keep:]:
            metadata = root / (old.name + ".json")
            old.unlink()
            metadata.unlink(missing_ok=True)
        return info
    except BaseException:
        stage.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(stage) + suffix).unlink(missing_ok=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path.home() / ".hermes/state.db")
    ap.add_argument("--kind", choices=("sessions", "rooms"), default="sessions")
    ap.add_argument("--root", type=Path, default=Path.home() / ".hermes/backups/state-db/rolling")
    ap.add_argument("--errors-log", type=Path, default=Path.home() / ".hermes/logs/errors.log")
    ap.add_argument("--interval-hours", type=float, default=6)
    ap.add_argument("--keep", type=int, default=28)
    ap.add_argument("--force-backup", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--skip-log-watch", action="store_true")
    args = ap.parse_args()
    assert args.keep >= 2 and args.interval_hours > 0
    if args.kind == "rooms" and not args.db.exists():
        return 0  # This store is created lazily by a gateway.
    args.root.mkdir(parents=True, exist_ok=True)
    lock = args.root / "guardian.lock"
    with lock.open("a+b") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # A previous, potentially large backup is still running.
        state_path = args.root / "guardian.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        matches = []
        if not args.skip_log_watch:
            matches, cursor = new_log_errors(args.errors_log, state.get("log_cursor", {}))
            state["log_cursor"] = cursor
            if matches:
                maybe_alert("append_error", f"new persistence errors ({len(matches)}); first: {matches[0]}", args.root, state, notify=args.notify)
        try:
            with closing(connection(args.db, readonly=True)) as conn:
                counts = check_db(conn, full=False, kind=args.kind)
            state["last_probe_at"] = datetime.now(timezone.utc).isoformat()
            state["last_probe_counts"] = counts
            due = args.force_backup or time.time() - state.get("last_backup_unix", 0) >= args.interval_hours * 3600
            if due:
                info = backup(args.db, args.root, keep=args.keep, kind=args.kind)
                state["last_backup_unix"] = time.time()
                state["last_backup"] = info
                print("STATE_DB_BACKUP_OK " + json.dumps(info, sort_keys=True), flush=True)
            atomic_json(state_path, state)
            return 0 if not matches else 2
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            maybe_alert("probe_failure", f"probe/backup failed: {type(exc).__name__}: {str(exc)[:180]}", args.root, state, notify=args.notify)
            atomic_json(state_path, state)
            return 1


if __name__ == "__main__":
    sys.exit(main())
