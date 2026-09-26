from datetime import date, datetime, timezone

import chess

from chess_mistake_coach import gamification as g
from chess_mistake_coach.db import open_db, save_game


def _conn():
    return open_db(":memory:")


def _game(url, end_time, username="alice", moves=30, om=10, mm=15, em=5):
    return {
        "url": url, "username": username, "end_time": end_time, "date": "2024-01-01",
        "time_class": "blitz", "my_colour": "white", "my_rating": 1500, "opp_rating": 1400,
        "result": "win", "eco": "C00", "moves_played": moves,
        "opening_moves": om, "middlegame_moves": mm, "endgame_moves": em,
    }


def _mistake(url, username="alice", cp_loss=100, phase="middlegame"):
    return {
        "game_url": url, "username": username, "date": "2024-01-01", "end_time": 1,
        "time_class": "blitz", "my_rating": 1500, "my_colour": "white", "move_number": 5,
        "phase": phase, "severity": "mistake", "cp_loss": cp_loss, "category": "hung a pawn",
        "played": "e4", "best": "d4", "clock_seconds": 20.0, "fen": "fen",
    }


# ---- streaks -------------------------------------------------------------

def test_streak_starts_at_one_and_is_idempotent_within_a_day():
    conn = _conn()
    day = date(2026, 9, 21)
    assert g.record_activity(conn, "alice", day)["current"] == 1
    assert g.record_activity(conn, "alice", day)["current"] == 1


def test_streak_grows_on_consecutive_days_and_tracks_best():
    conn = _conn()
    for d in (21, 22, 23):
        s = g.record_activity(conn, "alice", date(2026, 9, d))
    assert s["current"] == 3 and s["best"] == 3


def test_one_missed_day_is_bridged_by_the_weekly_freeze():
    conn = _conn()
    g.record_activity(conn, "alice", date(2026, 9, 21))  # Monday
    g.record_activity(conn, "alice", date(2026, 9, 22))
    s = g.record_activity(conn, "alice", date(2026, 9, 24))  # skipped the 23rd
    assert s["current"] == 3
    assert s["freezes"] == 0


def test_second_missed_day_in_the_same_week_breaks_the_streak():
    conn = _conn()
    g.record_activity(conn, "alice", date(2026, 9, 21))
    g.record_activity(conn, "alice", date(2026, 9, 23))   # uses the freeze
    s = g.record_activity(conn, "alice", date(2026, 9, 25))  # no freeze left this week
    assert s["current"] == 1
    assert s["best"] == 2


def test_freeze_refills_in_a_new_week():
    conn = _conn()
    g.record_activity(conn, "alice", date(2026, 9, 21))
    g.record_activity(conn, "alice", date(2026, 9, 23))   # freeze used (week 39)
    g.record_activity(conn, "alice", date(2026, 9, 24))
    g.record_activity(conn, "alice", date(2026, 9, 25))
    g.record_activity(conn, "alice", date(2026, 9, 26))
    g.record_activity(conn, "alice", date(2026, 9, 27))
    s = g.record_activity(conn, "alice", date(2026, 9, 29))  # week 40: fresh freeze bridges the 28th
    assert s["current"] == 7


def test_gap_of_three_days_resets_and_reads_as_zero():
    conn = _conn()
    g.record_activity(conn, "alice", date(2026, 9, 21))
    assert g.get_streak(conn, "alice", date(2026, 9, 24))["current"] == 0
    assert g.record_activity(conn, "alice", date(2026, 9, 24))["current"] == 1


def test_stale_streak_reads_as_zero_but_a_bridgeable_one_does_not():
    conn = _conn()
    g.record_activity(conn, "alice", date(2026, 9, 21))
    assert g.get_streak(conn, "alice", date(2026, 9, 23))["current"] == 1  # freeze can bridge
    assert g.get_streak(conn, "alice", date(2026, 9, 25))["current"] == 0


# ---- puzzle rating -------------------------------------------------------

def _rating(conn, theme="", table="user_puzzle_ratings", user="alice"):
    row = conn.execute(f"SELECT rating FROM {table} WHERE username = ? AND theme = ?",
                       (user, theme)).fetchone()
    return None if row is None else row["rating"]


def test_solving_a_puzzle_raises_and_failing_lowers_the_rating():
    conn = _conn()
    g.update_puzzle_rating(conn, "alice", 1200, True)
    assert _rating(conn) > g.BASE_RATING
    g.update_puzzle_rating(conn, "bob", 1200, False)
    assert _rating(conn, user="bob") < g.BASE_RATING


