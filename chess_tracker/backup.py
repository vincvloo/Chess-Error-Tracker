"""Backing up and restoring your data.

A backup is a normal SQLite file (any SQLite tool opens it) holding *your*
data: analysed games and mistakes, practice and puzzle history, streaks,
ratings, badges and settings. That is small enough to email, or to carry to
another device.

Two things in the database are deliberately left out of the default backup
because they can simply be downloaded again, and together they are most of
its size: the raw Chess.com game downloads (`archives`) and the Lichess
puzzle library (`puzzles`). `full=True` includes them.

Restoring replaces the data tables in the live database with the backup's
contents. It never touches the download/puzzle caches unless the backup
contains them, and it saves a safety backup of what was there first.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from .db import open_db

# Order matters: parents before children (mistakes reference games).
USER_TABLES = (
    "games", "mistakes", "runs", "settings", "practice_attempts", "puzzle_attempts",
    "user_streaks", "user_puzzle_ratings", "user_game_ratings", "badges_earned",
    "puzzle_rush_scores", "daily_puzzles",
)
# position_evals (engine verdicts on opening positions) only exists once the
# position cache is in; tables missing from a database are simply skipped.
CACHE_TABLES = ("archives", "puzzles", "puzzle_source_stats", "position_evals")
META_TABLE = "backup_meta"


class BackupError(Exception):
    """A backup couldn't be made, or a file isn't a usable backup."""


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("chess-error-tracker")
    except Exception:
        return "unknown"


def default_backup_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def backup_filename(full: bool = False, when: datetime | None = None) -> str:
    when = when or datetime.now()
    return f"chess-tracker-backup-{when:%Y%m%d-%H%M%S}{'-full' if full else ''}.db"


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _has_table(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone() is not None


def _copy_table(conn: sqlite3.Connection, src_schema: str, dst_schema: str, table: str) -> int:
    """Copy rows between two attached databases using only the columns both
    have, so a backup from an older or newer version still restores."""
    src_cols = _columns(conn, src_schema, table)
    dst_cols = _columns(conn, dst_schema, table)
    cols = [c for c in dst_cols if c in src_cols]
    if not cols:
        return 0
    names = ", ".join(f'"{c}"' for c in cols)
    conn.execute(f"INSERT INTO {dst_schema}.{table} ({names}) SELECT {names} FROM {src_schema}.{table}")
    return conn.execute(f"SELECT COUNT(*) FROM {dst_schema}.{table}").fetchone()[0]


def _finish_file(path: str) -> None:
    """Leave `path` as a single self-contained file (no -wal/-shm beside it)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def create_backup(db_path: str, dest_path: str, full: bool = False) -> dict:
    """Write a backup of `db_path` to `dest_path` and return its manifest.
    Safe to run while the app is using the database."""
    if not os.path.isfile(db_path):
        raise BackupError(f"No database at {db_path}")
    if os.path.exists(dest_path):
        raise BackupError(f"{dest_path} already exists")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    partial = dest_path + ".partial"
    if os.path.exists(partial):
        os.remove(partial)

    tables = USER_TABLES + (CACHE_TABLES if full else ())
    counts: dict[str, int] = {}
    conn = open_db(partial)        # creates the same schema the app uses
    try:
        conn.execute("ATTACH DATABASE ? AS src", (db_path,))
        conn.execute("BEGIN IMMEDIATE")
        for table in tables:
            if _has_table(conn, "src", table):
                counts[table] = _copy_table(conn, "src", "main", table)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(f"CREATE TABLE {META_TABLE} (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(f"INSERT INTO {META_TABLE} VALUES (?, ?)", [
            ("created_at", created), ("app_version", app_version()),
            ("kind", "full" if full else "data")])
        conn.commit()
        conn.execute("DETACH DATABASE src")
    except Exception:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(partial + suffix)
            except FileNotFoundError:
                pass
        raise
    conn.close()
    _finish_file(partial)
    os.replace(partial, dest_path)
    return {"path": dest_path, "created_at": created, "kind": "full" if full else "data",
            "app_version": app_version(), "counts": counts,
            "bytes": os.path.getsize(dest_path)}


