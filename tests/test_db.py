from chess_tracker.db import already_analysed, get_settings, open_db, save_game, set_settings

_REC = {
    "url": "https://example.com/g1", "username": "alice", "end_time": 1000,
    "date": "2024-01-01", "time_class": "blitz", "my_colour": "white",
    "my_rating": 1500, "opp_rating": 1400, "result": "win", "eco": "C00",
    "moves_played": 20, "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
}

_MISTAKE = {
    "game_url": _REC["url"], "username": "alice", "date": _REC["date"], "end_time": 1000,
    "time_class": "blitz", "my_rating": 1500, "my_colour": "white", "move_number": 5,
    "phase": "opening", "severity": "blunder", "cp_loss": 300, "category": "hung a pawn",
    "played": "e4", "best": "d4", "clock_seconds": 20.0, "fen": "fen-string",
}


def test_open_db_creates_current_schema():
    conn = open_db(":memory:")
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(games)").fetchall()}
    assert {"url", "username", "opening_moves", "middlegame_moves", "endgame_moves"} <= cols


def test_already_analysed_false_until_saved_at_that_depth():
    conn = open_db(":memory:")
    assert already_analysed(conn, _REC["url"], "alice", 14) is False
    save_game(conn, _REC, [_MISTAKE], depth=14)
    assert already_analysed(conn, _REC["url"], "alice", 14) is True
    assert already_analysed(conn, _REC["url"], "alice", 18) is False


def test_save_game_round_trips_game_and_mistakes():
    conn = open_db(":memory:")
    save_game(conn, _REC, [_MISTAKE], depth=14)

    stored = conn.execute(
        "SELECT * FROM games WHERE url = ? AND username = ?",
        (_REC["url"], "alice")).fetchone()
    assert stored["moves_played"] == 20
    assert stored["opening_moves"] == 10

    stored_mistakes = conn.execute(
        "SELECT * FROM mistakes WHERE game_url = ?", (_REC["url"],)).fetchall()
    assert len(stored_mistakes) == 1
    assert stored_mistakes[0]["category"] == "hung a pawn"


def test_save_game_replaces_earlier_shallower_analysis():
    conn = open_db(":memory:")
    save_game(conn, _REC, [_MISTAKE], depth=14)
    deeper_mistake = {**_MISTAKE, "category": "positional or planning error"}
    save_game(conn, _REC, [deeper_mistake], depth=18)

    mistakes = conn.execute(
        "SELECT * FROM mistakes WHERE game_url = ?", (_REC["url"],)).fetchall()
    assert len(mistakes) == 1
    assert mistakes[0]["category"] == "positional or planning error"
    assert already_analysed(conn, _REC["url"], "alice", 18) is True


def test_get_settings_returns_defaults_when_nothing_stored():
    conn = open_db(":memory:")
    settings = get_settings(conn)
    assert settings["primary_user"] is None
    assert settings["email"] == ""
    assert settings["depth"] == 14
    assert settings["threads"] == 2
    assert settings["pause"] == 0.6
    assert settings["min_loss"] == 50  # INACCURACY


def test_set_settings_upserts_and_casts_back_to_the_default_type():
    conn = open_db(":memory:")
    set_settings(conn, primary_user="alice", email="alice@example.com", depth=16, pause=0.8)
    settings = get_settings(conn)
    assert settings["primary_user"] == "alice"
    assert settings["email"] == "alice@example.com"
    assert settings["depth"] == 16
    assert isinstance(settings["depth"], int)
    assert settings["pause"] == 0.8
    assert isinstance(settings["pause"], float)
    # untouched settings still fall back to their defaults
    assert settings["threads"] == 2

    # a second call overwrites rather than duplicating
    set_settings(conn, depth=20)
    settings = get_settings(conn)
    assert settings["depth"] == 20
    assert settings["primary_user"] == "alice"  # earlier settings untouched