def test_puzzle_updates_overall_and_every_theme_it_carries():
    conn = _conn()
    g.update_puzzle_rating(conn, "alice", 1500, True, " fork pin ")
    assert _rating(conn) > g.BASE_RATING
    assert _rating(conn, "fork") > g.BASE_RATING
    assert _rating(conn, "pin") > g.BASE_RATING
    assert _rating(conn, "skewer") is None


def test_beating_a_harder_puzzle_moves_the_rating_more():
    conn = _conn()
    g.update_puzzle_rating(conn, "easy", 1000, True)
    g.update_puzzle_rating(conn, "hard", 1800, True)
    assert _rating(conn, user="hard") > _rating(conn, user="easy")


# ---- game rating ---------------------------------------------------------

def test_performance_rating_is_bounded_and_monotonic():
    assert g.performance_rating(-1000) == g.GAME_PERF_MAX
    assert g.performance_rating(0) < g.GAME_PERF_MAX
    assert g.performance_rating(10_000) == g.GAME_PERF_MIN
    assert g.performance_rating(20) > g.performance_rating(80)


def test_clean_games_raise_the_rating_and_sloppy_games_lower_it():
    clean, sloppy = _conn(), _conn()
    for i in range(20):
        save_game(clean, _game(f"c{i}", i), [], depth=14)
        save_game(sloppy, _game(f"s{i}", i), [_mistake(f"s{i}", cp_loss=500) for _ in range(8)],
                  depth=14)
    g.recompute_game_ratings(clean, "alice")
    g.recompute_game_ratings(sloppy, "alice")
    assert _rating(clean, table="user_game_ratings") > g.BASE_RATING
    assert _rating(sloppy, table="user_game_ratings") < g.BASE_RATING


def test_game_rating_stays_bounded_over_many_games():
    conn = _conn()
    for i in range(300):
        save_game(conn, _game(f"g{i}", i), [], depth=14)
    g.recompute_game_ratings(conn, "alice")
    assert _rating(conn, table="user_game_ratings") <= g.GAME_PERF_MAX


def test_recompute_depends_on_end_time_not_insertion_order():
    forward, backward = _conn(), _conn()
    games = [(f"g{i}", i, [_mistake(f"g{i}", cp_loss=400) for _ in range(i % 5)])
             for i in range(12)]
    for url, t, ms in games:
        save_game(forward, _game(url, t), ms, depth=14)
    for url, t, ms in reversed(games):
        save_game(backward, _game(url, t), ms, depth=14)
    g.recompute_game_ratings(forward, "alice")
    g.recompute_game_ratings(backward, "alice")
    assert _rating(forward, table="user_game_ratings") == _rating(backward, table="user_game_ratings")


def test_recompute_is_idempotent():
    conn = _conn()
    for i in range(5):
        save_game(conn, _game(f"g{i}", i), [_mistake(f"g{i}")], depth=14)
    g.recompute_game_ratings(conn, "alice")
    first = _rating(conn, table="user_game_ratings")
    n_first = conn.execute("SELECT games_seen FROM user_game_ratings WHERE theme = ''").fetchone()[0]
    g.recompute_game_ratings(conn, "alice")
    assert _rating(conn, table="user_game_ratings") == first
    assert conn.execute("SELECT games_seen FROM user_game_ratings WHERE theme = ''").fetchone()[0] == n_first == 5


def test_reanalysis_with_different_mistakes_replaces_rather_than_double_counts():
    conn = _conn()
    save_game(conn, _game("g1", 1), [_mistake("g1", cp_loss=500)] * 6, depth=14)
    g.recompute_game_ratings(conn, "alice")
    save_game(conn, _game("g1", 1), [], depth=20)  # reanalysed clean
    g.recompute_game_ratings(conn, "alice")
    assert conn.execute("SELECT games_seen FROM user_game_ratings WHERE theme = ''").fetchone()[0] == 1
    assert _rating(conn, table="user_game_ratings") > g.BASE_RATING


def test_phase_ratings_only_exist_for_phases_with_enough_moves():
    conn = _conn()
    save_game(conn, _game("g1", 1, om=1, mm=25, em=0), [], depth=14)
    g.recompute_game_ratings(conn, "alice")
    themes = {r["theme"] for r in conn.execute("SELECT theme FROM user_game_ratings")}
    assert themes == {"", "middlegame"}


