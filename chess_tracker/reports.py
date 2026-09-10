"""Reporting and comparison, straight from the database."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict

from .analysis import PHASES

# Error categories counted as "serious" everywhere in the report and (from
# phase 2 on) the dashboard's default severity filter, so the two agree by
# default instead of by coincidence.
SERIOUS = ("mistake", "blunder")

# Upper bound (inclusive) of each move-number bucket except the last, which
# is open-ended. Kept as a plain edges+labels pair, with the SQL CASE built
# from the same numbers below, so the Python bucketing used by the terminal
# report and any SQL bucketing used by the dashboard can never drift apart.
MOVE_BUCKET_EDGES = (10, 20, 30, 40)
MOVE_BUCKET_LABELS = ("1-10", "11-20", "21-30", "31-40", "41+")

# Upper bound (exclusive) of each clock-seconds bucket except the last.
CLOCK_BUCKET_EDGES = (30, 60)
CLOCK_BUCKET_LABELS = ("under 30s left", "30 to 60s left", "over 60s left")

ECO_MIN_GAMES = 3
ECO_TOP_N = 8
TOP_POSITIONS = 10
PRACTICE_QUEUE_LIMIT = 100
TIME_PRESSURE_ALERT = 0.30  # fraction of clocked errors under the first bucket
TREND_MIN_MONTHS = 2
DELTA_EPSILON = 0.05  # per-100-moves change below this counts as "flat"


def _bucket_sql_case(column: str, edges: tuple[int, ...], labels: tuple[str, ...],
                      op: str) -> str:
    whens = " ".join(f"WHEN {column} {op} {edge} THEN '{label}'"
                      for edge, label in zip(edges, labels))
    return f"CASE {whens} ELSE '{labels[-1]}' END"


MOVE_BUCKET_SQL_CASE = _bucket_sql_case("move_number", MOVE_BUCKET_EDGES,
                                        MOVE_BUCKET_LABELS, "<=")
CLOCK_BUCKET_SQL_CASE = _bucket_sql_case("clock_seconds", CLOCK_BUCKET_EDGES,
                                         CLOCK_BUCKET_LABELS, "<")


def move_bucket(move_number: int) -> str:
    """Which move-number bucket a mistake falls in. Pinned against
    MOVE_BUCKET_SQL_CASE by an exhaustive test -- see test_reports.py."""
    for edge, label in zip(MOVE_BUCKET_EDGES, MOVE_BUCKET_LABELS):
        if move_number <= edge:
            return label
    return MOVE_BUCKET_LABELS[-1]


def clock_bucket(clock_seconds: float) -> str:
    """Which clock-pressure bucket a mistake falls in. Callers must exclude
    NULL clocks themselves (there is no bucket for "unknown"), matching how
    the time-pressure section is scoped to clocked mistakes only."""
    for edge, label in zip(CLOCK_BUCKET_EDGES, CLOCK_BUCKET_LABELS):
        if clock_seconds < edge:
            return label
    return CLOCK_BUCKET_LABELS[-1]


def _scope(user: str, time_class: str | None = None, phase: str | None = None,
           last_days: int | None = None) -> tuple[str, list, str, list]:
    """
    Build WHERE clauses (and matching params) scoped to one user, for
    querying `games` and `mistakes`. `mistakes` additionally supports the
    `phase` filter since that column only exists on `mistakes`, not `games`.
    Shared by compare() and report() so the filter-building logic exists in
    exactly one place.
    """
    params: list = [user.lower()]
    games_where = "WHERE username = ?"
    if time_class:
        games_where += " AND time_class = ?"
        params.append(time_class)
    if last_days:
        games_where += " AND end_time >= ?"
        params.append(int(time.time()) - last_days * 86400)

    mistakes_where = games_where
    mistakes_params = list(params)
    if phase:
        mistakes_where += " AND phase = ?"
        mistakes_params.append(phase)

    return games_where, params, mistakes_where, mistakes_params


def user_summaries(conn: sqlite3.Connection) -> list[dict]:
    """
    One summary row per tracked user: games/moves stored, error rate, sample
    depth, date range. Shared by list_users() (CLI text output) and the web
    app's home page (a table).
    """
    rows = conn.execute("""
        SELECT g.username,
               COUNT(*)                AS games,
               SUM(g.moves_played)     AS moves,
               MIN(g.date)             AS first_game,
               MAX(g.date)             AS last_game,
               MIN(g.depth)            AS min_depth
        FROM games g GROUP BY g.username ORDER BY games DESC
    """).fetchall()

    summaries = []
    for r in rows:
        errs = conn.execute("""SELECT COUNT(*) c FROM mistakes
            WHERE username = ? AND severity IN ('mistake','blunder')""",
            (r["username"],)).fetchone()["c"]
        rate = errs / r["moves"] * 100 if r["moves"] else 0
        summaries.append({
            "username": r["username"], "games": r["games"], "moves": r["moves"],
            "error_rate": rate, "min_depth": r["min_depth"],
            "first_game": r["first_game"], "last_game": r["last_game"],
            "thin_sample": r["games"] < 100,
        })
    return summaries


def list_users(conn: sqlite3.Connection) -> str:
    """Who is in this database and how solid is each sample."""
    summaries = user_summaries(conn)
    if not summaries:
        return "No users stored yet."

    out = ["=" * 72,
           "USERS IN THIS DATABASE",
           "=" * 72,
           f"{'user':<18}{'games':>7}{'moves':>8}{'err/100':>9}{'depth':>7}  range",
           "-" * 72]
    for s in summaries:
        note = "" if not s["thin_sample"] else "  (thin sample)"
        out.append(f"{s['username']:<18}{s['games']:>7}{s['moves']:>8}"
                   f"{s['error_rate']:>9.2f}{s['min_depth']:>7}  "
                   f"{s['first_game']} to {s['last_game']}{note}")
    out.append("=" * 72)
    return "\n".join(out)


def compare(conn: sqlite3.Connection, users: list[str],
            time_class: str | None = None, phase: str | None = None) -> str:
    """
    Side by side error rates across every mistake category, at every
    severity (inaccuracy, mistake, blunder). The point is not who is better
    overall, it is which categories differ. Rates are per 100 of that
    player's own moves (or, with --phase, per 100 of that player's own moves
    in that phase), so unequal sample sizes stay comparable.
    """
    users = [u.lower() for u in users]
    stats: dict[str, dict] = {}
    moves_col = f"{phase}_moves" if phase else "moves_played"

    for u in users:
        games_clause, params, mistakes_clause, mistakes_params = _scope(u, time_class, phase)

        moves = conn.execute(
            f"SELECT COALESCE(SUM({moves_col}),0) m, COUNT(*) g FROM games {games_clause}",
            params).fetchone()
        cats = conn.execute(
            f"""SELECT category, COUNT(*) c FROM mistakes {mistakes_clause}
                GROUP BY category""",
            mistakes_params).fetchall()
        stats[u] = {"moves": moves["m"], "games": moves["g"],
                    "cats": {r["category"]: r["c"] for r in cats}}

    present = [u for u in users if stats[u]["moves"]]
    if len(present) < 2:
        return ("Need at least two users with stored games to compare. "
                "Run the scan for each of them first" +
                (" (per-phase rates need the games backfilled/reanalysed first)"
                 if phase else "") + ".")

    w = max(max(len(u) for u in present), 9)
    out = ["=" * (34 + (w + 2) * len(present)),
           "COMPARISON  (errors per 100 of that player's own"
           + (f" {phase}" if phase else "") + " moves, "
           "inaccuracy + mistake + blunder)"
           + (f"  [{time_class}]" if time_class else ""),
           "=" * (34 + (w + 2) * len(present)),
           f"{'':<32}" + "".join(f"{u:>{w + 2}}" for u in present),
           "-" * (34 + (w + 2) * len(present)),
           f"{'games analysed':<32}"
           + "".join(f"{stats[u]['games']:>{w + 2}}" for u in present),
           f"{'moves analysed':<32}"
           + "".join(f"{stats[u]['moves']:>{w + 2}}" for u in present),
           ""]

    every_cat = sorted({c for u in present for c in stats[u]["cats"]})

    def rate(u: str, c: str) -> float:
        return stats[u]["cats"].get(c, 0) / stats[u]["moves"] * 100

    totals = {u: sum(stats[u]["cats"].values()) / stats[u]["moves"] * 100
              for u in present}
    out.append(f"{'ALL ERRORS':<32}"
               + "".join(f"{totals[u]:>{w + 2}.2f}" for u in present))
    out.append("-" * (34 + (w + 2) * len(present)))

    # Biggest gaps first. Those are the categories worth acting on.
    for c in sorted(every_cat, key=lambda c: -(max(rate(u, c) for u in present)
                                               - min(rate(u, c) for u in present))):
        label = c if len(c) <= 30 else c[:27] + "..."
        out.append(f"  {label:<30}" + "".join(f"{rate(u, c):>{w + 2}.2f}" for u in present))

    out.append("")
    ref = present[0]
    gaps = [(rate(ref, c) - min(rate(u, c) for u in present[1:]), c)
            for c in every_cat]
    worst = [g for g in sorted(gaps, reverse=True) if g[0] > 0][:3]
    if worst:
        out.append(f"Where {ref} loses the most ground against the others:")
        for gap, c in worst:
            out.append(f"  +{gap:5.2f} per 100 moves   {c}")
        out.append("")
        out.append("Those gaps, not the overall total, are the training targets.")
    out.append("=" * (34 + (w + 2) * len(present)))
    return "\n".join(out)


def bar(n: int, total: int, width: int = 28) -> str:
    filled = 0 if total == 0 else round(width * n / total)
    return "#" * filled + "." * (width - filled)


def practice_queue(conn: sqlite3.Connection, user: str, time_class: str | None = None,
                    phase: str | None = None, category: str | None = None) -> list[sqlite3.Row]:
    """
    Mistakes ordered for practice: worst first, then most recent among ties,
    then a stable id -- the same total order as "positions to review" in the
    terminal report and dashboard, capped at PRACTICE_QUEUE_LIMIT rather than
    TOP_POSITIONS. report_model() calls this directly (see below) so the
    ordering lives in exactly one place -- a SQL ORDER BY here, not a
    separately maintained Python sort key.
    """
    u = user.lower()
    _, _, mistakes_where, mistakes_params = _scope(u, time_class, phase)
    if category:
        mistakes_where += " AND category = ?"
        mistakes_params.append(category)
    return conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} "
        f"AND severity IN ({','.join('?' * len(SERIOUS))}) "
        f"ORDER BY cp_loss DESC, end_time DESC, id DESC LIMIT ?",
        [*mistakes_params, *SERIOUS, PRACTICE_QUEUE_LIMIT]).fetchall()


def practice_pool(conn: sqlite3.Connection, category: str, exclude_user: str | None = None,
                  time_class: str | None = None, phase: str | None = None) -> list[sqlite3.Row]:
    """
    Same ordering and severity scope as practice_queue(), but across every
    tracked player instead of one -- for practice mode's "extend to others
    making the same mistake" prompt, once a player's own queue for a
    category is running low. `exclude_user` leaves out the player whose own
    queue is already shown (their rows would otherwise be duplicated).
    """
    where = "WHERE category = ?"
    params: list = [category]
    if exclude_user:
        where += " AND username != ?"
        params.append(exclude_user.lower())
    if time_class:
        where += " AND time_class = ?"
        params.append(time_class)
    if phase:
        where += " AND phase = ?"
        params.append(phase)
    return conn.execute(
        f"SELECT * FROM mistakes {where} "
        f"AND severity IN ({','.join('?' * len(SERIOUS))}) "
        f"ORDER BY cp_loss DESC, end_time DESC, id DESC LIMIT ?",
        [*params, *SERIOUS, PRACTICE_QUEUE_LIMIT]).fetchall()


def practice_stats(conn: sqlite3.Connection, user: str, time_class: str | None = None,
                   phase: str | None = None) -> dict:
    """
    Everything the Achievements page shows about practice-mode usage for one
    player. Unlike report_model(), always returns a real dict, never None --
    a player can have analysed games (report_model() succeeds) but zero
    practice attempts yet, which is a milder, separate empty state.

    `overall["total"]` is scoped to practice_queue() (capped at
    PRACTICE_QUEUE_LIMIT, i.e. what's actually offered in practice mode
    today), while by_category's own_total/others_total below are unclipped
    counts straight off `mistakes`. A player with one very lopsided category
    can show a bigger own_total there than overall["total"] -- each number
    answers a different question (what's reachable overall vs. how mistakes
    split by category), so don't try to reconcile them.
    """
    u = user.lower()
    _, _, mistakes_where, mistakes_params = _scope(u, time_class, phase)
    severity_in = f"AND severity IN ({','.join('?' * len(SERIOUS))})"

    total = len(practice_queue(conn, u, time_class, phase))
    attempted = conn.execute(
        "SELECT COUNT(DISTINCT mistake_id) c FROM practice_attempts WHERE practicing_user = ?",
        [u]).fetchone()["c"]
    solved = conn.execute(
        "SELECT COUNT(DISTINCT mistake_id) c FROM practice_attempts "
        "WHERE practicing_user = ? AND hint_used = 0 AND verdict IN ('best','also_fine')",
        [u]).fetchone()["c"]

    own_totals = dict(conn.execute(
        f"SELECT category, COUNT(*) c FROM mistakes {mistakes_where} {severity_in} "
        f"GROUP BY category", [*mistakes_params, *SERIOUS]).fetchall())

    others_where = "WHERE username != ?"
    others_params: list = [u]
    if time_class:
        others_where += " AND time_class = ?"
        others_params.append(time_class)
    if phase:
        others_where += " AND phase = ?"
        others_params.append(phase)
    # Deliberately NOT practice_pool(): that's capped at PRACTICE_QUEUE_LIMIT
    # per category (built for the practice-mode queue, not a denominator),
    # which would silently undercount any category with more than 100
    # other-player mistakes.
    others_totals = dict(conn.execute(
        f"SELECT category, COUNT(*) c FROM mistakes {others_where} {severity_in} "
        f"GROUP BY category", [*others_params, *SERIOUS]).fetchall())

    own_solved = dict(conn.execute(
        "SELECT category, COUNT(DISTINCT mistake_id) c FROM practice_attempts "
        "WHERE practicing_user = ? AND owner = ? AND hint_used = 0 "
        "AND verdict IN ('best','also_fine') GROUP BY category",
        [u, u]).fetchall())
    others_solved = dict(conn.execute(
        "SELECT category, COUNT(DISTINCT mistake_id) c FROM practice_attempts "
        "WHERE practicing_user = ? AND owner != ? AND hint_used = 0 "
        "AND verdict IN ('best','also_fine') GROUP BY category",
        [u, u]).fetchall())

    def rate(solved_n: int, total_n: int) -> float | None:
        return solved_n / total_n if total_n else None

    by_category = []
    for cat in sorted(set(own_totals) | set(others_totals)):
        own_total = own_totals.get(cat, 0)
        oth_total = others_totals.get(cat, 0)
        own_s = own_solved.get(cat, 0)
        oth_s = others_solved.get(cat, 0)
        by_category.append({
            "category": cat, "own_total": own_total, "own_solved": own_s,
            "own_rate": rate(own_s, own_total),
            "others_total": oth_total, "others_solved": oth_s,
            "others_rate": rate(oth_s, oth_total),
        })

    hint_row = conn.execute(
        "SELECT SUM(hint_used) hinted, COUNT(*) total FROM practice_attempts "
        "WHERE practicing_user = ? AND verdict IN ('best','also_fine')", [u]).fetchone()
    hint_sample = hint_row["total"] or 0
    hint_rate = hint_row["hinted"] / hint_sample if hint_sample else None

    recent_rows = conn.execute(
        "SELECT * FROM practice_attempts WHERE practicing_user = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 20", [u]).fetchall()
    recent = [{
        "created_at": r["created_at"], "category": r["category"], "owner": r["owner"],
        "is_own": r["owner"] == u, "verdict": r["verdict"], "hint_used": bool(r["hint_used"]),
    } for r in recent_rows]

    return {
        "overall": {"total": total, "attempted": attempted, "solved": solved},
        "by_category": by_category,
        "hint_rate": hint_rate, "hint_sample": hint_sample,
        "recent": recent,
    }


def report_model(conn: sqlite3.Connection, user: str, time_class: str | None = None,
                  last_days: int | None = None, phase: str | None = None) -> dict | None:
    """
    All the numbers behind the terminal report, with no text formatting.
    report() is a thin formatter over this. Returns None if there are no
    stored games for this filter.

    Two deliberate behaviour changes live here rather than in report():
    - "positions" breaks cp_loss ties by most recent, then by a stable id,
      and carries how often each row's category occurs in this selection
      (thousands of mistakes tie at the cp_loss cap -- see analysis.py -- so
      "worst first" alone was surfacing arbitrary, possibly very old rows).
    - the first-half/second-half deltas are sorted deterministically
      (previously iterated a Python set, so tied deltas could reorder
      between runs on identical data).
    """
    u = user.lower()
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        u, time_class, phase, last_days)

    games = conn.execute(f"SELECT * FROM games {games_where} ORDER BY end_time",
                         games_params).fetchall()
    if not games:
        return None

    serious = conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} "
        f"AND severity IN ({','.join('?' * len(SERIOUS))})",
        [*mistakes_params, *SERIOUS]).fetchall()

    moves_col = f"{phase}_moves" if phase else "moves_played"
    total_moves = sum(g[moves_col] or 0 for g in games)
    not_backfilled = phase and sum(1 for g in games if g[moves_col] is None)
    ratings = [g["my_rating"] for g in games if g["my_rating"]]
    n_serious = len(serious)
    moves_label = f"your {phase} moves" if phase else "your moves"
    cat_counts = Counter(m["category"] for m in serious)

    by_phase = None
    if not phase:
        phase_counts = Counter(m["phase"] for m in serious)
        by_phase = [(ph, phase_counts.get(ph, 0)) for ph in PHASES]

    move_counts = Counter(move_bucket(m["move_number"]) for m in serious)
    by_move = [(b, move_counts.get(b, 0)) for b in MOVE_BUCKET_LABELS]

    clocked = [m for m in serious if m["clock_seconds"] is not None]
    time_pressure = None
    if clocked:
        clock_counts = Counter(clock_bucket(m["clock_seconds"]) for m in clocked)
        groups = [(label, clock_counts.get(label, 0)) for label in CLOCK_BUCKET_LABELS]
        time_pressure = {
            "clocked_total": len(clocked),
            "groups": groups,
            "alert": groups[0][1] / len(clocked) > TIME_PRESSURE_ALERT,
        }

    # ---- the whole point of persisting: movement over time -----------------
    monthly: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        monthly[g["date"][:7]][1] += g[moves_col] or 0
    for m in serious:
        monthly[m["date"][:7]][0] += 1

    trend = None
    if len(monthly) >= TREND_MIN_MONTHS:
        rows = sorted(monthly.items())
        rates = [(mo, e / mv * 100 if mv else 0.0, mv) for mo, (e, mv) in rows]

        half = max(len(rows) // 2, 1)
        early_months = {mo for mo, _ in rows[:half]}
        early_moves = sum(mv for _, (_, mv) in rows[:half])
        late_moves = sum(mv for _, (_, mv) in rows[half:])
        early_c: Counter = Counter()
        late_c: Counter = Counter()
        for m in serious:
            (early_c if m["date"][:7] in early_months else late_c)[m["category"]] += 1

        halves = None
        if early_moves and late_moves:
            deltas = []
            for c in sorted(set(early_c) | set(late_c)):
                a = early_c[c] / early_moves * 100
                b = late_c[c] / late_moves * 100
                deltas.append((b - a, a, b, c))
            deltas.sort(key=lambda d: (-abs(d[0]), d[3]))
            halves = deltas[:6]

        trend = {"rates": rates, "halves": halves}

    by_colour_and_time_class = {}
    for key in ("my_colour", "time_class"):
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for g in games:
            agg[g[key]][1] += g[moves_col] or 0
        for m in serious:
            agg[m[key]][0] += 1
        by_colour_and_time_class[key] = sorted(
            (k, e, mv) for k, (e, mv) in agg.items() if mv)

    eco: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        if g["eco"] and g["eco"] != "?":
            eco[g["eco"]][1] += 1
    url_to_eco = {g["url"]: g["eco"] for g in games}
    for m in serious:
        e = url_to_eco.get(m["game_url"])
        if e and e != "?":
            eco[e][0] += 1
    frequent = {k: v for k, v in eco.items() if v[1] >= ECO_MIN_GAMES}
    openings = sorted(((k, n, e) for k, (e, n) in frequent.items()),
                      key=lambda row: -row[2] / row[1])[:ECO_TOP_N]

    top = practice_queue(conn, user, time_class, phase)[:TOP_POSITIONS]
    positions = [{**dict(m), "occurrences": cat_counts[m["category"]]} for m in top]

    return {
        "user": user, "time_class": time_class, "phase": phase,
        "games": len(games), "total_moves": total_moves, "moves_label": moves_label,
        "not_backfilled": not_backfilled or 0,
        "date_range": (games[0]["date"], games[-1]["date"]),
        "ratings": (ratings[0], ratings[-1]) if ratings else None,
        "n_serious": n_serious,
        "recurring": cat_counts.most_common(),
        "by_phase": by_phase,
        "by_move": by_move,
        "time_pressure": time_pressure,
        "trend": trend,
        "by_colour_and_time_class": by_colour_and_time_class,
        "openings": openings,
        "positions": positions,
    }


def report(conn: sqlite3.Connection, user: str, time_class: str | None = None,
           last_days: int | None = None, phase: str | None = None) -> str:
    model = report_model(conn, user, time_class, last_days, phase)
    if model is None:
        return "No games stored yet for that filter."

    phase = model["phase"]
    moves_label = model["moves_label"]
    n_serious = model["n_serious"]
    out: list[str] = []

    def line(s: str = "") -> None:
        out.append(s)

    filters = ", ".join(filter(None, [model["time_class"], phase]))
    line("=" * 64)
    line(f"CHESS ERROR PROFILE  |  {model['user']}" + (f"  [{filters}]" if filters else ""))
    line("=" * 64)
    line(f"Games in store     : {model['games']}")
    line(f"Your moves         : {model['total_moves']}" + (f"  ({phase})" if phase else ""))
    line(f"Date range         : {model['date_range'][0]} to {model['date_range'][1]}")
    if model["ratings"]:
        first, last = model["ratings"]
        line(f"Rating             : {first} then, {last} now ({last - first:+d})")
    line(f"Mistakes + blunders: {n_serious}  "
         f"({n_serious / max(model['total_moves'], 1) * 100:.1f}% of {moves_label})")
    if model["not_backfilled"]:
        line(f"NOTE: {model['not_backfilled']} game(s) have no per-phase move counts yet "
             f"(re-run without --report-only to backfill) -- they are excluded above.")
    line()

    line("-" * 64)
    line("RECURRING ERROR TYPES")
    line("-" * 64)
    for cat, n in model["recurring"]:
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {cat}")
    line()

    if model["by_phase"] is not None:
        line("-" * 64)
        line("WHEN THEY HAPPEN")
        line("-" * 64)
        for ph, n in model["by_phase"]:
            line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {ph}")
        line()

    line("By move number:")
    for b, n in model["by_move"]:
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  moves {b}")
    line()

    tp = model["time_pressure"]
    if tp:
        line("-" * 64)
        line("TIME PRESSURE")
        line("-" * 64)
        for label, n in tp["groups"]:
            line(f"{n:5d}  {n / tp['clocked_total'] * 100:5.1f}%  "
                 f"{bar(n, tp['clocked_total'])}  {label}")
        if tp["alert"]:
            line()
            line(">> Most of your damage happens on a low clock. That is a time")
            line("   management problem, not a chess knowledge problem.")
        line()

    trend = model["trend"]
    if trend:
        line("-" * 64)
        line(f"TREND  (serious errors per 100 of {moves_label})")
        line("-" * 64)
        rates = trend["rates"]
        peak = max((r[1] for r in rates), default=0) or 1
        for mo, rate, mv in rates:
            line(f"  {mo}  {rate:5.1f}  {'#' * round(30 * rate / peak)}  ({mv} moves)")
        first, last = rates[0][1], rates[-1][1]
        line()
        line(f"  {rates[0][0]} to {rates[-1][0]}: {first:.1f} -> {last:.1f} "
             f"({'improving' if last < first else 'getting worse'})")
        line()

        if trend["halves"] is not None:
            line("Per 100 moves, first half of the period vs second half:")
            for d, a, b, c in trend["halves"]:
                arrow = ("worse " if d > DELTA_EPSILON
                         else "better" if d < -DELTA_EPSILON else "flat  ")
                line(f"  {a:5.2f} -> {b:5.2f}  {arrow}  {c}")
            line()

    line("-" * 64)
    line("BY COLOUR AND TIME CONTROL")
    line("-" * 64)
    for key in ("my_colour", "time_class"):
        for k, e, mv in model["by_colour_and_time_class"][key]:
            line(f"  {k:<10} {e / mv * 100:5.2f} errors per 100 moves  ({mv} moves)")
    line()

    if model["openings"]:
        line("-" * 64)
        line("OPENINGS YOU PLAY OFTEN")
        line("-" * 64)
        for k, n, e in model["openings"]:
            line(f"  {k}   {n:3d} games   {e / n:.1f} serious errors per game")
        line()

    line("-" * 64)
    line(f"TOP {TOP_POSITIONS} POSITIONS TO REVIEW")
    line("-" * 64)
    for m in model["positions"]:
        clk = f"{m['clock_seconds']:.0f}s" if m["clock_seconds"] is not None else "?"
        line(f"  -{m['cp_loss']:>4}cp  {m['date']}  move {m['move_number']:<3} "
             f"played {m['played']:<7} best {m['best']:<7} clock {clk:>5}  "
             f"({m['occurrences']} times)")
        line(f"            {m['category']}")
        line(f"            {m['game_url']}")
    line()
    line("=" * 64)
    return "\n".join(out)