def read_manifest(path: str) -> dict:
    """Validate that `path` is a usable backup and describe it."""
    if not os.path.isfile(path):
        raise BackupError(f"No such file: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"Can't open {path}: {exc}")
    try:
        try:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackupError("That file is damaged (it failed SQLite's integrity check).")
        except sqlite3.DatabaseError:
            raise BackupError("That doesn't look like a Chess Error Tracker backup.")
        if not (_has_table(conn, "main", "games") and _has_table(conn, "main", "mistakes")):
            raise BackupError("That doesn't look like a Chess Error Tracker backup "
                              "(no games or mistakes tables).")
        meta = {}
        if _has_table(conn, "main", META_TABLE):
            meta = dict(conn.execute(f"SELECT key, value FROM {META_TABLE}").fetchall())
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in USER_TABLES + CACHE_TABLES if _has_table(conn, "main", t)}
    finally:
        conn.close()
    return {"path": path, "created_at": meta.get("created_at"),
            "kind": meta.get("kind", "unknown"), "app_version": meta.get("app_version"),
            "counts": counts, "bytes": os.path.getsize(path)}


def restore_backup(db_path: str, backup_path: str, safety_dir: str | None = None) -> dict:
    """Replace the data in `db_path` with the contents of `backup_path`.
    Saves a safety backup of the current data first (returned as `safety`)."""
    manifest = read_manifest(backup_path)
    if os.path.abspath(backup_path) == os.path.abspath(db_path):
        raise BackupError("That is the live database itself, not a backup.")

    safety = None
    if os.path.isfile(db_path):
        folder = safety_dir or default_backup_dir(db_path)
        name = backup_filename().replace("backup-", "before-restore-")
        safety = create_backup(db_path, os.path.join(folder, name))["path"]

    conn = open_db(db_path)
    try:
        conn.execute("ATTACH DATABASE ? AS bk", (backup_path,))
        # Caches are only replaced when the backup actually carries them.
        replace = [t for t in USER_TABLES if _has_table(conn, "bk", t)]
        replace += [t for t in CACHE_TABLES if _has_table(conn, "bk", t)
                    and conn.execute(f"SELECT 1 FROM bk.{t} LIMIT 1").fetchone()]
        conn.execute("BEGIN IMMEDIATE")
        for table in reversed(replace):
            conn.execute(f"DELETE FROM main.{table}")
        for table in replace:
            _copy_table(conn, "bk", "main", table)
        conn.commit()
        conn.execute("DETACH DATABASE bk")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"restored": manifest, "replaced": replace, "safety": safety}


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


def describe(manifest: dict) -> str:
    c = manifest["counts"]
    return (f"{c.get('games', 0):,} games, {c.get('mistakes', 0):,} mistakes, "
            f"{c.get('practice_attempts', 0) + c.get('puzzle_attempts', 0):,} practice/puzzle attempts "
            f"({_human_size(manifest['bytes'])})")


def backup_main(argv: list[str]) -> None:
    """`chess-tracker backup ...`"""
    from .cli import DEFAULT_DB
    p = argparse.ArgumentParser(prog="chess-tracker backup",
                                description="Save a portable copy of your data.")
    p.add_argument("--db", default=DEFAULT_DB, help="Database to back up.")
    p.add_argument("--to", help="Where to write it (default: a backups/ folder next to the database).")
    p.add_argument("--full", action="store_true",
                   help="Also include the downloaded Chess.com games and the puzzle library (much larger).")
    args = p.parse_args(argv)
    dest = args.to or os.path.join(default_backup_dir(args.db), backup_filename(args.full))
    try:
        info = create_backup(args.db, dest, full=args.full)
    except BackupError as exc:
        sys.exit(str(exc))
    print(f"Backup saved to {info['path']}\n  {describe(info)}")


def restore_main(argv: list[str]) -> None:
    """`chess-tracker restore FILE ...`"""
    from .cli import DEFAULT_DB
    p = argparse.ArgumentParser(prog="chess-tracker restore",
                                description="Replace your data with a backup.")
    p.add_argument("file", help="The backup file.")
    p.add_argument("--db", default=DEFAULT_DB, help="Database to restore into.")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")
    args = p.parse_args(argv)
    try:
        manifest = read_manifest(args.file)
    except BackupError as exc:
        sys.exit(str(exc))
    print(f"Backup: {describe(manifest)}, made {manifest['created_at'] or 'at an unknown time'}")
    print(f"This replaces the data in {args.db}. A safety copy of what's there now is saved first.")
    print("Close the web app before restoring.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Cancelled.")
    try:
        result = restore_backup(args.db, args.file)
    except BackupError as exc:
        sys.exit(str(exc))
    if result["safety"]:
        print(f"Safety copy of the previous data: {result['safety']}")
    print("Restored.")
