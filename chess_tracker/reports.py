"""Reporting and comparison, straight from the database."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict

from .analysis import PHASES


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


def report(conn: sqlite3.Connection, user: str, time_class: str | None = None,
           last_days: int | None = None, phase: str | None = None) -> str:
    u = user.lower()
    games_where, games_params, mistakes_where, mistakes_params = _scope(
        u, time_class, phase, last_days)

    games = conn.execute(f"SELECT * FROM games {games_where} ORDER BY end_time",
                         games_params).fetchall()
    if not games:
        return "No games stored yet for that filter."

    serious = conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} AND severity IN ('mistake','blunder')",
        mistakes_params).fetchall()

    out: list[str] = []

    def line(s: str = "") -> None:
        out.append(s)

    moves_col = f"{phase}_moves" if phase else "moves_played"
    total_moves = sum(g[moves_col] or 0 for g in games)
    not_backfilled = phase and sum(1 for g in games if g[moves_col] is None)
    ratings = [g["my_rating"] for g in games if g["my_rating"]]
    n_serious = len(serious)
    moves_label = f"your {phase} moves" if phase else "your moves"

    filters = ", ".join(filter(None, [time_class, phase]))
    line("=" * 64)
    line(f"CHESS ERROR PROFILE  |  {user}" + (f"  [{filters}]" if filters else ""))
    line("=" * 64)
    line(f"Games in store     : {len(games)}")
    line(f"Your moves         : {total_moves}" + (f"  ({phase})" if phase else ""))
    line(f"Date range         : {games[0]['date']} to {games[-1]['date']}")
    if ratings:
        line(f"Rating             : {ratings[0]} then, {ratings[-1]} now "
             f"({ratings[-1] - ratings[0]:+d})")
    line(f"Mistakes + blunders: {n_serious}  "
         f"({n_serious / max(total_moves, 1) * 100:.1f}% of {moves_label})")
    if not_backfilled:
        line(f"NOTE: {not_backfilled} game(s) have no per-phase move counts yet "
             f"(re-run without --report-only to backfill) -- they are excluded above.")
    line()

    line("-" * 64)
    line("RECURRING ERROR TYPES")
    line("-" * 64)
    for cat, n in Counter(m["category"] for m in serious).most_common():
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {cat}")
    line()

    if not phase:
        line("-" * 64)
        line("WHEN THEY HAPPEN")
        line("-" * 64)
        phases = Counter(m["phase"] for m in serious)
        for ph in PHASES:
            n = phases.get(ph, 0)
            line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  {ph}")
        line()

    buckets = ["1-10", "11-20", "21-30", "31-40", "41+"]

    def bucket(mv: int) -> str:
        return (buckets[0] if mv <= 10 else buckets[1] if mv <= 20
                else buckets[2] if mv <= 30 else buckets[3] if mv <= 40 else buckets[4])

    by_move = Counter(bucket(m["move_number"]) for m in serious)
    line("By move number:")
    for b in buckets:
        n = by_move.get(b, 0)
        line(f"{n:5d}  {n / max(n_serious,1) * 100:5.1f}%  {bar(n, n_serious)}  moves {b}")
    line()

    clocked = [m for m in serious if m["clock_seconds"] is not None]
    if clocked:
        line("-" * 64)
        line("TIME PRESSURE")
        line("-" * 64)
        groups = (("under 30s left", [m for m in clocked if m["clock_seconds"] < 30]),
                  ("30 to 60s left", [m for m in clocked if 30 <= m["clock_seconds"] < 60]),
                  ("over 60s left", [m for m in clocked if m["clock_seconds"] >= 60]))
        for label, grp in groups:
            n = len(grp)
            line(f"{n:5d}  {n / len(clocked) * 100:5.1f}%  {bar(n, len(clocked))}  {label}")
        if len(groups[0][1]) / len(clocked) > 0.30:
            line()
            line(">> Most of your damage happens on a low clock. That is a time")
            line("   management problem, not a chess knowledge problem.")
        line()

    # ---- the whole point of persisting: movement over time -----------------
    monthly: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        monthly[g["date"][:7]][1] += g[moves_col] or 0
    for m in serious:
        monthly[m["date"][:7]][0] += 1

    if len(monthly) >= 2:
        line("-" * 64)
        line(f"TREND  (serious errors per 100 of {moves_label})")
        line("-" * 64)
        rows = sorted(monthly.items())
        rates = [(mo, e / mv * 100 if mv else 0.0, mv) for mo, (e, mv) in rows]
        peak = max((r[1] for r in rates), default=0) or 1
        for mo, rate, mv in rates:
            line(f"  {mo}  {rate:5.1f}  {'#' * round(30 * rate / peak)}  ({mv} moves)")
        first, last = rates[0][1], rates[-1][1]
        line()
        line(f"  {rates[0][0]} to {rates[-1][0]}: {first:.1f} -> {last:.1f} "
             f"({'improving' if last < first else 'getting worse'})")
        line()

        half = max(len(rows) // 2, 1)
        early_months = {mo for mo, _ in rows[:half]}
        early_moves = sum(mv for _, (_, mv) in rows[:half])
        late_moves = sum(mv for _, (_, mv) in rows[half:])
        early_c: Counter = Counter()
        late_c: Counter = Counter()
        for m in serious:
            (early_c if m["date"][:7] in early_months else late_c)[m["category"]] += 1
        if early_moves and late_moves:
            line("Per 100 moves, first half of the period vs second half:")
            deltas = []
            for c in set(early_c) | set(late_c):
                a = early_c[c] / early_moves * 100
                b = late_c[c] / late_moves * 100
                deltas.append((b - a, a, b, c))
            for d, a, b, c in sorted(deltas, key=lambda x: -abs(x[0]))[:6]:
                arrow = "worse " if d > 0.05 else "better" if d < -0.05 else "flat  "
                line(f"  {a:5.2f} -> {b:5.2f}  {arrow}  {c}")
            line()

    line("-" * 64)
    line("BY COLOUR AND TIME CONTROL")
    line("-" * 64)
    for key in ("my_colour", "time_class"):
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for g in games:
            agg[g[key]][1] += g[moves_col] or 0
        for m in serious:
            agg[m[key]][0] += 1
        for k, (e, mv) in sorted(agg.items()):
            if mv:
                line(f"  {k:<10} {e / mv * 100:5.2f} errors per 100 moves  ({mv} moves)")
    line()

    eco: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in games:
        if g["eco"] != "?":
            eco[g["eco"]][1] += 1
    url_to_eco = {g["url"]: g["eco"] for g in games}
    for m in serious:
        e = url_to_eco.get(m["game_url"])
        if e and e != "?":
            eco[e][0] += 1
    frequent = {k: v for k, v in eco.items() if v[1] >= 3}
    if frequent:
        line("-" * 64)
        line("OPENINGS YOU PLAY OFTEN")
        line("-" * 64)
        for k, (e, n) in sorted(frequent.items(), key=lambda kv: -kv[1][0] / kv[1][1])[:8]:
            line(f"  {k}   {n:3d} games   {e / n:.1f} serious errors per game")
        line()

    line("-" * 64)
    line("TOP 10 POSITIONS TO REVIEW")
    line("-" * 64)
    top = conn.execute(
        f"SELECT * FROM mistakes {mistakes_where} AND severity IN ('mistake','blunder') "
        f"ORDER BY cp_loss DESC LIMIT 10", mistakes_params).fetchall()
    for m in top:
        clk = f"{m['clock_seconds']:.0f}s" if m["clock_seconds"] is not None else "?"
        line(f"  -{m['cp_loss']:>4}cp  {m['date']}  move {m['move_number']:<3} "
             f"played {m['played']:<7} best {m['best']:<7} clock {clk:>5}")
        line(f"            {m['category']}")
        line(f"            {m['game_url']}")
    line()
    line("=" * 64)
    return "\n".join(out)
