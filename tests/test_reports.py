import sqlite3

from chess_tracker.db import open_db, save_game
from chess_tracker.reports import (
    CLOCK_BUCKET_SQL_CASE,
    MOVE_BUCKET_SQL_CASE,
    PRACTICE_QUEUE_LIMIT,
    _scope,
    clock_bucket,
    move_bucket,
    practice_pool,
    practice_queue,
    practice_stats,
    report,
    report_model,
)


def test_scope_user_only():
    games_where, games_params, mistakes_where, mistakes_params = _scope("Alice")
    assert games_where == "WHERE username = ?"
    assert games_params == ["alice"]
    assert mistakes_where == games_where
    assert mistakes_params == ["alice"]


def test_scope_adds_time_class_to_both_clauses():
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        "alice", time_class="blitz")
    assert games_where == "WHERE username = ? AND time_class = ?"
    assert games_params == ["alice", "blitz"]
    assert mistakes_where == games_where
    assert mistakes_params == ["alice", "blitz"]


def test_scope_phase_only_affects_mistakes_clause():
    # `games` has no phase column, so the phase filter must not leak into it.
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        "alice", phase="endgame")
    assert games_where == "WHERE username = ?"
    assert games_params == ["alice"]
    assert mistakes_where == "WHERE username = ? AND phase = ?"
    assert mistakes_params == ["alice", "endgame"]


def test_scope_last_days_only_affects_games_clause_base():
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        "alice", last_days=30)
    assert games_where == "WHERE username = ? AND end_time >= ?"
    assert len(games_params) == 2
    assert games_params[0] == "alice"
    assert mistakes_where == games_where
    assert mistakes_params == games_params


def test_scope_combines_all_filters_in_order():
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        "alice", time_class="blitz", phase="opening", last_days=7)
    assert games_where == "WHERE username = ? AND time_class = ? AND end_time >= ?"
    assert games_params[:2] == ["alice", "blitz"]
    assert mistakes_where == games_where + " AND phase = ?"
    assert mistakes_params[-1] == "opening"


# ---- move_bucket / clock_bucket, pinned against their SQL CASE twins ------

def test_move_bucket_matches_its_sql_case_for_every_move_1_to_120():
    conn = sqlite3.connect(":memory:")
    for mv in range(1, 121):
        sql_label = conn.execute(
            f"SELECT {MOVE_BUCKET_SQL_CASE} FROM (SELECT ? AS move_number)",
            (mv,)).fetchone()[0]
        assert move_bucket(mv) == sql_label, mv


def test_move_bucket_labels_at_known_points():
    assert move_bucket(1) == "1-10"
    assert move_bucket(10) == "1-10"
    assert move_bucket(11) == "11-20"
    assert move_bucket(40) == "31-40"
    assert move_bucket(41) == "41+"
    assert move_bucket(200) == "41+"


def test_clock_bucket_matches_its_sql_case_around_every_boundary():
    conn = sqlite3.connect(":memory:")
    for cs in (0.0, 1.0, 29.9, 30.0, 30.1, 59.9, 60.0, 60.1, 61.0, 500.0):
        sql_label = conn.execute(
            f"SELECT {CLOCK_BUCKET_SQL_CASE} FROM (SELECT ? AS clock_seconds)",
            (cs,)).fetchone()[0]
        assert clock_bucket(cs) == sql_label, cs


def test_clock_bucket_labels_at_known_points():
    assert clock_bucket(0) == "under 30s left"
    assert clock_bucket(29.9) == "under 30s left"
    assert clock_bucket(30.0) == "30 to 60s left"
    assert clock_bucket(59.9) == "30 to 60s left"
    assert clock_bucket(60.0) == "over 60s left"
    assert clock_bucket(1000) == "over 60s left"


# ---- report_model() helpers -----------------------------------------------

def _game(url, username="alice", end_time=1000, date="2024-01-01", time_class="blitz",
         colour="white", rating=1500, eco="C00", moves=20, om=10, mm=6, em=4):
    return {
        "url": url, "username": username, "end_time": end_time, "date": date,
        "time_class": time_class, "my_colour": colour, "my_rating": rating,
        "opp_rating": 1400, "result": "win", "eco": eco,
        "moves_played": moves, "opening_moves": om, "middlegame_moves": mm,
        "endgame_moves": em,
    }