def test_bot_game_is_applied_incrementally_on_top_of_existing_ratings():
    conn = _conn()
    save_game(conn, _game("g1", 1), [], depth=14)
    g.recompute_game_ratings(conn, "alice")
    before = _rating(conn, table="user_game_ratings")
    g.apply_bot_game(conn, "alice", [], {"opening": 8, "middlegame": 12, "endgame": 0})
    assert _rating(conn, table="user_game_ratings") > before
    assert conn.execute("SELECT games_seen FROM user_game_ratings WHERE theme = ''").fetchone()[0] == 2


def test_phase_move_counts_for_a_bot_game():
    moves = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "g1f3", "b8c6")]
    counts = g.phase_move_counts(moves, chess.WHITE)
    assert sum(counts.values()) == 2


# ---- blended rating ------------------------------------------------------

def test_skill_rating_is_none_with_no_data():
    assert g.get_skill_rating(_conn(), "alice") == {"overall": None, "themes": []}


def test_skill_rating_blends_both_halves_and_labels_themes():
    conn = _conn()
    g.update_puzzle_rating(conn, "alice", 1800, True, " fork ")
    save_game(conn, _game("g1", 1), [], depth=14)
    g.recompute_game_ratings(conn, "alice")
    rating = g.get_skill_rating(conn, "alice")
    assert rating["overall"]["samples"] == 2
    themes = {t["theme"]: t for t in rating["themes"]}
    assert themes["fork"]["label"] == "Fork"
    assert "middlegame" in themes


def test_smart_rating_window_is_centred_on_the_overall_rating():
    conn = _conn()
    assert g.smart_rating_window(conn, "alice") is None
    g.update_puzzle_rating(conn, "alice", 1200, True)
    lo, hi = g.smart_rating_window(conn, "alice")
    assert hi - lo == 2 * g.SMART_RANGE


# ---- badges --------------------------------------------------------------

def _solve(conn, user, n):
    for i in range(n):
        conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                     "move_index_reached, created_at) VALUES (?, ?, 'solved', 1, '2026-01-01T00:00:00')",
                     (user, f"p{i}"))
    conn.commit()


def test_badge_is_awarded_once_when_its_threshold_is_crossed():
    conn = _conn()
    assert g.evaluate_badges(conn, "alice") == []
    _solve(conn, "alice", 1)
    assert g.evaluate_badges(conn, "alice") == ["first_puzzle"]
    assert g.evaluate_badges(conn, "alice") == []  # idempotent


def test_volume_badges_need_distinct_puzzles():
    conn = _conn()
    _solve(conn, "alice", 100)
    assert "puzzles_100" in g.evaluate_badges(conn, "alice")


def test_streak_badge_uses_the_best_streak():
    conn = _conn()
    conn.execute("INSERT INTO user_streaks (username, current_streak, best_streak, "
                 "last_active_date, updated_at) VALUES ('alice', 0, 7, '2026-01-01', 'x')")
    conn.commit()
    assert "streak_7" in g.evaluate_badges(conn, "alice")


def test_earned_badges_survive_deleting_practice_history():
    conn = _conn()
    conn.execute("INSERT INTO practice_attempts (practicing_user, mistake_id, owner, category, "
                 "verdict, hint_used, created_at) VALUES ('alice', 1, 'alice', 'c', 'best', 0, 'x')")
    conn.commit()
    assert "first_practice" in g.evaluate_badges(conn, "alice")
    conn.execute("DELETE FROM practice_attempts")
    conn.commit()
    g.evaluate_badges(conn, "alice")
    assert any(b["code"] == "first_practice" for b in g.get_badges(conn, "alice")["earned"])


def test_get_badges_splits_earned_and_locked():
    conn = _conn()
    _solve(conn, "alice", 1)
    g.evaluate_badges(conn, "alice")
    badges = g.get_badges(conn, "alice")
    assert [b["code"] for b in badges["earned"]] == ["first_puzzle"]
    assert len(badges["earned"]) + len(badges["locked"]) == len(g.BADGE_DEFINITIONS)


def test_record_progress_returns_newly_earned_badges_with_labels():
    conn = _conn()
    _solve(conn, "alice", 1)
    new = g.record_progress(conn, "alice")
    assert new[0]["label"] == "First Steps"
    assert g.get_streak(conn, "alice")["current"] == 1


# ---- daily puzzle, rush, leaderboard --------------------------------------

def _add_puzzle(conn, pid="aaaaa", rating=900):
    conn.execute("INSERT INTO puzzles (puzzle_id, fen, moves, rating, themes) VALUES (?, ?, ?, ?, ?)",
                 (pid, "6k1/p4ppp/8/8/8/8/5PPP/4R1K1 b - - 0 1", "a7a6 e1e8", rating, " mateIn1 "))
    conn.commit()


