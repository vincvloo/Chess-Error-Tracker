from unittest.mock import MagicMock

import chess
from fastapi.testclient import TestClient

from chess_tracker.db import get_settings, open_db, save_game, set_settings
from chess_tracker.web.app import create_app
from chess_tracker.web.jobs import JobAlreadyRunningError

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
    assert 'href="/achievements?users=alice"' in r.text


def test_set_primary_user_persists_and_redirects_home(tmp_path):
    db_path = _empty_db(tmp_path)
    client = TestClient(create_app(db_path), follow_redirects=False)
    r = client.post("/account", data={"username": "Alice"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    conn = open_db(db_path)
    assert get_settings(conn)["primary_user"] == "alice"  # lowercased
    conn.close()


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


def test_job_status_endpoint_returns_404_for_unknown_job(tmp_path):
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