def _mistake(url, username="alice", date="2024-01-01", end_time=1000, time_class="blitz",
            colour="white", rating=1500, move_number=5, phase="opening",
            severity="blunder", cp_loss=300, category="hung a piece",
            played="e4", best="d4", clock_seconds=20.0):
    return {
        "game_url": url, "username": username, "date": date, "end_time": end_time,
        "time_class": time_class, "my_rating": rating, "my_colour": colour,
        "move_number": move_number, "phase": phase, "severity": severity,
        "cp_loss": cp_loss, "category": category, "played": played, "best": best,
        "clock_seconds": clock_seconds, "fen": "fen",
    }


# ---- positions to review: recency tiebreak + occurrence count -------------

def test_positions_break_cp_loss_ties_by_recency_then_a_stable_id():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", end_time=1000, date="2024-01-01",
                 cp_loss=2000, move_number=5, category="a"),
        _mistake("https://x/g1", end_time=2000, date="2024-01-02",
                 cp_loss=2000, move_number=6, category="a"),
        _mistake("https://x/g1", end_time=1000, date="2024-01-01",
                 cp_loss=2000, move_number=7, category="a"),
    ], depth=14)

    model = report_model(conn, "alice")
    move_numbers = [p["move_number"] for p in model["positions"]]
    # move 6 has the latest end_time, so it wins the cp_loss tie outright.
    assert move_numbers[0] == 6
    # moves 5 and 7 tie on cp_loss and end_time; the later-inserted row
    # (higher id -- move 7) breaks the tie.
    assert move_numbers[1:] == [7, 5]


def test_positions_carry_how_often_their_category_occurs_in_selection():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", move_number=5, cp_loss=2000, category="hung a piece"),
        _mistake("https://x/g1", move_number=6, cp_loss=900, category="hung a piece"),
        _mistake("https://x/g1", move_number=7, cp_loss=150, category="missed tactic"),
    ], depth=14)

    model = report_model(conn, "alice")
    occurrences_by_move = {p["move_number"]: p["occurrences"] for p in model["positions"]}
    assert occurrences_by_move[5] == 2
    assert occurrences_by_move[6] == 2
    assert occurrences_by_move[7] == 1


# ---- first-half/second-half deltas: deterministic tie-break ---------------

def test_half_vs_half_deltas_break_abs_ties_by_category_name():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1", date="2024-01-01", moves=100, om=100, mm=0, em=0), [
        _mistake("https://x/g1", date="2024-01-01", category="beta", move_number=1),
        _mistake("https://x/g1", date="2024-01-01", category="beta", move_number=2),
    ], depth=14)
    save_game(conn, _game("https://x/g2", date="2024-02-01", moves=100, om=100, mm=0, em=0),
             [], depth=14)
    save_game(conn, _game("https://x/g3", date="2024-03-01", moves=100, om=100, mm=0, em=0),
             [], depth=14)
    save_game(conn, _game("https://x/g4", date="2024-04-01", moves=100, om=100, mm=0, em=0), [
        _mistake("https://x/g4", date="2024-04-01", category="alpha", move_number=1),
        _mistake("https://x/g4", date="2024-04-01", category="alpha", move_number=2),
    ], depth=14)

    model = report_model(conn, "alice")
    # "alpha" (early->late, +1.0) and "beta" (early->late, -1.0) tie at
    # abs(delta) == 1.0. Without a tie-break their order was whatever a
    # Python set happened to iterate; now it's alphabetical.
    categories_in_order = [c for _, _, _, c in model["trend"]["halves"]]
    assert categories_in_order == ["alpha", "beta"]


# ---- ECO NULL bug: a NULL eco must not become a phantom opening -----------

def test_eco_null_games_do_not_become_a_phantom_opening():
    conn = open_db(":memory:")
    for i in range(3):
        save_game(conn, _game(f"https://x/g{i}", date=f"2024-01-0{i + 1}", eco=None),
                 [], depth=14)
    for i in range(3):
        save_game(conn, _game(f"https://x/h{i}", date=f"2024-02-0{i + 1}", eco="C00"),
                 [], depth=14)

    model = report_model(conn, "alice")
    eco_codes = [k for k, _n, _e in model["openings"]]
    assert None not in eco_codes
    assert "C00" in eco_codes


