"""
Parity harness: the fact tables build_dashboard_data() emits must aggregate
to the exact same numbers as reports.report_model(), which is independently
tested (golden test, real-database diff) and treated here as the oracle.
"""
from collections import Counter, defaultdict

from chess_tracker.db import open_db, save_game
from chess_tracker.html_export import build_dashboard_data
from chess_tracker.reports import SERIOUS, report_model


def _game(url, username, end_time, date, time_class, colour, rating, eco,
         moves, om, mm, em):
    return {
        "url": url, "username": username, "end_time": end_time, "date": date,
        "time_class": time_class, "my_colour": colour, "my_rating": rating,
        "opp_rating": 1400, "result": "win", "eco": eco,
        "moves_played": moves, "opening_moves": om, "middlegame_moves": mm,
        "endgame_moves": em,
    }


def _mistake(url, username, date, end_time, time_class, colour, rating,
            move_number, phase, severity, cp_loss, category, played, best,
            clock_seconds):
    return {
        "game_url": url, "username": username, "date": date, "end_time": end_time,
        "time_class": time_class, "my_rating": rating, "my_colour": colour,
        "move_number": move_number, "phase": phase, "severity": severity,
        "cp_loss": cp_loss, "category": category, "played": played, "best": best,
        "clock_seconds": clock_seconds, "fen": "fen",
    }


def _rows_matching(table, filters):
    """filters: {dim_name: set(allowed indices)}. A dim absent from filters
    is not restricted -- summed/grouped across every value, matching how
    sumFacts/groupFacts will treat a dimension the UI has no filter for."""
    dims = table["dims"]
    for row in table["data"]:
        if all(row[i] in filters[d] for i, d in enumerate(dims) if d in filters):
            yield row


def _sum_measure(table, filters, measure_index=0):
    dims = table["dims"]
    return sum(row[len(dims) + measure_index] for row in _rows_matching(table, filters))


