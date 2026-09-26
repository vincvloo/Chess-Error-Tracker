import inspect
import re
from unittest.mock import MagicMock

import chess
import chess.engine
import pytest
from fastapi.testclient import TestClient

from chess_mistake_coach import analysis, coaching
from chess_mistake_coach.db import open_db
from chess_mistake_coach.web.app import create_app
from test_web_routes import (_empty_db, _fake_engine_path, _mock_analyze_engine,
                             _practice_seeded_db, _puzzle_seeded_db)

START = chess.STARTING_FEN


# ---- ratings and explanations ---------------------------------------------------

@pytest.mark.parametrize("cp_loss, best, expected", [
    (0, True, "best"), (300, True, "best"),          # playing the engine's move is always "best"
    (-40, False, "good"), (0, False, "good"), (49, False, "good"),
    (50, False, "inaccuracy"), (99, False, "inaccuracy"),
    (100, False, "mistake"), (249, False, "mistake"),
    (250, False, "blunder"), (5000, False, "blunder"),
])
def test_rate_move_uses_the_apps_own_thresholds(cp_loss, best, expected):
    assert coaching.rate_move(cp_loss, best) == expected


def test_every_category_the_classifier_can_return_has_an_explanation():
    categories = set(re.findall(r'return "([^"]+)"', inspect.getsource(analysis.classify)))
    assert len(categories) >= 12
    missing = [c for c in categories if not coaching.explain(c)]
    assert missing == []


def test_explain_is_empty_for_nothing_or_an_unknown_category():
    assert coaching.explain(None) == "" and coaching.explain("made up") == ""


# ---- /api/play/coach --------------------------------------------------------------

def _coach(client, monkeypatch, script, move=("d2d4",), fen=START, **extra):
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_analyze_engine(chess.WHITE, script))
    uci = move[0]
    return client.post("/api/play/coach", json={
        "fen": fen, "from": uci[:2], "to": uci[2:4], "promotion": uci[4:] or None, **extra})


def _client(tmp_path):
    return TestClient(create_app(_empty_db(tmp_path), engine_path=_fake_engine_path(tmp_path)))


def test_coach_rates_the_best_move_as_best(tmp_path, monkeypatch):
    r = _coach(_client(tmp_path), monkeypatch, [("e2e4", 30), ("e7e5", 25)], move=("e2e4",))
    body = r.json()
    assert body["rating"] == "best" and body["label"] == "Best move"
    assert body["category"] is None and body["lesson"] == ""
    assert body["bestUci"] == body["yourUci"] == "e2e4"


def test_coach_calls_a_small_loss_good(tmp_path, monkeypatch):
    r = _coach(_client(tmp_path), monkeypatch, [("e2e4", 30), ("d7d5", 5)])   # 25cp behind
    body = r.json()
    assert body["rating"] == "good" and body["category"] is None
    assert body["bestSan"] == "e4" and body["yourSan"] == "d4"


@pytest.mark.parametrize("after_cp, rating", [(-30, "inaccuracy"), (-120, "mistake"), (-300, "blunder")])
def test_coach_flags_errors_with_the_size_of_the_loss_and_a_lesson(tmp_path, monkeypatch, after_cp, rating):
    r = _coach(_client(tmp_path), monkeypatch, [("e2e4", 30), ("d7d5", after_cp)])
    body = r.json()
    assert body["rating"] == rating
    assert body["cpLoss"] == 30 - after_cp
    assert body["bestSan"] == "e4" and body["bestUci"] == "e2e4" and body["yourUci"] == "d2d4"
    assert body["category"] in coaching.CATEGORY_EXPLANATIONS
    assert body["lesson"] == coaching.explain(body["category"])


def test_coach_rejects_bad_requests(tmp_path, monkeypatch):
    client = _client(tmp_path)
    assert client.post("/api/play/coach", json={"from": "e2"}).status_code == 400
    assert client.post("/api/play/coach", json={"fen": "nonsense", "from": "e2", "to": "e4"}).status_code == 400
    # an inconsistent position (the side not to move already in check) must not reach the engine
    bad = "4k3/8/8/8/8/8/4R3/4K3 w - - 0 1"
    assert client.post("/api/play/coach", json={"fen": bad, "from": "e1", "to": "d1"}).status_code == 400


def test_coach_reports_an_illegal_move_without_calling_the_engine(tmp_path, monkeypatch):
    popen = MagicMock()
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci", popen)
    r = _client(tmp_path).post("/api/play/coach", json={"fen": START, "from": "e2", "to": "e5"})
    assert r.json() == {"legal": False}
    popen.assert_not_called()