# ---- golden test: the refactor must not otherwise change the report -------

_GOLDEN_REPORT = """\
================================================================
CHESS ERROR PROFILE  |  alice
================================================================
Games in store     : 6
Your moves         : 208
Date range         : 2024-01-01 to 2024-04-02
Rating             : 1500 then, 1545 now (+45)
Mistakes + blunders: 10  (4.8% of your moves)

----------------------------------------------------------------
RECURRING ERROR TYPES
----------------------------------------------------------------
    4   40.0%  ###########.................  missed tactic
    3   30.0%  ########....................  hung a piece
    3   30.0%  ########....................  endgame technique

----------------------------------------------------------------
WHEN THEY HAPPEN
----------------------------------------------------------------
    3   30.0%  ########....................  opening
    4   40.0%  ###########.................  middlegame
    3   30.0%  ########....................  endgame

By move number:
    3   30.0%  ########....................  moves 1-10
    2   20.0%  ######......................  moves 11-20
    1   10.0%  ###.........................  moves 21-30
    1   10.0%  ###.........................  moves 31-40
    3   30.0%  ########....................  moves 41+

----------------------------------------------------------------
TIME PRESSURE
----------------------------------------------------------------
    4   50.0%  ##############..............  under 30s left
    2   25.0%  #######.....................  30 to 60s left
    2   25.0%  #######.....................  over 60s left

>> Most of your damage happens on a low clock. That is a time
   management problem, not a chess knowledge problem.

----------------------------------------------------------------
TREND  (serious errors per 100 of your moves)
----------------------------------------------------------------
  2024-01    4.3  ################  (70 moves)
  2024-02    8.0  ##############################  (25 moves)
  2024-03    4.0  ###############  (50 moves)
  2024-04    4.8  ##################  (63 moves)

  2024-01 to 2024-04: 4.3 -> 4.8 (getting worse)

Per 100 moves, first half of the period vs second half:
   2.11 ->  0.88  better  hung a piece
   1.05 ->  1.77  worse   endgame technique
   2.11 ->  1.77  better  missed tactic

----------------------------------------------------------------
BY COLOUR AND TIME CONTROL
----------------------------------------------------------------
  black       4.63 errors per 100 moves  (108 moves)
  white       5.00 errors per 100 moves  (100 moves)
  blitz       5.69 errors per 100 moves  (123 moves)
  rapid       3.53 errors per 100 moves  (85 moves)

----------------------------------------------------------------
OPENINGS YOU PLAY OFTEN
----------------------------------------------------------------
  C00     3 games   1.7 serious errors per game

----------------------------------------------------------------
TOP 10 POSITIONS TO REVIEW
----------------------------------------------------------------
  -2000cp  2024-04-01  move 10  played Bd3     best Qh5     clock     ?  (3 times)
            hung a piece
            https://x/g5
  -2000cp  2024-03-01  move 48  played Rd8     best Rd1     clock  200s  (3 times)
            endgame technique
            https://x/g4
  -2000cp  2024-02-01  move 44  played Kf1     best Kf2     clock   90s  (3 times)
            endgame technique
            https://x/g3
  -2000cp  2024-01-06  move 8   played Bxf7    best O-O     clock     ?  (3 times)
            hung a piece
            https://x/g2
  - 900cp  2024-01-01  move 5   played Nf3     best Qxb7    clock   45s  (3 times)
            hung a piece
            https://x/g1
  - 150cp  2024-04-02  move 18  played b4      best g4      clock    8s  (4 times)
            missed tactic
            https://x/g6
  - 150cp  2024-04-02  move 15  played a4      best h4      clock   55s  (4 times)
            missed tactic
            https://x/g6
  - 150cp  2024-03-01  move 50  played Kg7     best Kf7     clock   25s  (3 times)
            endgame technique
            https://x/g4
  - 150cp  2024-02-01  move 33  played Qc2     best Qb3     clock   15s  (4 times)
            missed tactic
            https://x/g3
  - 150cp  2024-01-01  move 22  played Rd1     best Nxe5    clock   20s  (4 times)
            missed tactic
            https://x/g1

================================================================"""