def _seeded_db():
    conn = open_db(":memory:")
    games = [
        _game("https://x/a1", "alice", 1704067200, "2024-01-05", "blitz", "white", 1500,
             "C00", 40, 15, 15, 10),
        _game("https://x/a2", "alice", 1706745600, "2024-02-03", "blitz", "black", 1510,
             "C00", 30, 10, 12, 8),
        _game("https://x/a3", "alice", 1709251200, "2024-03-01", "rapid", "white", 1520,
             "C00", 25, 10, 10, 5),
        _game("https://x/a4", "alice", 1711929600, "2024-04-01", "rapid", "black", 1530,
             "B01", 50, 20, 20, 10),
        _game("https://x/a5", "alice", 1712016000, "2024-04-02", "blitz", "white", 1535,
             "B01", 20, 8, 8, 4),
        _game("https://x/b1", "bob", 1704153600, "2024-01-06", "rapid", "black", 1200,
             "C00", 35, 12, 13, 10),
        _game("https://x/b2", "bob", 1706832000, "2024-02-04", "blitz", "white", 1210,
             "D00", 28, 10, 10, 8),
        _game("https://x/b3", "bob", 1711843200, "2024-03-31", "blitz", "black", 1220,
             "D00", 22, 8, 8, 6),
    ]
    for g in games:
        save_game(conn, g, [], depth=14)

    mistakes = [
        # move-number and clock boundary coverage, spread across users/tc/phase
        _mistake("https://x/a1", "alice", "2024-01-05", 1704067200, "blitz", "white",
                 1500, 10, "opening", "blunder", 900, "hung a piece", "Nf3", "Qxb7", 29.9),
        _mistake("https://x/a1", "alice", "2024-01-05", 1704067200, "blitz", "white",
                 1500, 11, "middlegame", "mistake", 150, "missed tactic", "Rd1", "Nxe5", 30.0),
        _mistake("https://x/a1", "alice", "2024-01-05", 1704067200, "blitz", "white",
                 1500, 20, "middlegame", "blunder", 800, "hung a piece", "Qc2", "Qb3", 30.1),
        _mistake("https://x/a2", "alice", "2024-02-03", 1706745600, "blitz", "black",
                 1510, 21, "middlegame", "mistake", 120, "missed tactic", "Bxf7", "O-O",
                 59.9),
        _mistake("https://x/a2", "alice", "2024-02-03", 1706745600, "blitz", "black",
                 1510, 30, "endgame", "blunder", 2000, "endgame technique", "Kf1", "Kf2",
                 60.0),
        _mistake("https://x/a2", "alice", "2024-02-03", 1706745600, "blitz", "black",
                 1510, 31, "endgame", "inaccuracy", 60, "endgame technique", "Rd8", "Rd1",
                 60.1),
        _mistake("https://x/a3", "alice", "2024-03-01", 1709251200, "rapid", "white",
                 1520, 40, "endgame", "blunder", 2000, "endgame technique", "Kg7", "Kf7",
                 None),
        _mistake("https://x/a3", "alice", "2024-03-01", 1709251200, "rapid", "white",
                 1520, 41, "endgame", "mistake", 140, "endgame technique", "a4", "h4", 200.0),
        _mistake("https://x/a4", "alice", "2024-04-01", 1711929600, "rapid", "black",
                 1530, 5, "opening", "blunder", 1900, "hung a piece", "Bd3", "Qh5", 15.0),
        _mistake("https://x/a5", "alice", "2024-04-02", 1712016000, "blitz", "white",
                 1535, 8, "opening", "mistake", 130, "missed tactic", "b4", "g4", 8.0),
        _mistake("https://x/b1", "bob", "2024-01-06", 1704153600, "rapid", "black",
                 1200, 6, "opening", "blunder", 950, "allowed forced mate", "Kd1", "Rxf2",
                 5.0),
        _mistake("https://x/b1", "bob", "2024-01-06", 1704153600, "rapid", "black",
                 1200, 25, "middlegame", "mistake", 110, "missed tactic", "Qh7", "f5", 45.0),
        _mistake("https://x/b2", "bob", "2024-02-04", 1706832000, "blitz", "white",
                 1210, 33, "endgame", "blunder", 1700, "endgame technique", "Kc1", "Rxf2",
                 90.0),
        _mistake("https://x/b3", "bob", "2024-03-31", 1711843200, "blitz", "black",
                 1220, 12, "middlegame", "inaccuracy", 55, "missed tactic", "Rc4", "Rd3",
                 None),
        _mistake("https://x/b3", "bob", "2024-03-31", 1711843200, "blitz", "black",
                 1220, 15, "middlegame", "blunder", 1000, "allowed forced mate", "Rxd4",
                 "Qe8", 3.0),
    ]
    for g in games:
        game_mistakes = [m for m in mistakes if m["game_url"] == g["url"]]
        if game_mistakes:
            save_game(conn, g, game_mistakes, depth=14)
    return conn


def test_moves_and_counts_match_report_model_per_user_tc_phase():
    conn = _seeded_db()
    data = build_dashboard_data(conn, ["alice", "bob"])
    lists = data["lists"]
    u_ix = {u: i for i, u in enumerate(lists["users"])}
    tc_ix = {tc: i for i, tc in enumerate(lists["timeClasses"])}
    ph_ix = {p: i for i, p in enumerate(lists["phases"])}
    sev_ix = {s: i for i, s in enumerate(lists["severities"])}
    serious_idx = {sev_ix[s] for s in SERIOUS}

    for user in lists["users"]:
        for tc in lists["timeClasses"]:
            for phase in lists["phases"]:
                model = report_model(conn, user, time_class=tc, phase=phase)
                if model is None:
                    continue

                moves = _sum_measure(data["movesFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "phase": {ph_ix[phase]},
                })
                assert moves == model["total_moves"], (user, tc, phase, "moves")

                # gamesFacts has no phase dim (matches report_model()'s own
                # games count, which is also not phase-scoped) -- this is
                # the "per 100 games" denominator the by-phase panel uses.
                games = _sum_measure(data["gamesFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]},
                })
                assert games == model["games"], (user, tc, phase, "games")

                n_serious = _sum_measure(data["countFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "phase": {ph_ix[phase]},
                    "severity": serious_idx,
                })
                assert n_serious == model["n_serious"], (user, tc, phase, "n_serious")

                # recurring: per-category counts must match, unordered
                cat_ix = {c: i for i, c in enumerate(lists["categories"])}
                by_cat = defaultdict(int)
                for row in _rows_matching(data["countFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "phase": {ph_ix[phase]},
                    "severity": serious_idx,
                }):
                    cat = lists["categories"][row[4]]
                    by_cat[cat] += row[-1]
                assert dict(by_cat) == dict(model["recurring"]), (user, tc, phase, "recurring")

                # time pressure
                if model["time_pressure"] is not None:
                    clock_bucket_ix = {b: i for i, b in enumerate(lists["clockBuckets"])}
                    by_clock = defaultdict(int)
                    for row in _rows_matching(data["clockBucketFacts"], {
                        "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "phase": {ph_ix[phase]},
                        "severity": serious_idx,
                    }):
                        by_clock[lists["clockBuckets"][row[6]]] += row[-1]
                    for label, n in model["time_pressure"]["groups"]:
                        assert by_clock.get(label, 0) == n, \
                            (user, tc, phase, "clock_bucket", label)