def test_daily_puzzle_is_none_with_no_puzzles():
    assert g.get_or_assign_daily_puzzle(_conn()) is None


def test_daily_puzzle_is_stable_within_a_day_and_can_change_next_day():
    conn = _conn()
    for i in range(30):
        _add_puzzle(conn, f"p{i:02d}")
    first = g.get_or_assign_daily_puzzle(conn, date(2026, 9, 21))["puzzle_id"]
    for _ in range(5):
        assert g.get_or_assign_daily_puzzle(conn, date(2026, 9, 21))["puzzle_id"] == first
    assert conn.execute("SELECT COUNT(*) FROM daily_puzzles").fetchone()[0] == 1


def test_daily_solved_reflects_todays_solve_only():
    conn = _conn()
    _add_puzzle(conn)
    day = date(2026, 9, 21)
    g.get_or_assign_daily_puzzle(conn, day)
    assert not g.daily_solved(conn, "alice", day)
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('alice', 'aaaaa', 'solved', 1, "
                 "'2026-09-21T12:00:00+00:00')")
    conn.commit()
    assert g.daily_solved(conn, "alice", day)
    assert not g.daily_solved(conn, "bob", day)


def test_rush_scores_report_personal_bests():
    conn = _conn()
    assert g.best_rush_score(conn, "alice") is None
    assert g.record_rush_score(conn, "alice", 5)["personalBest"] is True
    assert g.record_rush_score(conn, "alice", 3)["personalBest"] is False
    assert g.record_rush_score(conn, "alice", 8)["best"] == 8
    assert g.best_rush_score(conn, "alice") == 8


def test_leaderboard_lists_only_active_players_sorted_by_rating():
    conn = _conn()
    for user in ("alice", "bob", "carol"):
        save_game(conn, _game(f"{user}1", 1, username=user), [], depth=14)
    g.update_puzzle_rating(conn, "alice", 1200, True)
    g.update_puzzle_rating(conn, "bob", 2000, True)
    rows = g.leaderboard(conn)
    assert [r["username"] for r in rows] == ["bob", "alice"]


def test_leaderboard_counts_attempts_made_before_gamification_existed():
    conn = _conn()
    save_game(conn, _game("g1", 1), [], depth=14)
    assert g.leaderboard(conn) == []
    conn.execute("INSERT INTO practice_attempts (practicing_user, mistake_id, owner, category, "
                 "verdict, hint_used, created_at) VALUES ('alice', 1, 'alice', 'c', 'best', 0, 'x')")
    conn.commit()
    assert [r["username"] for r in g.leaderboard(conn)] == ["alice"]


# ---- local time ---------------------------------------------------------------

import os
import time

import pytest


@pytest.fixture
def timezone_of():
    """Switch the process timezone for a test (POSIX only), then restore it."""
    old = os.environ.get("TZ")

    def use(tz):
        os.environ["TZ"] = tz
        time.tzset()

    yield use
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


needs_tzset = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (POSIX)")


@needs_tzset
def test_today_follows_the_local_timezone_not_utc(timezone_of):
    timezone_of("Pacific/Kiritimati")   # UTC+14: already tomorrow when UTC says today
    utc_today = datetime.now(timezone.utc).date()
    assert g._today() >= utc_today
    timezone_of("Pacific/Pago_Pago")    # UTC-11: still yesterday for hours after UTC midnight
    assert g._today() <= utc_today


@needs_tzset
def test_evening_practice_counts_for_the_local_day(timezone_of):
    # 02:30 UTC on the 22nd is still the evening of the 21st in Los Angeles.
    timezone_of("America/Los_Angeles")
    conn = _conn()
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('alice', 'p', 'solved', 1, "
                 "'2026-09-22T02:30:00+00:00')")
    conn.commit()
    days = g.activity_days(conn, "alice", days=3, today=date(2026, 9, 22))
    assert {d["date"]: d["active"] for d in days} == {
        "2026-09-20": False, "2026-09-21": True, "2026-09-22": False}


@needs_tzset
def test_daily_solved_uses_the_local_day(timezone_of):
    timezone_of("America/Los_Angeles")
    conn = _conn()
    _add_puzzle(conn)
    day = date(2026, 9, 21)
    g.get_or_assign_daily_puzzle(conn, day)
    conn.execute("INSERT INTO puzzle_attempts (practicing_user, puzzle_id, verdict, "
                 "move_index_reached, created_at) VALUES ('alice', 'aaaaa', 'solved', 1, "
                 "'2026-09-22T02:30:00+00:00')")
    conn.commit()
    assert g.daily_solved(conn, "alice", day)