def test_report_golden():
    """
    A rich, multi-month, multi-category seed exercising every section.
    Diffed by hand against the pre-refactor report() on the same data: the
    only differences are the two documented, deliberate changes -- the
    positions section's ordering/occurrence counts, and (elsewhere, not
    triggered by this particular seed) the deterministic half-vs-half
    ordering. Everything else in this string is byte-for-byte what the
    original inline report() produced.
    """
    conn = open_db(":memory:")
    games = [
        _game("https://x/g1", end_time=1704067200, date="2024-01-01",
             time_class="blitz", colour="white", rating=1500, eco="C00",
             moves=40, om=15, mm=15, em=10),
        _game("https://x/g2", end_time=1704499200, date="2024-01-06",
             time_class="blitz", colour="black", rating=1510, eco="C00",
             moves=30, om=10, mm=12, em=8),
        _game("https://x/g3", end_time=1706745600, date="2024-02-01",
             time_class="blitz", colour="white", rating=1520, eco="B01",
             moves=25, om=10, mm=10, em=5),
        _game("https://x/g4", end_time=1709251200, date="2024-03-01",
             time_class="rapid", colour="black", rating=1530, eco="B01",
             moves=50, om=20, mm=20, em=10),
        _game("https://x/g5", end_time=1711929600, date="2024-04-01",
             time_class="rapid", colour="white", rating=1540, eco=None,
             moves=35, om=15, mm=12, em=8),
        _game("https://x/g6", end_time=1712016000, date="2024-04-02",
             time_class="blitz", colour="black", rating=1545, eco="C00",
             moves=28, om=10, mm=10, em=8),
    ]
    mistakes = [
        _mistake("https://x/g1", date="2024-01-01", end_time=1704067200,
                 time_class="blitz", colour="white", rating=1500, move_number=5,
                 phase="opening", severity="blunder", cp_loss=900,
                 category="hung a piece", played="Nf3", best="Qxb7",
                 clock_seconds=45.0),
        _mistake("https://x/g1", date="2024-01-01", end_time=1704067200,
                 time_class="blitz", colour="white", rating=1500, move_number=22,
                 phase="middlegame", severity="mistake", cp_loss=150,
                 category="missed tactic", played="Rd1", best="Nxe5",
                 clock_seconds=20.0),
        _mistake("https://x/g2", date="2024-01-06", end_time=1704499200,
                 time_class="blitz", colour="black", rating=1510, move_number=8,
                 phase="opening", severity="blunder", cp_loss=2000,
                 category="hung a piece", played="Bxf7", best="O-O",
                 clock_seconds=None),
        _mistake("https://x/g3", date="2024-02-01", end_time=1706745600,
                 time_class="blitz", colour="white", rating=1520, move_number=33,
                 phase="middlegame", severity="mistake", cp_loss=150,
                 category="missed tactic", played="Qc2", best="Qb3",
                 clock_seconds=15.0),
        _mistake("https://x/g3", date="2024-02-01", end_time=1706745600,
                 time_class="blitz", colour="white", rating=1520, move_number=44,
                 phase="endgame", severity="blunder", cp_loss=2000,
                 category="endgame technique", played="Kf1", best="Kf2",
                 clock_seconds=90.0),
        _mistake("https://x/g4", date="2024-03-01", end_time=1709251200,
                 time_class="rapid", colour="black", rating=1530, move_number=48,
                 phase="endgame", severity="blunder", cp_loss=2000,
                 category="endgame technique", played="Rd8", best="Rd1",
                 clock_seconds=200.0),
        _mistake("https://x/g4", date="2024-03-01", end_time=1709251200,
                 time_class="rapid", colour="black", rating=1530, move_number=50,
                 phase="endgame", severity="mistake", cp_loss=150,
                 category="endgame technique", played="Kg7", best="Kf7",
                 clock_seconds=25.0),
        _mistake("https://x/g5", date="2024-04-01", end_time=1711929600,
                 time_class="rapid", colour="white", rating=1540, move_number=10,
                 phase="opening", severity="blunder", cp_loss=2000,
                 category="hung a piece", played="Bd3", best="Qh5",
                 clock_seconds=None),
        _mistake("https://x/g6", date="2024-04-02", end_time=1712016000,
                 time_class="blitz", colour="black", rating=1545, move_number=15,
                 phase="middlegame", severity="mistake", cp_loss=150,
                 category="missed tactic", played="a4", best="h4",
                 clock_seconds=55.0),
        _mistake("https://x/g6", date="2024-04-02", end_time=1712016000,
                 time_class="blitz", colour="black", rating=1545, move_number=18,
                 phase="middlegame", severity="mistake", cp_loss=150,
                 category="missed tactic", played="b4", best="g4",
                 clock_seconds=8.0),
    ]
    for g in games:
        game_mistakes = [m for m in mistakes if m["game_url"] == g["url"]]
        save_game(conn, g, game_mistakes, depth=14)

    assert report(conn, "alice") == _GOLDEN_REPORT