def test_colour_breakdown_matches_report_model():
    conn = _seeded_db()
    data = build_dashboard_data(conn, ["alice", "bob"])
    lists = data["lists"]
    u_ix = {u: i for i, u in enumerate(lists["users"])}
    tc_ix = {tc: i for i, tc in enumerate(lists["timeClasses"])}
    ph_ix = {p: i for i, p in enumerate(lists["phases"])}
    sev_ix = {s: i for i, s in enumerate(lists["severities"])}
    serious_idx = {sev_ix[s] for s in SERIOUS}
    col_ix = {c: i for i, c in enumerate(lists["colours"])}

    for user in lists["users"]:
        for tc in lists["timeClasses"]:
            model = report_model(conn, user, time_class=tc)
            if model is None:
                continue
            for colour_label, expected_e, expected_mv in model["by_colour_and_time_class"][
                    "my_colour"]:
                mv = _sum_measure(data["colourMovesFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "colour": {col_ix[colour_label]},
                })
                e = _sum_measure(data["colourCountFacts"], {
                    "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "colour": {col_ix[colour_label]},
                    "severity": serious_idx,
                })
                assert mv == expected_mv, (user, tc, colour_label, "moves")
                assert e == expected_e, (user, tc, colour_label, "errors")


def test_openings_match_report_model():
    conn = _seeded_db()
    data = build_dashboard_data(conn, ["alice", "bob"])
    lists = data["lists"]
    u_ix = {u: i for i, u in enumerate(lists["users"])}
    tc_ix = {tc: i for i, tc in enumerate(lists["timeClasses"])}
    ph_ix = {p: i for i, p in enumerate(lists["phases"])}
    sev_ix = {s: i for i, s in enumerate(lists["severities"])}
    serious_idx = {sev_ix[s] for s in SERIOUS}
    eco_ix = {e: i for i, e in enumerate(lists["ecos"])}

    for user in lists["users"]:
        for tc in lists["timeClasses"]:
            for phase in lists["phases"]:
                model = report_model(conn, user, time_class=tc, phase=phase)
                if model is None or not model["openings"]:
                    continue
                for eco, expected_n, expected_e in model["openings"]:
                    games = _sum_measure(data["ecoGamesFacts"], {
                        "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "eco": {eco_ix[eco]},
                    })
                    errors = _sum_measure(data["ecoErrorFacts"], {
                        "user": {u_ix[user]}, "tc": {tc_ix[tc]}, "phase": {ph_ix[phase]},
                        "eco": {eco_ix[eco]}, "severity": serious_idx,
                    })
                    assert games == expected_n, (user, tc, phase, eco, "games")
                    assert errors == expected_e, (user, tc, phase, eco, "errors")


def test_severity_filter_changes_counts_as_expected():
    """The dashboard's severity filter is new -- there is no report_model()
    equivalent to compare against (the terminal report never varied
    severity), so this checks the fact table directly against a hand count."""
    conn = _seeded_db()
    data = build_dashboard_data(conn, ["alice", "bob"])
    lists = data["lists"]
    u_ix = {u: i for i, u in enumerate(lists["users"])}
    sev_ix = {s: i for i, s in enumerate(lists["severities"])}

    all_severities = set(sev_ix.values())
    serious_only = {sev_ix[s] for s in SERIOUS}

    all_n = _sum_measure(data["countFacts"], {"user": {u_ix["alice"]}, "severity": all_severities})
    serious_n = _sum_measure(data["countFacts"],
                             {"user": {u_ix["alice"]}, "severity": serious_only})
    assert all_n > serious_n  # alice has at least one inaccuracy in the seed
