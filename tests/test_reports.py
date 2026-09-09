from chess_tracker.reports import _scope


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