# ---- practice_queue() -------------------------------------------------

def test_practice_queue_orders_worst_first_then_recency_then_id():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=150, move_number=1, category="a"),
        _mistake("https://x/g1", cp_loss=2000, end_time=2000, date="2024-01-02",
                 move_number=2, category="a"),
        _mistake("https://x/g1", cp_loss=2000, end_time=1000, date="2024-01-01",
                 move_number=3, category="a"),
        _mistake("https://x/g1", cp_loss=2000, end_time=1000, date="2024-01-01",
                 move_number=4, category="a"),
    ], depth=14)

    queue = practice_queue(conn, "alice")
    move_numbers = [m["move_number"] for m in queue]
    # move 2: highest cp_loss and latest end_time, wins outright.
    # moves 3 and 4 tie on cp_loss and end_time; higher id (move 4, inserted
    # later) wins the tiebreak. Move 1's lower cp_loss puts it last.
    assert move_numbers == [2, 4, 3, 1]


def test_practice_queue_excludes_inaccuracy_and_respects_scope():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1", time_class="blitz"), [
        _mistake("https://x/g1", time_class="blitz", phase="opening",
                 severity="inaccuracy", cp_loss=2000, move_number=1),
        _mistake("https://x/g1", time_class="blitz", phase="opening",
                 severity="blunder", cp_loss=900, move_number=2),
        _mistake("https://x/g1", time_class="blitz", phase="endgame",
                 severity="blunder", cp_loss=2000, move_number=3),
    ], depth=14)
    save_game(conn, _game("https://x/g2", time_class="rapid"), [
        _mistake("https://x/g2", time_class="rapid", phase="opening",
                 severity="blunder", cp_loss=2000, move_number=4),
    ], depth=14)

    all_blitz = practice_queue(conn, "alice", time_class="blitz")
    assert [m["move_number"] for m in all_blitz] == [3, 2]  # inaccuracy excluded

    blitz_opening = practice_queue(conn, "alice", time_class="blitz", phase="opening")
    assert [m["move_number"] for m in blitz_opening] == [2]

    rapid = practice_queue(conn, "alice", time_class="rapid")
    assert [m["move_number"] for m in rapid] == [4]


def test_practice_queue_respects_the_limit():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1", moves=1000, om=1000, mm=0, em=0), [
        _mistake("https://x/g1", cp_loss=2000 - i, move_number=i + 1, category="a")
        for i in range(PRACTICE_QUEUE_LIMIT + 20)
    ], depth=14)

    queue = practice_queue(conn, "alice")
    assert len(queue) == PRACTICE_QUEUE_LIMIT
    # still the globally worst ones, not an arbitrary truncation
    assert [m["move_number"] for m in queue] == list(range(1, PRACTICE_QUEUE_LIMIT + 1))


def test_practice_queue_empty_for_a_user_with_no_serious_mistakes():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", severity="inaccuracy", cp_loss=60, move_number=1),
    ], depth=14)
    assert practice_queue(conn, "alice") == []


