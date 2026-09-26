import os
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from chess_mistake_coach import backup as bk
from chess_mistake_coach import gamification as g
from chess_mistake_coach.db import get_settings, open_db, save_game, set_settings
from chess_mistake_coach.web.app import create_app

REC = {
    "url": "https://example.com/g1", "username": "alice", "end_time": 1000,
    "date": "2024-01-01", "time_class": "blitz", "my_colour": "white",
    "my_rating": 1500, "opp_rating": 1400, "result": "win", "eco": "C00",
    "moves_played": 20, "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
}
MISTAKE = {
    "game_url": REC["url"], "username": "alice", "date": REC["date"], "end_time": 1000,
    "time_class": "blitz", "my_rating": 1500, "my_colour": "white", "move_number": 5,
    "phase": "opening", "severity": "blunder", "cp_loss": 300, "category": "hung a pawn",
    "played": "e4", "best": "d4", "clock_seconds": 20.0, "fen": "fen-string",
}


def _populated(path):
    conn = open_db(path)
    save_game(conn, REC, [MISTAKE], depth=14)
    set_settings(conn, primary_user="alice", email="me@example.com")
    conn.execute("INSERT INTO practice_attempts (practicing_user, mistake_id, owner, category, "
                 "verdict, hint_used, created_at) VALUES ('alice', 1, 'alice', 'c', 'best', 0, 'x')")
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('alice', 'p1', 'solved', 1, 'x')")
    conn.execute("INSERT INTO puzzles (puzzle_id, fen, moves, rating, themes) "
                 "VALUES ('p1', 'f', 'a1a2 a2a3', 900, ' fork ')")
    conn.execute("INSERT INTO archives (url, username, month, body, game_count, complete) "
                 "VALUES ('u', 'alice', '2024-01', 'PGN BODY', 1, 1)")
    conn.commit()
    g.record_activity(conn, "alice")
    g.update_puzzle_rating(conn, "alice", 1200, True, " fork ")
    g.recompute_game_ratings(conn, "alice")
    g.evaluate_badges(conn, "alice")
    conn.close()
    return path


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_data_backup_holds_your_data_but_not_the_downloadable_caches(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    info = bk.create_backup(db, str(tmp_path / "b.db"))
    assert info["kind"] == "data"
    dest = info["path"]
    for table in ("games", "mistakes", "practice_attempts", "puzzle_attempts", "settings",
                  "user_streaks", "user_puzzle_ratings", "user_game_ratings", "badges_earned"):
        assert _count(dest, table) >= 1, table
    assert _count(dest, "puzzles") == 0 and _count(dest, "archives") == 0
    assert info["counts"]["games"] == 1


def test_full_backup_includes_the_caches(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    info = bk.create_backup(db, str(tmp_path / "full.db"), full=True)
    assert info["kind"] == "full"
    assert _count(info["path"], "puzzles") == 1 and _count(info["path"], "archives") == 1


def test_the_engine_position_cache_counts_as_a_recreatable_cache_and_absence_is_fine(tmp_path):
    assert "position_evals" in bk.CACHE_TABLES and "position_evals" not in bk.USER_TABLES
    db = _populated(str(tmp_path / "live.db"))      # this schema may not have the table at all
    info = bk.create_backup(db, str(tmp_path / "full.db"), full=True)
    assert info["kind"] == "full"


def test_backup_is_a_single_plain_file_and_leaves_no_partial(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    dest = str(tmp_path / "out" / "b.db")
    bk.create_backup(db, dest)
    assert sorted(os.listdir(tmp_path / "out")) == ["b.db"]


def test_backup_refuses_to_overwrite_or_use_a_missing_database(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    bk.create_backup(db, str(tmp_path / "b.db"))
    with pytest.raises(bk.BackupError):
        bk.create_backup(db, str(tmp_path / "b.db"))
    with pytest.raises(bk.BackupError):
        bk.create_backup(str(tmp_path / "nope.db"), str(tmp_path / "c.db"))


def test_backup_works_while_the_database_is_open_and_being_written(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    live = open_db(db)                       # e.g. the running app
    live.execute("INSERT INTO puzzle_rush_scores (username, score, duration_s, played_at) "
                 "VALUES ('alice', 7, 180, 'x')")
    live.commit()
    info = bk.create_backup(db, str(tmp_path / "b.db"))
    live.close()
    assert info["counts"]["puzzle_rush_scores"] == 1


def test_manifest_describes_a_valid_backup_and_rejects_junk(tmp_path):
    db = _populated(str(tmp_path / "live.db"))
    bk.create_backup(db, str(tmp_path / "b.db"))
    m = bk.read_manifest(str(tmp_path / "b.db"))
    assert m["kind"] == "data" and m["counts"]["mistakes"] == 1 and m["created_at"]

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(bk.BackupError):
        bk.read_manifest(str(junk))
    other = tmp_path / "other.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE unrelated (x)")
    c.commit()
    c.close()
    with pytest.raises(bk.BackupError):
        bk.read_manifest(str(other))
    with pytest.raises(bk.BackupError):
        bk.read_manifest(str(tmp_path / "missing.db"))


def test_restore_into_an_empty_database_brings_everything_back(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "b.db"))
    fresh = str(tmp_path / "fresh.db")
    open_db(fresh).close()
    result = bk.restore_backup(fresh, str(tmp_path / "b.db"))
    assert result["restored"]["counts"]["games"] == 1
    conn = open_db(fresh)
    assert get_settings(conn)["primary_user"] == "alice"
    assert conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0] == 1
    assert g.get_streak(conn, "alice")["best"] == 1
    assert g.get_skill_rating(conn, "alice")["overall"] is not None
    assert any(b["code"] == "first_practice" for b in g.get_badges(conn, "alice")["earned"])


def test_restore_replaces_current_data_and_saves_a_safety_copy(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "b.db"))
    live = str(tmp_path / "live.db")
    conn = open_db(live)
    save_game(conn, {**REC, "url": "https://example.com/other", "username": "bob"}, [], depth=14)
    conn.close()
    result = bk.restore_backup(live, str(tmp_path / "b.db"))
    conn = open_db(live)
    assert [r[0] for r in conn.execute("SELECT DISTINCT username FROM games")] == ["alice"]
    conn.close()
    assert result["safety"] and os.path.isfile(result["safety"])
    assert "before-restore" in os.path.basename(result["safety"])
    # ...and the safety copy really holds what was replaced.
    assert [r[0] for r in sqlite3.connect(result["safety"]).execute(
        "SELECT DISTINCT username FROM games")] == ["bob"]


def test_restoring_a_data_backup_keeps_the_existing_caches(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "b.db"))            # data only
    live = _populated(str(tmp_path / "live.db"))
    bk.restore_backup(live, str(tmp_path / "b.db"))
    assert _count(live, "puzzles") == 1 and _count(live, "archives") == 1


