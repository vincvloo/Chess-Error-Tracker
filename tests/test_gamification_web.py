"""Full-stack route tests for the gamification features (streaks, skill rating,
badges, daily puzzle, puzzle rush, leaderboard). Seeding helpers are shared
with test_web_routes.py."""

import chess
from fastapi.testclient import TestClient

from chess_tracker.db import open_db, save_game, set_settings
from chess_tracker.web.app import create_app
from test_web_routes import (MISTAKE, REC, _empty_db, _fake_engine_path, _mock_analyze_engine,
                             _practice_seeded_db, _puzzle_seeded_db, _seeded_db)

SOLVE = {"moveIndex": 1, "from": "e1", "to": "e8", "practicingUser": "alice"}


def _rows(db_path, sql, params=()):
    conn = open_db(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _set_rating(db_path, rating, user="alice"):
    conn = open_db(db_path)
    conn.execute("INSERT INTO user_puzzle_ratings (username, theme, rating, puzzles_seen, "
                 "updated_at) VALUES (?, '', ?, 10, 'x')", (user, rating))
    conn.commit()
    conn.close()


def test_solving_a_puzzle_moves_rating_streak_and_awards_a_badge(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    body = TestClient(create_app(db_path)).post("/api/puzzles/aaaaa/attempt", json=SOLVE).json()
    assert body["solved"] is True
    assert [b["code"] for b in body["newBadges"]] == ["first_puzzle"]
    ratings = {r["theme"]: r["rating"] for r in _rows(
        db_path, "SELECT theme, rating FROM user_puzzle_ratings WHERE username = 'alice'")}
    assert set(ratings) == {"", "mateIn1", "backRankMate"}
    assert all(v > 1200 for v in ratings.values())
    assert _rows(db_path, "SELECT current_streak FROM user_streaks")[0]["current_streak"] == 1


def test_failing_a_puzzle_lowers_the_rating_but_still_counts_the_day(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    TestClient(create_app(db_path)).post(
        "/api/puzzles/aaaaa/attempt",
        json={"moveIndex": 1, "from": "g1", "to": "f1", "practicingUser": "alice"})
    rating = _rows(db_path, "SELECT rating FROM user_puzzle_ratings WHERE theme = ''")[0]["rating"]
    assert rating < 1200
    assert _rows(db_path, "SELECT 1 FROM user_streaks")


def test_puzzle_attempt_without_a_user_touches_no_gamification_state(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    r = TestClient(create_app(db_path)).post(
        "/api/puzzles/aaaaa/attempt", json={"moveIndex": 1, "from": "e1", "to": "e8"})
    assert r.json()["newBadges"] == []
    assert _rows(db_path, "SELECT 1 FROM user_puzzle_ratings") == []
    assert _rows(db_path, "SELECT 1 FROM user_streaks") == []


def test_practice_attempt_counts_toward_the_streak_and_awards_first_practice(tmp_path):
    db_path = _practice_seeded_db(tmp_path)
    r = TestClient(create_app(db_path)).post(
        "/api/practice/1/attempt",
        json={"from": "e2", "to": "e4", "practicingUser": "alice", "hintUsed": False})
    assert "first_practice" in [b["code"] for b in r.json()["newBadges"]]
    assert _rows(db_path, "SELECT current_streak FROM user_streaks")[0]["current_streak"] == 1


def test_achievements_page_shows_streak_badges_and_theme_ratings(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    conn.close()
    client = TestClient(create_app(db_path))
    client.post("/api/puzzles/aaaaa/attempt", json=SOLVE)
    r = client.get("/achievements", params={"users": "alice"})
    assert r.status_code == 200
    assert "Streak &amp; rating" in r.text
    assert "Badges" in r.text
    assert "First Steps" in r.text
    assert "Rating by theme" in r.text
    assert "Back-rank mate" in r.text


def test_achievements_page_renders_with_no_gamification_activity(tmp_path):
    r = TestClient(create_app(_seeded_db(tmp_path))).get("/achievements", params={"users": "alice"})
    assert r.status_code == 200
    assert "to get a skill rating" in r.text


def test_leaderboard_shows_only_active_players(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    other = {**REC, "url": "https://example.com/g2", "username": "bob"}
    save_game(conn, other, [{**MISTAKE, "game_url": other["url"], "username": "bob"}], depth=14)
    conn.close()
    client = TestClient(create_app(db_path))
    client.post("/api/puzzles/aaaaa/attempt", json=SOLVE)
    r = client.get("/leaderboard")
    assert r.status_code == 200
    assert "alice" in r.text
    assert ">bob<" not in r.text


def test_leaderboard_empty_state(tmp_path):
    r = TestClient(create_app(_empty_db(tmp_path))).get("/leaderboard")
    assert r.status_code == 200
    assert "Nobody has any activity yet" in r.text


def test_daily_puzzle_page_serves_the_same_puzzle_all_day(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    a = client.get("/puzzles/daily", params={"users": "alice"})
    b = client.get("/puzzles/daily", params={"users": "alice"})
    assert a.status_code == 200
    assert "Puzzle of the day" in a.text and "aaaaa" in a.text and "aaaaa" in b.text
    assert len(_rows(db_path, "SELECT * FROM daily_puzzles")) == 1


def test_daily_puzzle_page_says_when_you_solved_it(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    client.get("/puzzles/daily", params={"users": "alice"})
    client.post("/api/puzzles/aaaaa/attempt", json=SOLVE)
    assert "solved it today" in client.get("/puzzles/daily", params={"users": "alice"}).text


def test_daily_puzzle_page_with_no_puzzles_imported(tmp_path):
    r = TestClient(create_app(_empty_db(tmp_path))).get("/puzzles/daily", params={"users": "alice"})
    assert r.status_code == 200
    assert "No puzzles have been imported yet" in r.text


def test_rush_page_and_next_puzzle(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    page = client.get("/puzzles/rush", params={"users": "alice"})
    assert page.status_code == 200 and "rushStart" in page.text
    nxt = client.get("/api/puzzles/rush/next", params={"user": "alice"})
    assert nxt.status_code == 200 and nxt.json()["puzzleId"] == "aaaaa"


def test_rush_next_404s_with_no_puzzles(tmp_path):
    r = TestClient(create_app(_empty_db(tmp_path))).get(
        "/api/puzzles/rush/next", params={"user": "alice"})
    assert r.status_code == 404


def test_rush_finish_records_the_score_and_flags_personal_bests(tmp_path):
    client = TestClient(create_app(_puzzle_seeded_db(tmp_path)))
    first = client.post("/api/puzzles/rush/finish",
                        json={"practicingUser": "alice", "score": 4}).json()
    second = client.post("/api/puzzles/rush/finish",
                         json={"practicingUser": "alice", "score": 2}).json()
    assert first["personalBest"] is True and second["personalBest"] is False
    assert second["best"] == 4
    assert client.post("/api/puzzles/rush/finish", json={"score": 3}).status_code == 400


def test_puzzles_page_prefills_the_range_from_the_skill_rating(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    _set_rating(db_path, 1600)
    r = TestClient(create_app(db_path)).get("/puzzles", params={"users": "alice"})
    assert 'name="minRating" value="1450"' in r.text
    assert 'name="maxRating" value="1750"' in r.text
    assert "pre-filled from your skill rating" in r.text


def test_puzzles_page_explicit_range_beats_the_skill_rating(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    _set_rating(db_path, 1600)
    r = TestClient(create_app(db_path)).get(
        "/puzzles", params={"users": "alice", "minRating": 0, "maxRating": 9999})
    assert 'name="minRating" value="0"' in r.text
    assert "pre-filled from your skill rating" not in r.text


def _analyze(client, **extra):
    return client.post("/api/play/analyze",
                       json={"moves": ["d2d4", "e7e5"], "colour": "white", **extra})


def _patch_engine(monkeypatch):
    monkeypatch.setattr(
        "chess.engine.SimpleEngine.popen_uci",
        lambda *a, **k: _mock_analyze_engine(chess.WHITE, [("e2e4", 30), ("d7d5", -170)]))


def test_play_analyze_with_a_user_updates_the_game_rating(tmp_path, monkeypatch):
    db_path = _empty_db(tmp_path)
    _patch_engine(monkeypatch)
    client = TestClient(create_app(db_path, engine_path=_fake_engine_path(tmp_path)))
    r = _analyze(client, user="alice")
    assert r.status_code == 200 and "newBadges" in r.json()
    rows = _rows(db_path, "SELECT theme, games_seen FROM user_game_ratings WHERE username = 'alice'")
    assert "" in {row["theme"] for row in rows}
    assert all(row["games_seen"] == 1 for row in rows)
    assert _rows(db_path, "SELECT 1 FROM games") == []  # bot games are still never stored


def test_play_analyze_without_a_user_leaves_ratings_alone(tmp_path, monkeypatch):
    db_path = _empty_db(tmp_path)
    _patch_engine(monkeypatch)
    _analyze(TestClient(create_app(db_path, engine_path=_fake_engine_path(tmp_path))))
    assert _rows(db_path, "SELECT 1 FROM user_game_ratings") == []


def test_home_page_links_to_daily_puzzle_and_leaderboard(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    set_settings(conn, primary_user="alice")
    conn.close()
    r = TestClient(create_app(db_path)).get("/")
    assert "/puzzles/daily?users=alice" in r.text
    assert 'href="/leaderboard"' in r.text


def test_opening_achievements_awards_badges_earned_by_older_history(tmp_path):
    db_path = _seeded_db(tmp_path)
    conn = open_db(db_path)
    conn.execute("INSERT INTO practice_attempts (practicing_user, mistake_id, owner, category, "
                 "verdict, hint_used, created_at) VALUES ('alice', 1, 'alice', 'c', 'best', 0, 'x')")
    conn.commit()
    conn.close()
    r = TestClient(create_app(db_path)).get("/achievements", params={"users": "alice"})
    assert 'class="badge earned"><b>Getting Started' in r.text