def test_coach_degrades_gracefully_without_stockfish(tmp_path, monkeypatch):
    monkeypatch.setattr("chess_mistake_coach.web.routes_api.find_engine", lambda: None)
    client = TestClient(create_app(_empty_db(tmp_path), engine_path=None))
    r = client.post("/api/play/coach", json={"fen": START, "from": "e2", "to": "e4"})
    assert r.status_code == 503


def test_coach_survives_an_engine_crash(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise chess.engine.EngineTerminatedError()
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci", boom)
    r = _client(tmp_path).post("/api/play/coach", json={"fen": START, "from": "e2", "to": "e4"})
    assert r.status_code == 503


def test_coach_handles_promotions(tmp_path, monkeypatch):
    fen = "8/P6k/8/8/8/8/8/7K w - - 0 1"
    r = _coach(_client(tmp_path), monkeypatch, [("a7a8q", 900), ("h7g6", 890)], move=("a7a8q",), fen=fen)
    assert r.json()["rating"] == "best"


# ---- the arrows need the moves in the practice and puzzle responses ---------------------

def test_practice_attempt_returns_both_moves_and_the_lesson(tmp_path):
    client = TestClient(create_app(_practice_seeded_db(tmp_path)))
    body = client.post("/api/practice/1/attempt", json={"from": "d2", "to": "d4"}).json()
    assert body["verdict"] == "mistake"
    assert body["yourUci"] == "d2d4" and body["bestUci"] == "e2e4"
    assert body["category"] and body["lesson"] == coaching.explain(body["category"])


def test_a_failed_puzzle_attempt_returns_the_right_move_for_the_arrow(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    body = client.post("/api/puzzles/aaaaa/attempt",
                       json={"moveIndex": 1, "from": "g1", "to": "f1"}).json()
    assert body["correct"] is False
    assert body["bestUci"] == "e1e8" and body["yourUci"] == "g1f1"


# ---- take-backs and the skill rating ----------------------------------------------------

def _analyze(tmp_path, monkeypatch, **extra):
    db = _empty_db(tmp_path)
    monkeypatch.setattr("chess.engine.SimpleEngine.popen_uci",
                        lambda *a, **k: _mock_analyze_engine(chess.WHITE, [("e2e4", 30), ("d7d5", -170)]))
    client = TestClient(create_app(db, engine_path=_fake_engine_path(tmp_path)))
    r = client.post("/api/play/analyze", json={"moves": ["d2d4", "e7e5"], "colour": "white",
                                                "user": "alice", **extra})
    conn = open_db(db)
    rows = conn.execute("SELECT COUNT(*) FROM user_game_ratings").fetchone()[0]
    conn.close()
    return r.json(), rows


def test_a_game_without_take_backs_still_updates_the_rating(tmp_path, monkeypatch):
    body, rows = _analyze(tmp_path, monkeypatch, takebacks=0)
    assert body["ratingUpdated"] is True and rows > 0


def test_a_game_with_take_backs_does_not_change_the_rating(tmp_path, monkeypatch):
    body, rows = _analyze(tmp_path, monkeypatch, takebacks=2)
    assert body["ratingUpdated"] is False and rows == 0
    assert len(body["mistakes"]) == 1              # the analysis itself is still returned


# ---- pages ---------------------------------------------------------------------------------

def test_pages_carry_the_coaching_controls(tmp_path):
    (tmp_path / "puz").mkdir()
    (tmp_path / "pra").mkdir()
    puzzle_app = TestClient(create_app(_puzzle_seeded_db(tmp_path / "puz")))
    play = puzzle_app.get("/play", params={"users": "alice"}).text
    for hook in ('id="coachCheckbox"', 'id="takeBackBtn"', 'id="takeBackShowBtn"',
                 'id="arrows"', "/api/play/coach"):
        assert hook in play, hook
    puzzles = puzzle_app.get("/puzzles", params={"users": "alice", "minRating": 0,
                                                 "maxRating": 9999}).text
    assert 'id="arrows"' in puzzles and "showArrow(result.bestUci" in puzzles
    practice = TestClient(create_app(_practice_seeded_db(tmp_path / "pra"))).get(
        "/practice", params={"users": "alice", "category": "hung a pawn"}).text
    assert 'id="lesson"' in practice and 'id="arrows"' in practice