def test_restoring_a_full_backup_brings_the_caches_too(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "full.db"), full=True)
    fresh = str(tmp_path / "fresh.db")
    open_db(fresh).close()
    bk.restore_backup(fresh, str(tmp_path / "full.db"))
    assert _count(fresh, "puzzles") == 1 and _count(fresh, "archives") == 1


def test_restore_copes_with_a_backup_that_has_extra_columns(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    b = str(tmp_path / "b.db")
    bk.create_backup(src, b)
    c = sqlite3.connect(b)
    c.execute("ALTER TABLE games ADD COLUMN from_a_newer_version TEXT")
    c.commit()
    c.close()
    fresh = str(tmp_path / "fresh.db")
    open_db(fresh).close()
    bk.restore_backup(fresh, b)
    assert _count(fresh, "games") == 1


def test_restore_rejects_a_bad_file_and_changes_nothing(tmp_path):
    live = _populated(str(tmp_path / "live.db"))
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"nope" * 500)
    with pytest.raises(bk.BackupError):
        bk.restore_backup(live, str(junk))
    assert _count(live, "games") == 1


def test_restore_refuses_the_live_database_itself(tmp_path):
    live = _populated(str(tmp_path / "live.db"))
    with pytest.raises(bk.BackupError):
        bk.restore_backup(live, live)


# ---- command line -------------------------------------------------------------

