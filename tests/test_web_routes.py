from unittest.mock import MagicMock

import chess
from fastapi.testclient import TestClient

from chess_tracker.db import open_db, save_game
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
    r = client.get("/practice", params={"users": "alice"})
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
    no_id = client.get("/practice", params={"users": "alice"})
    with_id = client.get("/practice/2", params={"users": "alice"})  # higher id sorts first
    assert no_id.status_code == with_id.status_code == 200
    assert _PROMOTION_FEN in no_id.text
    assert _PROMOTION_FEN in with_id.text


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


def test_home_page_lists_tracked_users(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "alice" in r.text


def test_home_page_with_empty_db_shows_no_users_message(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "No users tracked yet" in r.text


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
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "bob", "email": ""})
    assert r.status_code == 400
    assert "Email is required" in r.text


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
