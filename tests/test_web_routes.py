import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import chess
from fastapi.testclient import TestClient

from chess_tracker.analysis_runner import DEFAULT_PARALLEL_THRESHOLD, DEFAULT_WORKERS
from chess_tracker.db import get_settings, open_db, save_game, set_settings
from chess_tracker.web.app import create_app
from chess_tracker.web.jobs import (BIG_UPDATE_THRESHOLD, FIRST_RUN_GAME_LIMIT,
                                    JobAlreadyRunningError, SECONDS_PER_GAME)

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


def _seeded_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    conn.close()
    return db_path


def _empty_db(tmp_path) -> str:
    db_path = str(tmp_path / "empty.db")
    open_db(db_path).close()
    return db_path


def _fake_engine_path(tmp_path) -> str:
    path = tmp_path / "fake-stockfish"
    path.touch()
    return str(path)


_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# One white pawn from promoting, otherwise bare kings (kept far apart so the
# position is a legal one -- adjacent kings make python-chess treat it as an
# illegal "check" and only generate king moves). Several legal moves share
# the same from/to square (a7a8 with different promotion pieces).
_PROMOTION_FEN = "8/P6k/8/8/8/8/8/7K w - - 0 1"


def _practice_game(url="https://x/g1"):
    return {
        "url": url, "username": "alice", "end_time": 1000, "date": "2024-01-01",
        "time_class": "blitz", "my_colour": "white", "my_rating": 1500,
        "opp_rating": 1400, "result": "win", "eco": "C00",
        "moves_played": 20, "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
    }


def _practice_mistake(game_url, fen=_START_FEN, best="e4", move_number=1, phase="opening"):
    return {
        "game_url": game_url, "username": "alice", "date": "2024-01-01", "end_time": 1000,
        "time_class": "blitz", "my_rating": 1500, "my_colour": "white",
        "move_number": move_number, "phase": phase, "severity": "blunder", "cp_loss": 300,
        "category": "hung a pawn", "played": "d4", "best": best,
        "clock_seconds": 20.0, "fen": fen,
    }


def _practice_seeded_db(tmp_path, mistakes=None) -> str:
    db_path = str(tmp_path / "practice.db")
    conn = open_db(db_path)
    game = _practice_game()
    save_game(conn, game, mistakes if mistakes is not None else [_practice_mistake(game["url"])],
             depth=14)
    conn.close()
    return db_path