def test_cli_backup_then_restore_round_trip(tmp_path, capsys):
    src = _populated(str(tmp_path / "src.db"))
    out = str(tmp_path / "cli.db")
    bk.backup_main(["--db", src, "--to", out])
    assert "Backup saved" in capsys.readouterr().out
    fresh = str(tmp_path / "fresh.db")
    open_db(fresh).close()
    bk.restore_main([out, "--db", fresh, "--yes"])
    assert "Restored." in capsys.readouterr().out
    assert _count(fresh, "games") == 1


def test_cli_restore_asks_first_and_can_be_declined(tmp_path, monkeypatch):
    src = _populated(str(tmp_path / "src.db"))
    out = str(tmp_path / "cli.db")
    bk.backup_main(["--db", src, "--to", out])
    live = str(tmp_path / "live.db")
    open_db(live).close()
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SystemExit):
        bk.restore_main([out, "--db", live])
    assert _count(live, "games") == 0


def test_cli_dispatches_backup_and_restore_subcommands(tmp_path, monkeypatch, capsys):
    from chess_mistake_coach import cli
    src = _populated(str(tmp_path / "src.db"))
    monkeypatch.setattr("sys.argv", ["chess-mistake-coach", "backup", "--db", src,
                                     "--to", str(tmp_path / "x.db")])
    cli.main()
    assert "Backup saved" in capsys.readouterr().out


# ---- web -------------------------------------------------------------------------

def _app(tmp_path):
    db = _populated(str(tmp_path / "web.db"))
    return create_app(db), db


def test_settings_page_offers_backup_and_shows_last_backup(tmp_path):
    app, db = _app(tmp_path)
    client = TestClient(app)
    import re
    assert re.search(r"Last backup:\s*never", client.get("/settings").text)
    client.get("/backup/download")
    after = client.get("/settings").text
    assert not re.search(r"Last backup:\s*never", after)
    assert re.search(r"Last backup:\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", after)


def test_download_returns_a_valid_backup_file(tmp_path):
    app, db = _app(tmp_path)
    r = TestClient(app).get("/backup/download")
    assert r.status_code == 200
    assert "chess-mistake-coach-backup-" in r.headers["content-disposition"]
    saved = tmp_path / "dl.db"
    saved.write_bytes(r.content)
    m = bk.read_manifest(str(saved))
    assert m["kind"] == "data" and m["counts"]["games"] == 1


def test_download_full_includes_the_caches(tmp_path):
    app, db = _app(tmp_path)
    r = TestClient(app).get("/backup/download", params={"full": 1})
    saved = tmp_path / "dl.db"
    saved.write_bytes(r.content)
    assert bk.read_manifest(str(saved))["kind"] == "full"


def test_upload_restores_and_reports_success(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "b.db"))
    live = str(tmp_path / "live.db")
    open_db(live).close()
    client = TestClient(create_app(live))
    with open(tmp_path / "b.db", "rb") as f:
        r = client.post("/backup/restore", files={"backup": ("b.db", f)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?restored=1"
    assert _count(live, "games") == 1
    assert "Backup restored" in client.get("/settings?restored=1").text


def test_upload_of_a_bad_file_shows_an_error_and_changes_nothing(tmp_path):
    live = _populated(str(tmp_path / "live.db"))
    client = TestClient(create_app(live))
    r = client.post("/backup/restore", files={"backup": ("x.db", b"garbage" * 200)},
                    follow_redirects=True)
    assert r.status_code == 200 and "look like a Chess Mistake Coach backup" in r.text
    assert _count(live, "games") == 1


def test_restore_is_refused_while_a_job_is_running(tmp_path):
    src = _populated(str(tmp_path / "src.db"))
    bk.create_backup(src, str(tmp_path / "b.db"))
    live = str(tmp_path / "live.db")
    open_db(live).close()
    app = create_app(live)
    app.state.jobs = MagicMock()
    app.state.jobs.get_active_job_id.return_value = "job"
    with open(tmp_path / "b.db", "rb") as f:
        r = TestClient(app).post("/backup/restore", files={"backup": ("b.db", f)},
                                 follow_redirects=True)
    assert "is running" in r.text
    assert _count(live, "games") == 0