def test_practice_queue_filters_by_category():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=2000, move_number=1, category="hung a piece"),
        _mistake("https://x/g1", cp_loss=1900, move_number=2, category="missed tactic"),
    ], depth=14)

    hung = practice_queue(conn, "alice", category="hung a piece")
    assert [m["move_number"] for m in hung] == [1]

    tactic = practice_queue(conn, "alice", category="missed tactic")
    assert [m["move_number"] for m in tactic] == [2]


def test_practice_pool_pools_other_users_excluding_the_given_one():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/a1", username="alice"), [
        _mistake("https://x/a1", username="alice", cp_loss=2000, move_number=1,
                 category="hung a piece"),
    ], depth=14)
    save_game(conn, _game("https://x/b1", username="bob"), [
        _mistake("https://x/b1", username="bob", cp_loss=1900, move_number=2,
                 category="hung a piece"),
        _mistake("https://x/b1", username="bob", cp_loss=1800, move_number=3,
                 category="missed tactic"),
    ], depth=14)

    pool = practice_pool(conn, "hung a piece", exclude_user="alice")
    assert [m["username"] for m in pool] == ["bob"]
    assert [m["move_number"] for m in pool] == [2]

    pool_including_alice = practice_pool(conn, "hung a piece")
    assert sorted(m["username"] for m in pool_including_alice) == ["alice", "bob"]


def test_practice_pool_respects_ordering_and_scope():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/b1", username="bob", time_class="blitz"), [
        _mistake("https://x/b1", username="bob", time_class="blitz", cp_loss=2000,
                 end_time=2000, date="2024-01-02", move_number=1, category="hung a piece"),
    ], depth=14)
    save_game(conn, _game("https://x/b2", username="bob", time_class="rapid"), [
        _mistake("https://x/b2", username="bob", time_class="rapid", cp_loss=2000,
                 end_time=1000, date="2024-01-01", move_number=2, category="hung a piece"),
    ], depth=14)

    blitz_only = practice_pool(conn, "hung a piece", time_class="blitz")
    assert [m["move_number"] for m in blitz_only] == [1]

    both = practice_pool(conn, "hung a piece")
    # tied cp_loss -> most recent (move 1, end_time 2000) first
    assert [m["move_number"] for m in both] == [1, 2]


# ---- practice_stats() ------------------------------------------------

def _log_attempt(conn, practicing_user, mistake_id, owner, category, verdict,
                 hint_used=False, created_at="2024-01-01T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO practice_attempts "
        "(practicing_user, mistake_id, owner, category, verdict, hint_used, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (practicing_user, mistake_id, owner, category, verdict, int(hint_used), created_at))


def _mistake_ids(conn, username):
    return [r["id"] for r in conn.execute(
        "SELECT id FROM mistakes WHERE username = ? ORDER BY id", (username,)).fetchall()]


def test_practice_stats_zero_attempts_returns_zeroed_dict_not_none():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=2000, move_number=1, category="hung a piece"),
    ], depth=14)

    stats = practice_stats(conn, "alice")
    assert stats["overall"] == {"total": 1, "attempted": 0, "solved": 0}
    assert stats["hint_rate"] is None
    assert stats["recent"] == []


def test_practice_stats_overall_total_is_not_capped_at_the_practice_queue_limit():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1", moves=1000, om=1000, mm=0, em=0), [
        _mistake("https://x/g1", cp_loss=2000 - i, move_number=i + 1, category="a")
        for i in range(PRACTICE_QUEUE_LIMIT + 20)
    ], depth=14)

    # practice_queue() itself is still capped (it's an ordering/display
    # concern for practice mode), but the achievements total must not be.
    assert len(practice_queue(conn, "alice")) == PRACTICE_QUEUE_LIMIT
    assert practice_stats(conn, "alice")["overall"]["total"] == PRACTICE_QUEUE_LIMIT + 20


def test_practice_stats_overall_progress():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=2000, move_number=1, category="a"),
        _mistake("https://x/g1", cp_loss=1900, move_number=2, category="a"),
    ], depth=14)
    ids = _mistake_ids(conn, "alice")

    _log_attempt(conn, "alice", ids[0], "alice", "a", "best")
    _log_attempt(conn, "alice", ids[1], "alice", "a", "mistake")

    stats = practice_stats(conn, "alice")
    assert stats["overall"] == {"total": 2, "attempted": 2, "solved": 1}