def test_practice_page_renders_position_without_revealing_best_move(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.get("/practice", params={"users": "alice", "category": "hung a pawn"})
    assert r.status_code == 200
    assert "alice" in r.text
    assert _START_FEN in r.text
    assert '"e2e4"' in r.text  # part of the embedded legal-move list

    # The no-peeking guarantee is about the embedded DATA payload
    # specifically -- not the whole page, which legitimately contains the
    # word "best" in unrelated client-side JS (e.g. the "best"/"also_fine"/
    # "mistake" verdict labels used once a move has actually been attempted).
    data_blob = r.text[r.text.index("const DATA = ") : r.text.index("const MISTAKE_ID")]
    assert '"best"' not in data_blob
    assert '"e4"' not in data_blob  # the answer SAN itself, not a substring of "e2e4" etc.
    assert ">e4<" not in r.text  # never rendered as visible page text either


def test_practice_page_with_no_id_resolves_to_the_queue_first_entry(tmp_path):
    db_path = _practice_seeded_db(tmp_path, mistakes=[
        _practice_mistake("https://x/g1", move_number=1),
        _practice_mistake("https://x/g1", move_number=2, fen=_PROMOTION_FEN, best="a8=Q"),
    ])
    client = TestClient(create_app(db_path))
    no_id = client.get("/practice", params={"users": "alice", "category": "hung a pawn"})
    with_id = client.get("/practice/2", params={"users": "alice"})  # higher id sorts first
    assert no_id.status_code == with_id.status_code == 200
    assert _PROMOTION_FEN in no_id.text
    assert _PROMOTION_FEN in with_id.text


def test_practice_page_with_no_category_shows_a_category_picker(tmp_path):
    db_path = _practice_seeded_db(tmp_path, mistakes=[
        _practice_mistake("https://x/g1", move_number=1),
    ])
    client = TestClient(create_app(db_path))
    r = client.get("/practice", params={"users": "alice"})
    assert r.status_code == 200
    assert "what do you want to work on" in r.text
    assert "hung a pawn" in r.text
    assert _START_FEN not in r.text  # no position picked yet, so no board data


def test_practice_page_404s_for_a_mistake_outside_the_scope(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.get("/practice/999", params={"users": "alice"})
    assert r.status_code == 404


def test_practice_page_empty_state_for_no_serious_mistakes(tmp_path):
    game = _practice_game()
    mistake = _practice_mistake(game["url"])
    mistake["severity"] = "inaccuracy"
    db_path = _practice_seeded_db(tmp_path, mistakes=[mistake])
    client = TestClient(create_app(db_path))
    r = client.get("/practice", params={"users": "alice"})
    assert r.status_code == 200
    assert "No stored mistakes to practice" in r.text


def test_practice_attempt_correct_move(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.post("/api/practice/1/attempt", json={"from": "e2", "to": "e4"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "legal": True, "verdict": "best", "correct": True,
        "yourSan": "e4", "bestSan": "e4",
        "yourFen": body["yourFen"], "bestFen": body["yourFen"],
    }


def test_practice_attempt_legal_but_wrong_move_with_no_engine_available(tmp_path):
    # create_app() here is given no engine_path, matching a machine with no
    # Stockfish installed -- practice mode must still work, just without the
    # engine-backed "also fine" nuance.
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.post("/api/practice/1/attempt", json={"from": "d2", "to": "d4"})
    assert r.status_code == 200
    body = r.json()
    assert body["legal"] is True
    assert body["verdict"] == "mistake"
    assert body["correct"] is False
    assert body["yourCpLoss"] is None
    assert body["yourSan"] == "d4"
    assert body["bestSan"] == "e4"


def _mock_engine_with_scores(mover_relative_before, mover_relative_after):
    """A stand-in SimpleEngine whose .analyse() returns the given scores in
    order, each relative to whichever side was to move in that position --
    exactly like a real engine's output, so score_cp() needs no changes to
    consume it."""
    import chess.engine as _e

    engine = MagicMock()
    engine.__enter__.return_value = engine
    engine.analyse.side_effect = [
        {"score": _e.PovScore(_e.Cp(mover_relative_before), chess.WHITE)},
        {"score": _e.PovScore(_e.Cp(mover_relative_after), chess.BLACK)},
    ]
    return engine


def test_practice_attempt_also_fine_when_engine_says_move_is_close(tmp_path, monkeypatch):
    app = create_app(_practice_seeded_db(tmp_path), engine_path="fake-stockfish")
    # white +30 before Nf3; after Nf3 (black to move), still +25 for white
    # relative to black as mover that's -25 -- a 5cp loss, well under MISTAKE.
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_engine_with_scores(30, -25))
    client = TestClient(app)
    r = client.post("/api/practice/1/attempt", json={"from": "g1", "to": "f3"})
    body = r.json()
    assert body["legal"] is True
    assert body["verdict"] == "also_fine"
    assert body["correct"] is False
    assert body["yourCpLoss"] == 5


def test_practice_attempt_mistake_when_engine_says_move_is_bad(tmp_path, monkeypatch):
    app = create_app(_practice_seeded_db(tmp_path), engine_path="fake-stockfish")
    # white +30 before Nf3; after Nf3, +170 relative to black as mover means
    # -170 for white -- a 200cp loss, well over MISTAKE.
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_engine_with_scores(30, 170))
    client = TestClient(app)
    r = client.post("/api/practice/1/attempt", json={"from": "g1", "to": "f3"})
    body = r.json()
    assert body["legal"] is True
    assert body["verdict"] == "mistake"
    assert body["correct"] is False
    assert body["yourCpLoss"] == 200


def test_practice_attempt_illegal_move(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.post("/api/practice/1/attempt", json={"from": "e2", "to": "e5"})
    assert r.status_code == 200
    assert r.json() == {"legal": False}


def test_practice_attempt_resolves_promotion(tmp_path):
    db_path = _practice_seeded_db(tmp_path, mistakes=[
        _practice_mistake("https://x/g1", fen=_PROMOTION_FEN, best="a8=Q"),
    ])
    client = TestClient(create_app(db_path))
    r = client.post("/api/practice/1/attempt",
                    json={"from": "a7", "to": "a8", "promotion": "q"})
    body = r.json()
    assert body["legal"] is True
    assert body["correct"] is True
    assert body["yourSan"] == "a8=Q"

    r2 = client.post("/api/practice/1/attempt",
                     json={"from": "a7", "to": "a8", "promotion": "n"})
    body2 = r2.json()
    assert body2["legal"] is True
    assert body2["correct"] is False
    assert body2["yourSan"] == "a8=N"


def test_practice_attempt_404s_for_unknown_mistake(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.post("/api/practice/999/attempt", json={"from": "e2", "to": "e4"})
    assert r.status_code == 404


def test_practice_hint_reveals_only_the_source_square_and_piece(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.get("/api/practice/1/hint")
    assert r.status_code == 200
    body = r.json()
    # best="e4" from the start position is a pawn push from e2
    assert body == {"square": "e2", "piece": "pawn"}
    assert "best" not in body
    assert "bestSan" not in body


def test_practice_hint_404s_for_unknown_mistake(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    r = client.get("/api/practice/999/hint")
    assert r.status_code == 404


# ---- practice attempt logging (practice_attempts table) -------------------

def _attempt_rows(db_path):
    conn = open_db(db_path)
    rows = conn.execute("SELECT * FROM practice_attempts").fetchall()
    conn.close()
    return rows


def test_practice_attempt_logs_a_row_when_practicing_user_given(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    r = client.post("/api/practice/1/attempt",
                    json={"from": "e2", "to": "e4", "practicingUser": "alice", "hintUsed": False})
    assert r.status_code == 200

    rows = _attempt_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["practicing_user"] == "alice"
    assert rows[0]["mistake_id"] == 1
    assert rows[0]["owner"] == "alice"
    assert rows[0]["category"] == "hung a pawn"
    assert rows[0]["verdict"] == "best"
    assert rows[0]["hint_used"] == 0


def test_practice_attempt_does_not_log_without_practicing_user(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    # same body as test_practice_attempt_correct_move -- no practicingUser --
    # must keep behaving exactly as before, and must not log anything.
    r = client.post("/api/practice/1/attempt", json={"from": "e2", "to": "e4"})
    assert r.status_code == 200
    assert _attempt_rows(db_path) == []


def test_practice_attempt_does_not_log_illegal_moves(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    r = client.post("/api/practice/1/attempt",
                    json={"from": "e2", "to": "e5", "practicingUser": "alice"})
    assert r.json() == {"legal": False}
    assert _attempt_rows(db_path) == []


def test_practice_attempt_logs_hint_used_flag(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    r = client.post("/api/practice/1/attempt",
                    json={"from": "d2", "to": "d4", "practicingUser": "alice", "hintUsed": True})
    assert r.json()["verdict"] == "mistake"

    rows = _attempt_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["hint_used"] == 1
    assert rows[0]["verdict"] == "mistake"


def test_practice_attempt_hint_free_retry_flips_position_to_solved(tmp_path):
    from chess_tracker.reports import practice_stats

    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))

    client.post("/api/practice/1/attempt",
               json={"from": "e2", "to": "e4", "practicingUser": "alice", "hintUsed": True})
    conn = open_db(db_path)
    assert practice_stats(conn, "alice")["overall"]["solved"] == 0
    conn.close()

    client.post("/api/practice/1/attempt",
               json={"from": "e2", "to": "e4", "practicingUser": "alice", "hintUsed": False})
    conn = open_db(db_path)
    assert practice_stats(conn, "alice")["overall"]["solved"] == 1
    conn.close()


def test_delete_one_practice_attempt(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    client.post("/api/practice/1/attempt",
               json={"from": "e2", "to": "e4", "practicingUser": "alice"})
    client.post("/api/practice/1/attempt",
               json={"from": "d2", "to": "d4", "practicingUser": "alice"})
    assert len(_attempt_rows(db_path)) == 2

    attempt_id = _attempt_rows(db_path)[0]["id"]
    r = client.delete(f"/api/practice-attempts/{attempt_id}")
    assert r.status_code == 200

    remaining = _attempt_rows(db_path)
    assert len(remaining) == 1
    assert remaining[0]["id"] != attempt_id


def test_reset_all_practice_attempts_for_a_user(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    client.post("/api/practice/1/attempt",
               json={"from": "e2", "to": "e4", "practicingUser": "alice"})
    client.post("/api/practice/1/attempt",
               json={"from": "d2", "to": "d4", "practicingUser": "alice"})
    assert len(_attempt_rows(db_path)) == 2

    r = client.delete("/api/practice-attempts", params={"user": "alice"})
    assert r.status_code == 200
    assert _attempt_rows(db_path) == []


def test_home_page_lists_tracked_users(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "alice" in r.text


def test_home_page_with_no_primary_user_shows_onboarding(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "Which account is yours?" in r.text


def test_home_page_shows_hub_once_primary_user_is_set(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, primary_user="alice")
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "Which account is yours?" not in r.text
    assert "alice" in r.text
    assert 'href="/practice?users=alice"' in r.text


def test_home_page_shows_compare_picker_for_other_tracked_players(tmp_path):
    db_path = str(tmp_path / "compare.db")
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    save_game(conn, {**REC, "username": "bob"}, [], depth=14)
    set_settings(conn, primary_user="alice")
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="compare-check" value="bob"' in r.text
    assert "Compare selected with alice" in r.text
    assert 'href="/achievements?users=alice"' in r.text


def test_home_hub_no_longer_shows_analyse_actions(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, primary_user="alice")
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "Analyse" not in r.text
    assert 'href="/analyse-more"' not in r.text


def test_home_hub_has_inline_add_player_field(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, primary_user="alice")
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "Add another player" in r.text
    assert 'name="user"' in r.text


def test_set_primary_user_persists_and_redirects_home(tmp_path):
    db_path = _empty_db(tmp_path)
    client = TestClient(create_app(db_path), follow_redirects=False)
    r = client.post("/account", data={"username": "Alice"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    conn = open_db(db_path)
    assert get_settings(conn)["primary_user"] == "alice"  # lowercased
    conn.close()


def test_onboarding_new_username_starts_capped_job_and_redirects_to_demo_dashboard(
        tmp_path, monkeypatch):
    app = create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    calls = {}

    def fake_start_job(users, email, engine_path, depth, threads, pause, **kwargs):
        calls["users"] = users
        calls["kwargs"] = kwargs
        return type("S", (), {"id": "fake-job-id"})()

    monkeypatch.setattr(app.state.jobs, "start_job", fake_start_job)
    client = TestClient(app, follow_redirects=False)
    r = client.post("/account", data={
        "username": "newplayer", "email": "you@example.com", "start_job": "1",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/demo-dashboard?job=fake-job-id"
    assert calls["users"] == ["newplayer"]
    assert calls["kwargs"]["limit"] == FIRST_RUN_GAME_LIMIT


def test_onboarding_missing_email_redirects_to_settings(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)), follow_redirects=False)
    r = client.post("/account", data={"username": "newplayer", "start_job": "1"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?needs_email=1"


def test_onboarding_missing_engine_shows_inline_error(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.routes_pages.find_engine", lambda: None)
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.post("/account", data={
        "username": "newplayer", "email": "you@example.com", "start_job": "1",
    })
    assert r.status_code == 400
    assert "Stockfish" in r.text


def test_account_switch_does_not_start_a_job(tmp_path, monkeypatch):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    save_game(conn, {**REC, "username": "bob"}, [], depth=14)
    set_settings(conn, primary_user="alice")
    conn.close()

    app = create_app(db_path, engine_path=_fake_engine_path(tmp_path))
    start_job_called = []
    monkeypatch.setattr(app.state.jobs, "start_job", lambda *a, **k: start_job_called.append(1))
    client = TestClient(app, follow_redirects=False)
    r = client.post("/account", data={"username": "bob"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert not start_job_called


def test_demo_dashboard_route_serves_demo_data(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/demo-dashboard")
    assert r.status_code == 200
    assert "demo" in r.text


def test_dashboard_route_returns_html_for_known_user(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard", params={"users": "alice"})
    assert r.status_code == 200
    assert "Chess Mistake Explorer" in r.text
    assert "alice" in r.text


def test_dashboard_route_with_unknown_user_shows_placeholder(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard", params={"users": "nosuchuser"})
    assert r.status_code == 200
    assert "No stored games" in r.text


def test_dashboard_route_requires_users_param(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard")
    assert r.status_code == 400


def test_start_job_redirects_to_progress_page(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    monkeypatch.setattr(app.state.jobs, "start_job",
                        lambda *a, **k: type("S", (), {"id": "fake-job-id"})())
    client = TestClient(app, follow_redirects=False)
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/fake-job-id"


def test_start_job_requires_a_username(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "  ", "email": "you@example.com"})
    assert r.status_code == 400
    assert "Enter at least one" in r.text


def test_start_job_requires_email(tmp_path):
    # Neither Analyse nor Analyse-more-players shows an email field any
    # more (it's a saved setting), so there's nothing to fix inline --
    # missing email sends the user to the parameters page instead of a 400.
    client = TestClient(create_app(_seeded_db(tmp_path)), follow_redirects=False)
    r = client.post("/jobs", data={"user": "bob", "email": ""})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?needs_email=1"


def test_start_job_reports_missing_engine(tmp_path, monkeypatch):
    # no engine_path passed to create_app(), and find_engine() monkeypatched
    # to simulate a machine with no Stockfish installed
    monkeypatch.setattr("chess_tracker.web.routes_pages.find_engine", lambda: None)
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 400
    assert "Stockfish" in r.text


def test_start_job_rejects_when_already_running(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))

    def raise_running(*a, **k):
        raise JobAlreadyRunningError("busy")

    monkeypatch.setattr(app.state.jobs, "start_job", raise_running)
    client = TestClient(app)
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 409


def test_cancelling_does_not_free_the_slot_until_the_thread_actually_exits(tmp_path, monkeypatch):
    # Regression for the "narrow the date range" flow: cancelling only sets
    # a cooperative flag the background thread checks between games, so the
    # slot stays taken for a moment after /cancel returns. Submitting a new
    # job right away used to race this and hit 409; the fix (in progress.html)
    # is to poll /api/jobs/{id} until it's no longer running/queued before
    # resubmitting. This proves both halves: the race is real, and waiting
    # for the real state (not just the cancel call) resolves it.
    db_path = _seeded_db(tmp_path)
    app = create_app(db_path, engine_path=_fake_engine_path(tmp_path))
    started = threading.Event()
    release = threading.Event()

    def slow_run_analysis(conn, users, email, engine_path, depth, threads, pause,
                          progress_cb=None, cancel_event=None, **kwargs):
        started.set()
        release.wait(timeout=2)  # only "notices" cancellation once released

    monkeypatch.setattr("chess_tracker.web.jobs.run_analysis", slow_run_analysis)
    client = TestClient(app, follow_redirects=False)

    r1 = client.post("/jobs", data={"user": "alice", "email": "you@example.com"})
    assert r1.status_code == 303
    job_id = r1.headers["location"].split("/")[-1]
    assert started.wait(timeout=2)

    client.post(f"/api/jobs/{job_id}/cancel")
    # Immediately after /cancel, the thread hasn't actually stopped yet --
    # the slot is still taken.
    r2 = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r2.status_code == 409

    release.set()
    deadline = time.time() + 2
    while time.time() < deadline and app.state.jobs.get_active_job_id() is not None:
        time.sleep(0.01)
    assert app.state.jobs.get_active_job_id() is None

    r3 = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r3.status_code == 303


def test_active_job_endpoint_returns_null_when_idle(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/jobs/active")
    assert r.status_code == 200
    assert r.json() == {"job_id": None}


def test_active_job_endpoint_returns_running_job_id(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path))
    monkeypatch.setattr(app.state.jobs, "get_active_job_id", lambda: "fake-job-id")
    r = TestClient(app).get("/api/jobs/active")
    assert r.status_code == 200
    assert r.json() == {"job_id": "fake-job-id"}


def test_job_status_endpoint_returns_404_for_unknown_job(tmp_path):
    # regression guard: /api/jobs/active must be registered ahead of
    # /api/jobs/{job_id}, or "active" gets swallowed as a job_id and this
    # route (and the two above) would misbehave
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/jobs/nonexistent")
    assert r.status_code == 404


def test_job_cancel_endpoint_returns_404_for_unknown_job(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/api/jobs/nonexistent/cancel")
    assert r.status_code == 404


def test_job_progress_page_404s_for_unknown_job(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/jobs/nonexistent")
    assert r.status_code == 404


def test_job_progress_page_includes_big_update_threshold_and_settings(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    monkeypatch.setattr(app.state.jobs, "get_status",
                        lambda job_id: {"users": ["alice"], "state": "running"})
    client = TestClient(app)
    r = client.get("/jobs/whatever")
    assert r.status_code == 200
    assert str(BIG_UPDATE_THRESHOLD) in r.text
    assert 'name="since"' in r.text
    assert "const PARALLEL_THRESHOLD = " + str(DEFAULT_PARALLEL_THRESHOLD) in r.text
    # With no run history the parallel guess is the fixed per-game guess
    # spread across the workers.
    assert "const SECONDS_PER_GAME_PARALLEL = " + str(SECONDS_PER_GAME / DEFAULT_WORKERS) in r.text
    assert "const SECONDS_PER_GAME_SERIAL = " + str(SECONDS_PER_GAME) in r.text


def test_user_density_endpoint_returns_todo_counts_and_fills_gap_months(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    conn.execute("INSERT INTO archives (url, username, month, game_count, complete) "
                 "VALUES (?, ?, ?, ?, 1)", ("https://x/archive1", "alice", "2026-06", 5))
    # 2026-07 deliberately has no archive row -- a quiet month Chess.com's
    # own monthly listing skips -- to prove it still shows up as a real
    # zero-games month rather than just being absent from the picker.
    conn.execute("INSERT INTO archives (url, username, month, game_count, complete) "
                 "VALUES (?, ?, ?, ?, 1)", ("https://x/archive2", "alice", "2026-08", 3))
    # Two of 2026-06's 5 archived games are already analysed at depth 14 --
    # only the remaining 3 should count as still needing analysis.
    conn.execute("""INSERT INTO games (url, username, date, depth)
                    VALUES ('https://x/a1', 'alice', '2026-06-05', 14),
                           ('https://x/a2', 'alice', '2026-06-10', 14)""")
    conn.commit()
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/api/users/alice/density?depth=14")
    assert r.status_code == 200
    density = {d["month"]: d["games"] for d in r.json()["density"]}
    assert density["2026-06"] == 3  # 5 archived, 2 already in the local db
    assert density["2026-07"] == 0  # gap month, filled in rather than missing
    assert density["2026-08"] == 3  # none of these analysed yet
    today_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert today_month in density  # the range always reaches the current month


def test_user_density_endpoint_deeper_reanalysis_counts_everything_as_todo(tmp_path):
    # A game analysed at a shallower depth than requested doesn't count as
    # "already analysed" (db.already_analysed()'s own rule) -- so it must
    # still show up as needing (re-)analysis.
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    conn.execute("INSERT INTO archives (url, username, month, game_count, complete) "
                 "VALUES (?, ?, ?, ?, 1)", ("https://x/archive1", "alice", "2026-06", 2))
    conn.execute("""INSERT INTO games (url, username, date, depth)
                    VALUES ('https://x/a1', 'alice', '2026-06-05', 10)""")
    conn.commit()
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/api/users/alice/density?depth=14")
    assert r.status_code == 200
    density = {d["month"]: d["games"] for d in r.json()["density"]}
    assert density["2026-06"] == 2


def test_user_density_endpoint_empty_for_unknown_user(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/users/nosuchuser/density")
    assert r.status_code == 200
    assert r.json() == {"density": []}


def test_job_progress_page_data_link_points_at_the_jobs_own_users(tmp_path, monkeypatch):
    # regression: this used to link to "/" and dropped the user back on the
    # home page instead of back at the dashboard/practice/compare view they
    # were updating.
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    monkeypatch.setattr(app.state.jobs, "get_status",
                        lambda job_id: {"users": ["alice", "bob"], "state": "running"})
    client = TestClient(app)
    r = client.get("/jobs/whatever")
    assert r.status_code == 200
    assert 'href="/dashboard?users=alice%2Cbob"' in r.text


# ---- settings page ----------------------------------------------------

def test_settings_page_shows_current_values(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, email="me@example.com", depth=18)
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'value="me@example.com"' in r.text
    assert 'value="18"' in r.text


def test_settings_page_save_persists_and_redirects(tmp_path):
    db_path = _seeded_db(tmp_path)
    client = TestClient(create_app(db_path), follow_redirects=False)
    r = client.post("/settings", data={
        "email": "new@example.com", "depth": "20", "threads": "4",
        "pause": "1.2", "min_loss": "60",
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"

    conn = open_db(db_path)
    settings = get_settings(conn)
    conn.close()
    assert settings["email"] == "new@example.com"
    assert settings["depth"] == 20
    assert settings["pause"] == 1.2


# ---- achievements page --------------------------------------------------

def _achievement_game(url, username, date, moves=100, om=100, mm=0, em=0):
    return {
        "url": url, "username": username, "end_time": 1000, "date": date,
        "time_class": "blitz", "my_colour": "white", "my_rating": 1500,
        "opp_rating": 1400, "result": "win", "eco": "C00",
        "moves_played": moves, "opening_moves": om, "middlegame_moves": mm,
        "endgame_moves": em,
    }


def _achievement_mistake(url, username, date, category):
    return {
        "game_url": url, "username": username, "date": date, "end_time": 1000,
        "time_class": "blitz", "my_rating": 1500, "my_colour": "white",
        "move_number": 5, "phase": "opening", "severity": "blunder", "cp_loss": 300,
        "category": category, "played": "d4", "best": "e4",
        "clock_seconds": 20.0, "fen": _START_FEN,
    }


def test_achievements_page_with_no_data_shows_empty_state(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/achievements", params={"users": "nobody"})
    assert r.status_code == 200
    assert "nothing to show here" in r.text


def test_achievements_page_requires_a_user(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/achievements")
    assert r.status_code == 400


def test_achievements_page_shows_category_trend_deltas(tmp_path):
    db_path = str(tmp_path / "ach.db")
    conn = open_db(db_path)
    save_game(conn, _achievement_game("https://x/a1", "alice", "2024-01-01"),
             [_achievement_mistake("https://x/a1", "alice", "2024-01-01", "beta"),
              _achievement_mistake("https://x/a1", "alice", "2024-01-01", "beta")],
             depth=14)
    save_game(conn, _achievement_game("https://x/a2", "alice", "2024-02-01"), [], depth=14)
    save_game(conn, _achievement_game("https://x/a3", "alice", "2024-03-01"), [], depth=14)
    save_game(conn, _achievement_game("https://x/a4", "alice", "2024-04-01"),
             [_achievement_mistake("https://x/a4", "alice", "2024-04-01", "alpha"),
              _achievement_mistake("https://x/a4", "alice", "2024-04-01", "alpha")],
             depth=14)
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/achievements", params={"users": "alice"})
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" in r.text
    assert "Most improved" in r.text


def test_achievements_page_with_no_practice_attempts_shows_empty_practice_state(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    r = client.get("/achievements", params={"users": "alice"})
    assert r.status_code == 200
    assert "0 of 1 recorded positions" in r.text
    assert "No successful attempts yet" in r.text
    assert "No practice attempts recorded yet" in r.text


def test_achievements_page_shows_practice_stats(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    client.post("/api/practice/1/attempt",
               json={"from": "e2", "to": "e4", "practicingUser": "alice", "hintUsed": False})

    r = client.get("/achievements", params={"users": "alice"})
    assert r.status_code == 200
    assert "1 of 1 recorded positions" in r.text
    assert "1 solved" in r.text
    assert "(100%)" in r.text
    assert "Recent practice sessions" in r.text
    assert "hung a pawn" in r.text


# ---- practice: extend to other players' mistakes -------------------------

def test_practice_extend_pulls_in_other_players_tagged_by_owner(tmp_path):
    db_path = str(tmp_path / "extend.db")
    conn = open_db(db_path)
    save_game(conn, _achievement_game("https://x/a1", "alice", "2024-01-01"),
             [_achievement_mistake("https://x/a1", "alice", "2024-01-01", "hung a piece")],
             depth=14)
    save_game(conn, _achievement_game("https://x/b1", "bob", "2024-01-01"),
             [_achievement_mistake("https://x/b1", "bob", "2024-01-01", "hung a piece"),
              _achievement_mistake("https://x/b1", "bob", "2024-01-01", "hung a piece")],
             depth=14)
    conn.close()

    client = TestClient(create_app(db_path))
    picked = client.get("/practice", params={"users": "alice", "category": "hung a piece"})
    assert picked.status_code == 200
    assert "Include them" in picked.text  # alice only has 1, bob has 2 more available

    extended = client.get("/practice", params={
        "users": "alice", "category": "hung a piece", "extend": "1"})
    assert extended.status_code == 200
    assert extended.text.count("position 1 of 3") == 1  # alice's 1 + bob's 2, pooled
    assert "Include them" not in extended.text

    # Once extended, stepping through the queue with Next must keep carrying
    # extend=1 -- otherwise a pooled (other player's) position id looked up
    # again under the un-extended (own-only) queue 404s.
    import re
    next_href = re.search(r'id="nextLink" href="([^"]+)"', extended.text).group(1)
    assert "extend=1" in next_href
    followed = client.get(next_href.replace("&amp;", "&"))
    assert followed.status_code == 200
    assert "position 2 of 3" in followed.text
    assert "@bob" in followed.text  # a pooled row, tagged with its owner


def test_achievements_page_others_column_uses_pooled_mistakes(tmp_path):
    db_path = str(tmp_path / "extend_stats.db")
    conn = open_db(db_path)
    save_game(conn, _achievement_game("https://x/a1", "alice", "2024-01-01"),
             [_achievement_mistake("https://x/a1", "alice", "2024-01-01", "hung a piece")],
             depth=14)
    save_game(conn, _achievement_game("https://x/b1", "bob", "2024-01-01"),
             [_achievement_mistake("https://x/b1", "bob", "2024-01-01", "hung a piece"),
              _achievement_mistake("https://x/b1", "bob", "2024-01-01", "hung a piece")],
             depth=14)
    bob_id = conn.execute(
        "SELECT id FROM mistakes WHERE username = 'bob' ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()

    client = TestClient(create_app(db_path))
    attempt = client.post(f"/api/practice/{bob_id}/attempt",
                          json={"from": "e2", "to": "e4", "practicingUser": "alice",
                                "hintUsed": False})
    assert attempt.json()["verdict"] == "best"

    page = client.get("/achievements", params={"users": "alice"})
    assert page.status_code == 200
    assert "hung a piece" in page.text
    assert "1/2" in page.text  # 1 of bob's 2 mistakes in this category solved by alice


# ---- update checker -----------------------------------------------------

def test_update_check_returns_available_true(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr("chess_tracker.web.updater.check_for_update",
                        lambda git_path, repo: {"available": True, "reason": None})
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/update/check")
    assert r.status_code == 200
    assert r.json() == {"available": True}


def test_update_check_returns_available_false_when_git_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: None)
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/update/check")
    assert r.json() == {"available": False}


def test_update_check_is_cached_within_the_ttl(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: "/usr/bin/git")

    def fake_check(git_path, repo):
        calls.append(1)
        return {"available": False, "reason": None}

    monkeypatch.setattr("chess_tracker.web.updater.check_for_update", fake_check)
    client = TestClient(create_app(_seeded_db(tmp_path)))
    client.get("/api/update/check")
    client.get("/api/update/check")
    assert len(calls) == 1


def test_home_hub_includes_update_banner_markup(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, primary_user="alice")
    conn.close()
    client = TestClient(create_app(db_path))
    r = client.get("/")
    assert r.status_code == 200
    # Always present, hidden by default -- JS decides visibility from
    # /api/update/check, so this just confirms the hook point exists.
    assert 'id="updateBanner"' in r.text
    assert 'style="display:none"' in r.text


def test_update_apply_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr("chess_tracker.web.updater.apply_update",
                        lambda git_path, repo: {"ok": True, "message": "Updated!"})
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/api/update/apply")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Updated!"}


def test_update_apply_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: "/usr/bin/git")
    monkeypatch.setattr("chess_tracker.web.updater.apply_update",
                        lambda git_path, repo: {"ok": False, "message": "local changes present"})
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/api/update/apply")
    assert r.json() == {"ok": False, "message": "local changes present"}


def test_update_apply_without_git_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.updater.find_git", lambda: None)
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/api/update/apply")
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ---- play mode (phase 5) --------------------------------------------------

def _mock_play_engine(bot_move_uci):
    """A stand-in SimpleEngine for play-mode tests: analyse() returns one
    info dict with the given move as pv[0], regardless of multipv -- these
    tests only need *a* legal bot move back, not to exercise steering
    itself (see tests/test_bot.py for that)."""
    engine = MagicMock()
    engine.__enter__.return_value = engine
    engine.analyse.return_value = {
        "score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
        "pv": [chess.Move.from_uci(bot_move_uci)],
    }
    return engine


def test_play_move_legal_move_gets_a_bot_reply(tmp_path, monkeypatch):
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_play_engine("e7e6"))
    app = create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    r = TestClient(app).post("/api/play/move", json={
        "fen": _START_FEN, "from": "e2", "to": "e4", "engine": "stockfish", "elo": 1500,
    })
    body = r.json()
    assert body["legal"] is True
    assert body["botMove"] == "e7e6"
    assert body["gameOver"] is False
    app.state.play_engine.close()


def test_play_move_illegal_move_rejected(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.post("/api/play/move", json={
        "fen": _START_FEN, "from": "e2", "to": "e5", "engine": "stockfish", "elo": 1500,
    })
    assert r.json() == {"legal": False}


def test_play_move_rejects_invalid_position(tmp_path):
    # Side not to move already in check -- syntactically valid FEN, but an
    # impossible chess position. Verified directly against a real engine
    # that feeding this through unchecked crashes the binary outright
    # (access violation), so this must be rejected before reaching the
    # engine at all.
    client = TestClient(create_app(_empty_db(tmp_path)))
    bad_fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR b KQkq - 1 2"
    r = client.post("/api/play/move", json={
        "fen": bad_fen, "from": "h4", "to": "e1", "engine": "stockfish", "elo": 1500,
    })
    assert r.status_code == 400


def test_play_move_checkmate_ends_game_without_consulting_engine(tmp_path, monkeypatch):
    # If the engine were consulted here, this would raise -- proving the
    # checkmate short-circuit happens before any engine call.
    def _boom(*a, **k):
        raise AssertionError("engine should not be consulted after checkmate")
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci", _boom)

    app = create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    board = chess.Board()
    for uci in ["f2f3", "e7e5", "g2g4"]:
        board.push(chess.Move.from_uci(uci))
    r = TestClient(app).post("/api/play/move", json={
        "fen": board.fen(), "from": "d8", "to": "h4", "engine": "stockfish", "elo": 1500,
    })
    body = r.json()
    assert body["gameOver"] is True
    assert body["botMove"] is None
    assert body["outcome"] == "black"
    assert body["termination"] == "checkmate"


def test_play_move_adaptive_with_no_data_has_no_effect(tmp_path, monkeypatch):
    # No mistakes at all for this user/time_class -- eligible_phases() comes
    # back empty, so choose_bot_move must take the no-MultiPV path. A mock
    # that asserts multipv is None proves adaptive never actually engaged.
    engine = MagicMock()
    engine.__enter__.return_value = engine

    def analyse(board, limit, multipv=None):
        assert multipv is None, "adaptive steering should not engage with zero eligible data"
        return {"score": chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
                "pv": [chess.Move.from_uci("e7e6")]}
    engine.analyse.side_effect = analyse
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci", lambda *a, **k: engine)

    app = create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    r = TestClient(app).post("/api/play/move", json={
        "fen": _START_FEN, "from": "e2", "to": "e4", "engine": "stockfish", "elo": 1500,
        "adaptive": True, "timeClass": "blitz", "user": "nobody",
    })
    body = r.json()
    assert body["legal"] is True
    assert body["botMove"] == "e7e6"


def test_play_move_stockfish_not_installed_degrades_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.routes_api.find_engine", lambda: None)
    client = TestClient(create_app(_empty_db(tmp_path), engine_path=None))
    r = client.post("/api/play/move", json={
        "fen": _START_FEN, "from": "e2", "to": "e4", "engine": "stockfish", "elo": 1500,
    })
    assert r.status_code == 503


def test_play_first_move_bot_opens_as_white(tmp_path, monkeypatch):
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_play_engine("e2e4"))
    app = create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    r = TestClient(app).post("/api/play/first-move", json={"engine": "stockfish", "elo": 1500})
    assert r.json()["botMove"] == "e2e4"


def test_play_legal_moves_endpoint(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/api/play/legal-moves", params={"fen": _START_FEN})
    assert "e2e4" in r.json()["legalMoves"]


def test_play_page_renders(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/play", params={"users": "alice"})
    assert r.status_code == 200
    assert "alice" in r.text


def _mock_analyze_engine(me, script):
    """Same scripted-(uci, cp)-pairs idea as _ScriptedEngine in
    test_analysis.py, wrapped as a context-manager mock since /api/play/
    analyze opens its engine with `with ... as engine:`."""
    engine = MagicMock()
    engine.__enter__.return_value = engine
    calls = list(script)

    def analyse(board, limit):
        uci, cp = calls.pop(0)
        return {"score": chess.engine.PovScore(chess.engine.Cp(cp), me),
                "pv": [chess.Move.from_uci(uci)]}
    engine.analyse.side_effect = analyse
    return engine


def test_play_analyze_returns_mistakes_for_a_short_game(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chess.engine.SimpleEngine.popen_uci",
        lambda *a, **k: _mock_analyze_engine(chess.WHITE, [("e2e4", 30), ("d7d5", -170)]))
    client = TestClient(create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path)))
    r = client.post("/api/play/analyze", json={"moves": ["d2d4", "e7e5"], "colour": "white"})
    body = r.json()
    assert body["totalMoves"] == 2
    assert len(body["mistakes"]) == 1
    assert body["mistakes"][0]["played"] == "d4"
    assert body["mistakes"][0]["best"] == "e4"


def test_play_analyze_rejects_illegal_move_list(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.post("/api/play/analyze", json={"moves": ["e2e4", "e2e4"], "colour": "white"})
    assert r.status_code == 400


def test_play_analyze_requires_moves_and_colour(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    assert client.post("/api/play/analyze", json={"moves": [], "colour": "white"}).status_code == 400
    assert client.post("/api/play/analyze",
                       json={"moves": ["e2e4"], "colour": "purple"}).status_code == 400


def test_play_analyze_stockfish_not_installed_degrades_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.routes_api.find_engine", lambda: None)
    client = TestClient(create_app(_empty_db(tmp_path), engine_path=None))
    r = client.post("/api/play/analyze", json={"moves": ["e2e4"], "colour": "white"})
    assert r.status_code == 503


# ---- puzzles -------------------------------------------------------------

# A real mate-in-1: black plays an irrelevant pawn move, white delivers Re8#.
_PUZZLE_FEN = "6k1/p4ppp/8/8/8/8/5PPP/4R1K1 b - - 0 1"
_PUZZLE_MOVES = "a7a6 e1e8"


def _puzzle_seeded_db(tmp_path) -> str:
    db_path = str(tmp_path / "puzzles.db")
    conn = open_db(db_path)
    conn.execute("""
        INSERT INTO puzzles (puzzle_id, fen, moves, rating, rating_deviation,
                             popularity, nb_plays, themes, game_url, opening_tags, imported_at)
        VALUES ('aaaaa', ?, ?, 900, 80, 90, 5000, ' mateIn1 backRankMate ',
                'https://lichess.org/abc', '', '2026-01-01T00:00:00+00:00')
    """, (_PUZZLE_FEN, _PUZZLE_MOVES))
    conn.commit()
    conn.close()
    return db_path


def test_puzzles_page_renders_the_puzzle_position_not_the_pre_setup_fen(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    r = client.get("/puzzles", params={"users": "alice", "minRating": 0, "maxRating": 9999})
    assert r.status_code == 200
    # The pre-setup-move FEN should never be shown as the starting position --
    # only the position after moves[0] (a7a6) is applied.
    assert _PUZZLE_FEN not in r.text
    assert "e1e8" in r.text  # part of the embedded legal-move list


def test_puzzles_page_shows_empty_state_when_nothing_matches(tmp_path):
    # _puzzle_seeded_db inserts a puzzle directly via SQL, bypassing
    # import_puzzles() -- so puzzle_source_stats (only ever populated by a
    # real import) needs seeding too, to hit the "narrow filter, but
    # puzzles genuinely exist elsewhere" message rather than the
    # never-imported-anything one.
    db_path = _puzzle_seeded_db(tmp_path)
    conn = open_db(db_path)
    conn.execute("INSERT INTO puzzle_source_stats (bucket_key, total_count, updated_at) "
                "VALUES ('total', 1, '2026-01-01')")
    conn.commit()
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.get("/puzzles", params={"users": "alice", "minRating": 2000, "maxRating": 2100})
    assert r.status_code == 200
    assert "No local puzzles match" in r.text


def test_puzzles_page_shows_never_imported_state_when_db_is_truly_empty(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/puzzles", params={"users": "alice"})
    assert r.status_code == 200
    assert "No puzzles have been imported yet" in r.text


def test_puzzle_attempt_correct_solves_a_one_move_puzzle(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    r = client.post("/api/puzzles/aaaaa/attempt",
                    json={"moveIndex": 1, "from": "e1", "to": "e8", "practicingUser": "alice"})
    body = r.json()
    assert body["legal"] is True
    assert body["correct"] is True
    assert body["solved"] is True
    assert body["yourSan"] == "Re8#"


def test_puzzle_attempt_wrong_move_reveals_the_solution(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    r = client.post("/api/puzzles/aaaaa/attempt",
                    json={"moveIndex": 1, "from": "e1", "to": "e2", "practicingUser": "alice"})
    body = r.json()
    assert body["legal"] is True
    assert body["correct"] is False
    assert body["solved"] is False
    assert body["bestSan"] == "Re8#"


def test_puzzle_attempt_illegal_move(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    r = client.post("/api/puzzles/aaaaa/attempt",
                    json={"moveIndex": 1, "from": "e1", "to": "e9"})
    assert r.json() == {"legal": False}


def test_puzzle_attempt_unknown_puzzle_404s(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    r = client.post("/api/puzzles/zzzzz/attempt",
                    json={"moveIndex": 1, "from": "e1", "to": "e8"})
    assert r.status_code == 404


def test_puzzle_attempt_continues_a_multi_move_puzzle(tmp_path):
    db_path = str(tmp_path / "multi.db")
    conn = open_db(db_path)
    # Setup(a2a3, white), solver correct(g8f6, black), opponent auto-reply
    # (b1c3, white) -- puzzle isn't solved yet, one more solver move (f6e4)
    # remains at index 3. Realistic shape: total length is always even,
    # always ending on a solver move (see puzzle_attempt's docstring).
    conn.execute("""
        INSERT INTO puzzles (puzzle_id, fen, moves, rating, rating_deviation,
                             popularity, nb_plays, themes, game_url, opening_tags, imported_at)
        VALUES ('multi1', 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                'a2a3 g8f6 b1c3 f6e4', 1000, 80, 90, 100, ' opening ', '', '', '2026-01-01')
    """)
    conn.commit()
    conn.close()

    client = TestClient(create_app(db_path))
    r = client.post("/api/puzzles/multi1/attempt",
                    json={"moveIndex": 1, "from": "g8", "to": "f6", "practicingUser": "alice"})
    body = r.json()
    assert body["legal"] is True
    assert body["correct"] is True
    assert body["solved"] is False
    assert body["opponentMove"] == "Nc3"
    assert body["nextMoveIndex"] == 3

    # Finishing the puzzle from the returned nextMoveIndex should solve it.
    r2 = client.post("/api/puzzles/multi1/attempt",
                     json={"moveIndex": 3, "from": "f6", "to": "e4", "practicingUser": "alice"})
    body2 = r2.json()
    assert body2["correct"] is True
    assert body2["solved"] is True


def test_puzzle_attempt_logs_solved_and_failed_verdicts(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    client.post("/api/puzzles/aaaaa/attempt",
                json={"moveIndex": 1, "from": "e1", "to": "e2", "practicingUser": "alice"})
    conn = open_db(db_path)
    rows = conn.execute("SELECT * FROM puzzle_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "failed"
    assert rows[0]["practicing_user"] == "alice"


def test_puzzle_attempt_does_not_log_without_practicing_user(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    client.post("/api/puzzles/aaaaa/attempt", json={"moveIndex": 1, "from": "e1", "to": "e8"})
    conn = open_db(db_path)
    assert conn.execute("SELECT COUNT(*) AS n FROM puzzle_attempts").fetchone()["n"] == 0


# ---- puzzles: "get more puzzles" background import ----------------------

def test_start_puzzle_import_returns_job_status(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles",
                        lambda conn, source_path, **kwargs: None)
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.post("/api/puzzles/import", params={"minRating": 1000, "maxRating": 2000})
    body = r.json()
    assert "id" in body
    assert body["state"] in ("queued", "downloading", "importing", "done")


def test_active_puzzle_import_returns_null_when_idle(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    assert client.get("/api/puzzles/import/active").json() == {"job_id": None}


def test_puzzle_import_status_404s_for_unknown_job(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/api/puzzles/import/no-such-job")
    assert r.status_code == 404


def test_puzzle_import_cancel_404s_for_unknown_job(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.post("/api/puzzles/import/no-such-job/cancel")
    assert r.status_code == 404


def test_start_puzzle_import_conflicts_when_already_running(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr("chess_tracker.web.puzzle_import.find_puzzle_source",
                        lambda: "/cached.csv")

    def fake_import_puzzles(conn, source_path, **kwargs):
        started.set()
        release.wait(timeout=2)
    monkeypatch.setattr("chess_tracker.web.puzzle_import.import_puzzles", fake_import_puzzles)

    client = TestClient(create_app(_empty_db(tmp_path)))
    client.post("/api/puzzles/import", params={})
    assert started.wait(timeout=2)

    r = client.post("/api/puzzles/import", params={})
    assert r.status_code == 409

    release.set()

