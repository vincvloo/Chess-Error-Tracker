"""Markup-level checks for the UI/UX pass: navigation, accessibility hooks,
badge progress, the activity strip and the copy changes. (What a page looks
like still needs eyes; these guard what's cheap to guard.)"""

from datetime import date

from fastapi.testclient import TestClient

from chess_tracker import gamification as g
from chess_tracker.db import open_db, save_game, set_settings
from chess_tracker.web.app import create_app
from test_web_routes import (MISTAKE, REC, _fake_engine_path, _practice_game, _practice_mistake,
                             _practice_seeded_db, _puzzle_seeded_db, _seeded_db)


def _with_primary(db_path, user="alice"):
    conn = open_db(db_path)
    set_settings(conn, primary_user=user)
    conn.close()
    return db_path


# ---- badge progress --------------------------------------------------------

def test_locked_badges_report_progress_toward_their_target():
    conn = open_db(":memory:")
    for i in range(37):
        conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                     "move_index_reached, created_at) VALUES ('alice', ?, 'solved', 1, 'x')",
                     (f"p{i}",))
    conn.commit()
    g.evaluate_badges(conn, "alice")
    locked = {b["code"]: b for b in g.get_badges(conn, "alice")["locked"]}
    assert locked["puzzles_100"]["current"] == 37 and locked["puzzles_100"]["target"] == 100
    assert "first_puzzle" not in locked            # earned, so no longer locked


def test_badge_progress_is_capped_at_the_target():
    conn = open_db(":memory:")
    conn.execute("INSERT INTO user_streaks (username, current_streak, best_streak, "
                 "last_active_date, updated_at) VALUES ('alice', 0, 12, '2026-01-01', 'x')")
    conn.commit()
    locked = {b["code"]: b for b in g.get_badges(conn, "alice")["locked"]}
    assert locked["streak_30"]["current"] == 12
    assert "streak_7" not in locked or locked["streak_7"]["current"] == 7


# ---- activity strip --------------------------------------------------------

def test_activity_days_marks_practice_and_puzzle_days_oldest_first():
    conn = open_db(":memory:")
    conn.execute("INSERT INTO practice_attempts (practicing_user, mistake_id, owner, category, "
                 "verdict, hint_used, created_at) VALUES ('alice', 1, 'alice', 'c', 'best', 0, "
                 "'2026-09-20T10:00:00+00:00')")
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('alice', 'p', 'failed', 1, "
                 "'2026-09-22T10:00:00+00:00')")
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('bob', 'p', 'solved', 1, "
                 "'2026-09-21T10:00:00+00:00')")
    conn.commit()
    days = g.activity_days(conn, "alice", days=5, today=date(2026, 9, 23))
    assert [d["date"] for d in days] == ["2026-09-19", "2026-09-20", "2026-09-21",
                                         "2026-09-22", "2026-09-23"]
    assert [d["active"] for d in days] == [False, True, False, True, False]


def test_achievements_page_shows_tiles_strip_and_badge_progress(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    conn.close()
    client = TestClient(create_app(db_path))
    client.post("/api/puzzles/aaaaa/attempt",
                json={"moveIndex": 1, "from": "e1", "to": "e8", "practicingUser": "alice"})
    html = client.get("/achievements", params={"users": "alice"}).text
    assert "<h2>Achievements</h2>" in html
    assert 'class="daystrip"' in html and "of the last 14 days" in html
    assert "skill rating (estimate)" in html
    assert "1 / 100" in html                        # Puzzle Century progress


# ---- navigation --------------------------------------------------------------

def test_main_nav_appears_once_there_is_a_primary_user_and_marks_the_page(tmp_path):
    db_path = _with_primary(_puzzle_seeded_db(tmp_path))
    html = TestClient(create_app(db_path)).get("/puzzles", params={"users": "alice"}).text
    assert 'aria-label="Main"' in html
    assert '<a href="/puzzles" aria-current="page">Puzzles</a>' in html
    assert 'href="/leaderboard"' in html


def test_main_nav_is_hidden_during_onboarding(tmp_path):
    html = TestClient(create_app(_seeded_db(tmp_path))).get("/").text
    assert 'aria-label="Main"' not in html


def test_home_groups_cards_and_leads_with_todays_puzzle(tmp_path):
    db_path = _with_primary(_seeded_db(tmp_path))
    html = TestClient(create_app(db_path)).get("/").text
    assert "Train" in html and "Your progress" in html
    assert html.index('href="/puzzles/daily?users=alice"') < html.index('href="/practice?users=alice"')


# ---- pages that no longer carry an irrelevant Update button --------------------

def test_update_button_is_gone_from_play_puzzles_and_the_practice_board(tmp_path):
    db_path = _with_primary(_practice_seeded_db(tmp_path))
    conn = open_db(db_path)
    conn.close()
    client = TestClient(create_app(db_path, engine_path=_fake_engine_path(tmp_path)))
    for url in ("/play", "/puzzles", "/practice?category=hung+a+pawn"):
        html = client.get(url).text
        assert 'action="/jobs"' not in html, url


def test_update_button_stays_on_home_and_achievements(tmp_path):
    db_path = _with_primary(_seeded_db(tmp_path))
    client = TestClient(create_app(db_path))
    assert 'action="/jobs"' in client.get("/").text
    assert 'action="/jobs"' in client.get("/achievements").text


# ---- practice: no leaked internal cap ---------------------------------------------

def _practice_page_with_cp(tmp_path, cp_loss):
    db_path = str(tmp_path / f"cp{cp_loss}.db")
    conn = open_db(db_path)
    save_game(conn, _practice_game(), [{**_practice_mistake("https://x/g1"), "cp_loss": cp_loss}],
              depth=14)
    conn.close()
    r = TestClient(create_app(db_path)).get(
        "/practice", params={"users": "alice", "category": "hung a pawn"})
    assert r.status_code == 200
    return r.text


def test_practice_shows_decisive_instead_of_the_5000cp_cap(tmp_path):
    html = _practice_page_with_cp(tmp_path, 5000)
    assert "decisive" in html and "-5000cp" not in html


def test_practice_still_shows_ordinary_centipawn_loss(tmp_path):
    assert "-300cp" in _practice_page_with_cp(tmp_path, 300)


# ---- board accessibility hooks ---------------------------------------------------------

def test_board_markup_and_script_carry_the_accessibility_hooks(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    html = TestClient(create_app(db_path)).get("/puzzles", params={"users": "alice"}).text
    assert 'role="group"' in html and "Arrow keys move between squares" in html
    assert 'id="ranks" aria-hidden="true"' in html
    assert 'role", "button"' in html and "aria-pressed" in html and "PIECE_NAME" in html
    assert 'id="feedback" role="status" aria-live="polite"' in html


def test_dark_mode_primary_buttons_use_dark_text_on_the_light_accent(tmp_path):
    html = TestClient(create_app(_seeded_db(tmp_path))).get("/").text
    assert "--on-accent: #0e1526" in html
    assert "color: var(--on-accent)" in html
    assert ":focus-visible" in html


def test_puzzles_filters_are_collapsed_when_a_puzzle_is_showing(tmp_path):
    db_path = _puzzle_seeded_db(tmp_path)
    client = TestClient(create_app(db_path))
    shown = client.get("/puzzles", params={"users": "alice", "minRating": 0, "maxRating": 9999}).text
    assert '<details class="filters">' in shown
    none = client.get("/puzzles", params={"users": "alice", "minRating": 3000, "maxRating": 3100}).text
    assert '<details class="filters" open>' in none