def test_practice_stats_hint_used_attempt_does_not_count_as_solved():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=2000, move_number=1, category="a"),
    ], depth=14)
    mistake_id = _mistake_ids(conn, "alice")[0]

    _log_attempt(conn, "alice", mistake_id, "alice", "a", "best", hint_used=True)
    assert practice_stats(conn, "alice")["overall"]["solved"] == 0

    # A later hint-free attempt on the same position must flip it to solved.
    _log_attempt(conn, "alice", mistake_id, "alice", "a", "best", hint_used=False)
    assert practice_stats(conn, "alice")["overall"]["solved"] == 1


def test_practice_stats_by_category_own_vs_others():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/a1", username="alice"), [
        _mistake("https://x/a1", username="alice", cp_loss=2000, move_number=1,
                 category="hung a piece"),
    ], depth=14)
    save_game(conn, _game("https://x/b1", username="bob", moves=1000, om=1000, mm=0, em=0), [
        _mistake("https://x/b1", username="bob", cp_loss=2000 - i, move_number=i + 1,
                 category="hung a piece")
        for i in range(PRACTICE_QUEUE_LIMIT + 20)
    ], depth=14)

    alice_id = _mistake_ids(conn, "alice")[0]
    bob_ids = _mistake_ids(conn, "bob")

    _log_attempt(conn, "alice", alice_id, "alice", "hung a piece", "best")
    _log_attempt(conn, "alice", bob_ids[0], "bob", "hung a piece", "best")

    by_cat = {row["category"]: row for row in practice_stats(conn, "alice")["by_category"]}
    row = by_cat["hung a piece"]
    assert row["own_total"] == 1
    assert row["own_solved"] == 1
    assert row["own_rate"] == 1.0
    # practice_pool() would cap this at PRACTICE_QUEUE_LIMIT -- the real
    # count (bob's mistakes) must not be silently truncated to that limit.
    assert row["others_total"] == PRACTICE_QUEUE_LIMIT + 20
    assert row["others_solved"] == 1


def test_practice_stats_category_with_zero_others_shows_none_rate():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/a1", username="alice"), [
        _mistake("https://x/a1", username="alice", cp_loss=2000, move_number=1,
                 category="hung a piece"),
    ], depth=14)

    by_cat = {row["category"]: row for row in practice_stats(conn, "alice")["by_category"]}
    assert by_cat["hung a piece"]["others_total"] == 0
    assert by_cat["hung a piece"]["others_rate"] is None


def test_practice_stats_hint_rate_is_over_successful_attempts_only():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1"), [
        _mistake("https://x/g1", cp_loss=2000, move_number=1, category="a"),
        _mistake("https://x/g1", cp_loss=1900, move_number=2, category="a"),
        _mistake("https://x/g1", cp_loss=1800, move_number=3, category="a"),
    ], depth=14)
    ids = _mistake_ids(conn, "alice")

    _log_attempt(conn, "alice", ids[0], "alice", "a", "best", hint_used=True)
    _log_attempt(conn, "alice", ids[1], "alice", "a", "also_fine", hint_used=False)
    _log_attempt(conn, "alice", ids[2], "alice", "a", "mistake", hint_used=True)  # excluded

    stats = practice_stats(conn, "alice")
    assert stats["hint_sample"] == 2  # only the two successful attempts
    assert stats["hint_rate"] == 0.5


def test_practice_stats_recent_history_order_and_limit():
    conn = open_db(":memory:")
    save_game(conn, _game("https://x/g1", moves=1000, om=1000, mm=0, em=0), [
        _mistake("https://x/g1", cp_loss=2000, move_number=i + 1, category="a")
        for i in range(25)
    ], depth=14)
    ids = _mistake_ids(conn, "alice")

    for i, mistake_id in enumerate(ids):
        _log_attempt(conn, "alice", mistake_id, "alice", "a", "best",
                    created_at=f"2024-01-{i + 1:02d}T00:00:00+00:00")

    recent = practice_stats(conn, "alice")["recent"]
    assert len(recent) == 20
    assert recent[0]["created_at"] == "2024-01-25T00:00:00+00:00"  # newest first
