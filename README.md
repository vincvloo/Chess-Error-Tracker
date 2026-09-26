# Chess Mistake Coach

[![CI](https://github.com/vincvloo/Chess-Mistake-Coach/actions/workflows/ci.yml/badge.svg)](https://github.com/vincvloo/Chess-Mistake-Coach/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**Find the mistakes you keep making, then train them away.**

Reviewing one game tells you what went wrong in that game. Chess Mistake Coach
tells you what goes wrong in *your* chess, and then helps you fix it. It pulls
your Chess.com history, runs Stockfish over every position where it was your
move, sorts each significant error into a type ("left a piece undefended",
"missed a forcing check", ...) and shows which ones keep happening. Then it turns
your own mistakes into training: retry them, solve puzzles, and play a bot that
rates your moves as you go.

It runs entirely on your computer. Your games and progress are stored in a local
file and never leave it.

![Dashboard preview, with synthetic sample data](docs/dashboard-preview.png)

*The dashboard, shown here with made-up sample data rather than a real account.*

---

## What you can do

| | |
|---|---|
| **Dashboard** | Your mistakes ranked by type, when they happen (opening, middlegame, endgame, time pressure), which are shrinking and which are stuck, and the positions most worth reviewing. Filter by player, phase, time control, severity and mistake type. |
| **Practice** | Replay your own stored mistakes on a board. Pick a type to work on, try the move again, ask for a hint. After a miss the best move is drawn on the board (green, yours in red or amber) with a one-line reminder of what went wrong in your game. |
| **Play** | A full game against Stockfish or Maia at the strength you choose, optionally steered toward the kind of position you struggle with. **Coach me as I play** (on by default) rates each of your moves as you make it, shows the engine's best move and what kind of mistake it was, and lets you take a move back and try again. "Analyze this game" scores the finished game like your real ones. |
| **Puzzles** | Lichess puzzles from a local copy of their public database. Filter by rating and theme; a wrong answer shows the right move as an arrow. Includes a **daily puzzle** and a timed **puzzle rush**. |
| **Achievements** | Your streak, skill rating (overall and by theme), badges, and how each kind of mistake has moved over time. |
| **Leaderboard** | Every player tracked on your computer, side by side. Purely local. |
| **Backup** | One-click backup and restore of everything you've built up. |

---

## Quick install

One script does the whole setup (Python environment, the app, Stockfish) and then
starts the app. Running it again later just starts the app.

- **Windows:** double-click `install.cmd`, or run `powershell -ExecutionPolicy Bypass -File install.ps1`
- **macOS / Linux:** `bash install.sh`

It needs Python 3.10 or newer already installed. Add `-NoLaunch` (Windows) or
`--no-launch` (macOS / Linux) to set things up without starting the app.

The first time you open the app it asks for your Chess.com username (the last part
of your profile URL, `chess.com/member/YOUR_USERNAME`; not your Google email) and a
contact email. Chess.com requires a contact address on requests, and it's never used
to log in or sent anywhere else. The app then analyses your 100 most recent games so
you can start quickly; press **Update** any time to fetch and analyse more in the
background. Progress is saved game by game, so you can stop whenever you like.

<details>
<summary>Manual setup instead</summary>

```
python -m venv .venvs/chess
.venvs/chess/Scripts/activate          # macOS / Linux: source .venvs/chess/bin/activate
pip install -e ".[web]"
```

Install Stockfish (`winget install Stockfish` on Windows, `brew install stockfish`
on macOS, `sudo apt install stockfish` on Debian and Ubuntu), or download it from
stockfishchess.org. The app finds it automatically; see
[Where the engine is found](#where-the-engine-is-found). Then:

```
chess-mistake-coach serve
```

This opens a browser window pointed at the app (in "app mode", with no address bar, if
Chrome or Edge is found). Add `--no-browser` to just start the server and open the URL
yourself, or `--port` if 8000 is taken. `--db` and `--engine` work as they do on the
command line.

The plain `pip install -e .` (without `[web]`) gives you only the command-line tool.
</details>

---

## Skill rating, streaks and badges

Practising or solving a puzzle keeps a **daily streak** going. You get one automatic
**streak freeze** per week, which bridges a single missed day. Days are counted in your
computer's local time.

Your **skill rating** has an overall number and a number per theme (fork, pin, endgame, ...):

- **Puzzles** move it live, Elo-style: beating a harder puzzle moves it more. A puzzle
  tagged "fork pin" updates your overall, fork and pin ratings.
- **Your analysed games** move it based on how cleanly you played (average centipawn loss
  per move), not on whether you won. They feed the overall rating and the opening /
  middlegame / endgame ratings. Tactical themes stay puzzle-only.
- **Bot games** move it once, when you press "Analyze this game". A game where you took
  moves back doesn't count.

The game part is a heuristic estimate, not a true Elo. It is rebuilt from scratch in date
order after every analysis run, so fetching old games or re-analysing can't skew it. On the
Puzzles page the rating range is pre-filled from your skill rating; whatever you type in
still wins.

**Badges** (streak lengths, puzzles solved, hint-free practice, rating milestones) are
awarded when you earn them and kept permanently.

---

## Understanding your results

**Recurring error types.** The ranked list of what actually goes wrong. This is the part
that should change how you train. If "left a piece undefended" is 40% of your serious
errors, no amount of opening study will help you.

**When they happen.** Split by phase and by move number. Errors clustered in moves 1 to 10
point at opening preparation. Errors after move 30 usually point at fatigue or the clock,
not knowledge.

**Time pressure.** Errors bucketed by seconds remaining. If most of your damage lands under
30 seconds, the problem is time management, and tactics puzzles won't fix it.

**Trend.** Serious errors per 100 moves by month, plus a comparison of the first half of
the period against the second, by category. This shows which weakness is genuinely
shrinking and which one is stuck, which a single run cannot tell you.

**By colour and time control.** A gap between white and black is a repertoire problem. A
gap between blitz and rapid is a speed problem.

**Openings you play often** (three or more games, ranked by serious errors per game) and
the **top positions to review** (the biggest evaluation swings, with your move, the
engine's choice, your clock and a link to the game).

---

## Command line

Everything the app does with your games is also available without a browser:

```
chess-mistake-coach --user YOURNAME --email you@example.com          # fetch, analyse, report
chess-mistake-coach --user YOURNAME --report-only                    # report only, no network or engine
chess-mistake-coach --user YOURNAME --report-only --last-days 90
chess-mistake-coach --user YOURNAME --email you@example.com --since 2026-01 --time-class blitz --limit 50
```

Only games it hasn't seen get analysed; a routine re-run makes two HTTP requests plus
whatever new games you've played. You can interrupt with Ctrl+C and everything analysed
so far stays saved.

| Flag | Purpose |
|---|---|
| `--user` | Chess.com username. Comma-separate for several: `--user me,rival1`. Required. |
| `--email` | Contact address for the User-Agent. Required unless `--report-only`. |
| `--list-users` | Show every user in the database with sample size and error rate, then exit. |
| `--compare` | Side-by-side comparison instead of per-user reports. Needs two or more users. |
| `--db` | Database location. Defaults to `chess_tracker.db` next to the script. |
| `--engine` | Path to Stockfish. Auto-detected if omitted. |
| `--depth` | Search depth. Default 14. See below. |
| `--since` | Earliest month, format `YYYY-MM`. |
| `--time-class` | `bullet`, `blitz`, `rapid` or `daily`. |
| `--limit` | Cap on new games analysed this run. |
| `--min-loss` | Centipawn loss before a move counts as an error. Default 50. |
| `--threads` | Engine threads. Default 2. |
| `--pause` | Seconds between HTTP requests. Default 0.6. |
| `--report-only` | Report from the database. No network, no engine. |
| `--last-days` | Restrict the report to recent games. |
| `--export` | Write all stored mistakes to a CSV. |
| `--export-html` | Write the interactive dashboard to an HTML file. Report-only, no network. |
| `--config` | Path to a JSON file of defaults for the options above. |
| `--quiet` / `--verbose` | Less or more output. |

Other commands: `serve` (the web app), and `backup` / `restore` (see [Backing up](#backing-up)).

### Config file

If you always pass the same `--email`, `--depth`, `--threads` etc., put them in a JSON file
instead. By default the tool looks for `~/.chess-mistake-coach.json`; pass
`--config path/to/file.json` for a different one.

```json
{ "email": "you@example.com", "depth": 18, "threads": 4, "pause": 0.8 }
```

Any of `email`, `db`, `engine`, `depth`, `threads`, `pause`, `min_loss`, `time_class` can go
in the file. A flag on the command line wins over the file, which wins over the built-in
default. `--user` stays a required, per-run flag.

### Choosing a depth

Depth 12 is fast and fine for spotting hung pieces. Depth 14 is the default and the right
trade-off for most purposes. Depth 18 or 20 is several times slower and only worth it if
you're studying specific positions closely. You can deepen later without losing anything:
re-running with a higher `--depth` re-analyses the affected games and cleanly replaces the
old rows.

### Where the engine is found

In order: the `CHESS_ENGINE` environment variable, then `PATH`, then the standard install
locations for your platform. To pin it permanently on Windows:

```
setx CHESS_ENGINE "C:\Tools\stockfish\stockfish-windows-x86-64-avx2.exe"
```

---

## Your data

Everything lives in one SQLite file, `chess_tracker.db` next to the script by default
(the name is kept from earlier versions so existing data keeps working).

| Table | Contents |
|---|---|
| `games` | One row per analysed game **per tracked user**, keyed on (url, username). |
| `mistakes` | One row per error: move number, category, severity, centipawn loss, clock, FEN. |
| `archives` | Raw monthly PGN payloads as fetched. |
| `runs` | Audit log of every run: timestamp, requests made, games added. |
| `settings` | Web app only: which player is "you", plus fetch settings. |
| `practice_attempts`, `puzzle_attempts` | One row per practice move / finished puzzle. |
| `puzzles`, `puzzle_source_stats` | The locally imported Lichess puzzles, and how many exist in the full database. |
| `user_streaks`, `user_puzzle_ratings`, `user_game_ratings`, `badges_earned`, `daily_puzzles`, `puzzle_rush_scores` | Streaks, skill ratings, badges, the daily puzzle and rush scores. |
| `position_evals` | The engine's verdict on opening positions, remembered so the same position isn't analysed again for every game that reaches it. Only a speed-up; safe to delete. |

Everything is scoped by username, so one database can hold any number of people without
their data mixing. The FEN column means every stored mistake can be pasted straight into a
board, and any SQL client will open the file:

```sql
-- your worst categories under time pressure
SELECT category, COUNT(*) FROM mistakes
WHERE username = 'yourname' AND clock_seconds < 30
GROUP BY category ORDER BY 2 DESC;
```

`--export mistakes.csv` dumps the same data for a spreadsheet.

### Backing up

```
chess-mistake-coach backup                 # saves to a backups/ folder next to the database
chess-mistake-coach backup --to mine.db    # or anywhere you like
chess-mistake-coach restore mine.db        # asks first, and keeps a safety copy of what it replaces
```

The same is on the app's Settings page (Download backup, Restore). A backup is an ordinary
SQLite file holding your analysed games, mistakes, practice and puzzle history, streaks,
ratings, badges and settings (so it includes your email). It leaves out the downloaded
Chess.com games and the Lichess puzzle library, which can be fetched again and make up most
of the database; add `--full` (or "Download everything") to include them. For reference, a
database of 42,000 games is about 380 MB and its data-only backup about 140 MB.

Backing up is safe while the app is running. Restoring while an update is running is
refused; from the command line, close the app first.

On Windows, if your user folder is redirected to OneDrive, put the database somewhere local
with `--db`. SQLite and cloud sync don't mix.

---

## Chess.com and API safety

Your Chess.com account is never at risk, because the tool never authenticates. It uses the
public Published-Data API: read-only, no key, no login. Chess.com's stated policy is that
serial access is unlimited and parallel requests are what trigger a 429, so the tool is
built to stay far below any threshold:

- Strictly serial, one request at a time, with a pause between them
- Past monthly archives are stored permanently and never requested again
- The current month is revalidated with an ETag, so an unchanged month costs a 304 with no payload
- A 429 triggers exponential backoff and honours `Retry-After`

The first run makes one request per month of your history; every run after that makes two.
The contact email matters because with a recognisable User-Agent Chess.com will try to
reach you before blocking anything, and a block would apply to the client, not your account.

### Tracking several people

Add friends or rivals from the app's home page, or with `--user me,rival1,rival2`. Each
person is fetched and analysed in turn, and one person's history is never mixed into
another's. A game between two tracked people is stored once for each of them, scored from
each player's own side.

`--compare` puts them side by side. Rates are per 100 of that player's own moves, so
unequal sample sizes stay comparable, and categories are ordered by the size of the gap.
Read the gaps, not the totals: two players with the same overall error rate can have
completely different problems. The useful hypothesis to test against a stronger player is
that they make a similar number of errors but far fewer catastrophic ones, which turns
"make fewer mistakes" into "stop making the expensive ones". Compare like with like (for
example `--time-class blitz`).

Public and reasonable to use are not the same thing. Benchmarking against a few stronger
players is fine. Publishing a named person's weakness profile, sending it to them
unsolicited, or pointing this at hundreds of accounts is not.

---

## Troubleshooting

**403 from Chess.com.** The User-Agent was rejected. Give a real address as the contact email.

**404, no such user.** Wrong username. Check the end of your profile URL. A closed or
restricted profile can also produce this.

**Stockfish not found.** Pass `--engine` with the full path or set `CHESS_ENGINE`. The error
message lists install commands per platform.

**`pip install chess` grabbed the wrong package.** There is an abandoned package of a
similar name on PyPI. Inside the virtual environment, `pip uninstall chess` then
`pip install chess` again, and check with `python -c "import chess; print(chess.__version__)"`
(it should be 1.11 or later).

**Analysis is slow.** Lower `--depth` to 12, raise `--threads`, and use `--limit` to work
through your history in batches. Progress is saved per game.

**429 responses.** The tool backs off on its own. If it keeps happening, raise `--pause`.

---

## Limitations worth knowing

The classifier is a set of heuristics on top of engine output. "Positional or planning
error" is partly a catch-all for mistakes it couldn't name specifically. Treat the named
categories as reliable and that one as a residual.

Blame attribution is imperfect. When a bad plan takes three moves to go wrong, the engine
often flags the last move in the sequence rather than the first.

Sample size matters. Fifty games is enough to see your top two or three error types, but not
to trust the opening breakdown or small differences between categories. Those need a few
hundred games, which is what the persistent database is for.

The skill rating is an estimate, and the engine's judgement is not a training plan.
Stockfish will tell you a move loses 60 centipawns; it won't tell you that at your level
that mistake is irrelevant next to the piece you hung on move 12. Read the frequency
rankings, act on the top two, ignore the rest.

---

## For developers

The code is a small package, `chess_mistake_coach/`, split along its natural seams:

| Module | Responsibility |
|---|---|
| `engine.py` | Locates a Stockfish binary on the current platform. |
| `db.py` | The SQLite schema and saving/loading data. |
| `chesscom.py` | The Chess.com API client: serial, cached, conditional requests. |
| `analysis.py` | Stockfish analysis and mistake classification. |
| `analysis_runner.py` | Orchestrates a fetch-and-analyse run, shared by the CLI and the app's background jobs. |
| `position_cache.py` | Remembers the engine's verdict on opening positions between games and runs. |
| `reports.py`, `html_export.py` | Text reports and the interactive dashboard. |
| `coaching.py` | Move ratings and the plain-language explanation of each mistake type. |
| `bot.py` | Play mode's opponent: move choice at a target Elo. |
| `puzzles.py` | The Lichess puzzle database: import, random pick, answer checking. |
| `gamification.py` | Streaks, skill ratings, badges, daily puzzle, puzzle rush and the leaderboard. |
| `backup.py` | Backup and restore. |
| `cli.py` | Command-line entry point (dispatches to `web/` for `serve`). |
| `web/` | The local web app: pages, API, background jobs, and templates (one shared chessboard template for Practice, Play and Puzzles). |

To run the tests:

```
pip install -e ".[dev,web]"
pytest
```

The suite covers the pure logic (classification, ratings, streaks, backup, the position
cache), the Chess.com client's error handling with `requests` mocked, and the web app's
routes and job manager with FastAPI's `TestClient`. It needs neither Stockfish nor network
access, and CI runs it on every push and pull request. Install both extras together as shown:
several test files import FastAPI at module level and fail to collect without the `web` extra.
