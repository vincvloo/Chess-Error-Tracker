import threading
from unittest.mock import patch

import chess
import pytest
import zstandard

from chess_tracker.db import open_db
from chess_tracker.puzzles import (COMMON_THEME_LABELS, COMMON_THEMES, THEME_GROUPS,
                                   PuzzleImportCancelled, check_puzzle_move,
                                   download_puzzle_source, find_puzzle_source, humanize_theme,
                                   import_puzzles, parse_puzzle_row)

_HEADER = "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags\n"

# A real mate-in-1: black plays an irrelevant pawn move, white delivers Re8#.
_BACK_RANK_MATE = (
    "aaaaa,6k1/p4ppp/8/8/8/8/5PPP/4R1K1 b - - 0 1,a7a6 e1e8,900,80,90,5000,"
    "mateIn1 backRankMate,https://lichess.org/abc,\n"
)
# A real mate-in-1: Scholar's-mate-style Qxf7#, tagged differently/rated higher.
_SCHOLARS_MATE = (
    "bbbbb,r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR b KQkq - 2 3,"
    "g8f6 f3f7,1200,70,85,3000,mateIn1 fork,https://lichess.org/def,Italian_Game\n"
)
_ENDGAME_PUZZLE = (
    "ccccc,8/8/8/8/8/8/6k1/6K1 w - - 0 1,g1f1 g2g3,2500,60,70,1000,"
    "endgame,https://lichess.org/ghi,\n"
)


def _write_csv(tmp_path, *rows):
    path = tmp_path / "puzzles.csv"
    path.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return str(path)


def test_parse_puzzle_row_pads_themes_for_safe_like_matching():
    row = {"PuzzleId": "x", "FEN": "fen", "Moves": "a1a2 a2a3", "Rating": "1500",
           "RatingDeviation": "70", "Popularity": "90", "NbPlays": "100",
           "Themes": "fork pin", "GameUrl": "", "OpeningTags": ""}
    puzzle = parse_puzzle_row(row)
    assert puzzle["themes"] == " fork pin "
    assert puzzle["rating"] == 1500  # cast to int, not left as string


def test_import_puzzles_stores_all_rows(tmp_path):
    conn = open_db(":memory:")
    csv_path = _write_csv(tmp_path, _BACK_RANK_MATE, _SCHOLARS_MATE, _ENDGAME_PUZZLE)
    stats = import_puzzles(conn, csv_path)
    assert stats.scanned == 3
    assert stats.imported == 3
    ids = {r["puzzle_id"] for r in conn.execute("SELECT puzzle_id FROM puzzles").fetchall()}
    assert ids == {"aaaaa", "bbbbb", "ccccc"}


def test_import_puzzles_filters_by_rating_and_theme(tmp_path):
    conn = open_db(":memory:")
    csv_path = _write_csv(tmp_path, _BACK_RANK_MATE, _SCHOLARS_MATE, _ENDGAME_PUZZLE)
    stats = import_puzzles(conn, csv_path, min_rating=1000, max_rating=2000, themes=["fork"])
    assert stats.scanned == 3
    assert stats.imported == 1
    ids = [r["puzzle_id"] for r in conn.execute("SELECT puzzle_id FROM puzzles").fetchall()]
    assert ids == ["bbbbb"]


def test_import_puzzles_records_source_stats_beyond_the_import_filter(tmp_path):
    """The whole point of puzzle_source_stats: even a narrow import should
    still record the TRUE total across the whole source file, not just
    what got imported -- see the phase plan's "count the total, even not
    downloaded" requirement."""
    conn = open_db(":memory:")
    csv_path = _write_csv(tmp_path, _BACK_RANK_MATE, _SCHOLARS_MATE, _ENDGAME_PUZZLE)
    import_puzzles(conn, csv_path, min_rating=1000, max_rating=2000, themes=["fork"])

    total = conn.execute(
        "SELECT total_count FROM puzzle_source_stats WHERE bucket_key = 'total'").fetchone()
    assert total["total_count"] == 3

    mate_theme = conn.execute(
        "SELECT total_count FROM puzzle_source_stats WHERE bucket_key = 'theme:mateIn1'").fetchone()
    assert mate_theme["total_count"] == 2  # both mate puzzles, even though only 1 was imported

    endgame_bucket = conn.execute(
        "SELECT total_count FROM puzzle_source_stats WHERE bucket_key = 'rating:2500-2600'").fetchone()
    assert endgame_bucket["total_count"] == 1


def test_import_puzzles_respects_limit_but_keeps_scanning_for_stats(tmp_path):
    conn = open_db(":memory:")
    csv_path = _write_csv(tmp_path, _BACK_RANK_MATE, _SCHOLARS_MATE, _ENDGAME_PUZZLE)
    stats = import_puzzles(conn, csv_path, limit=1)
    assert stats.scanned == 3
    assert stats.imported == 1
    total = conn.execute(
        "SELECT total_count FROM puzzle_source_stats WHERE bucket_key = 'total'").fetchone()
    assert total["total_count"] == 3


def test_import_puzzles_is_idempotent_on_rerun(tmp_path):
    conn = open_db(":memory:")
    csv_path = _write_csv(tmp_path, _BACK_RANK_MATE)
    import_puzzles(conn, csv_path)
    import_puzzles(conn, csv_path)  # re-import/top-up should not duplicate
    count = conn.execute("SELECT COUNT(*) AS n FROM puzzles").fetchone()["n"]
    assert count == 1


def test_check_puzzle_move_matches_the_solver_move():
    moves = "a7a6 e1e8"
    assert check_puzzle_move(moves, 1, chess.Move.from_uci("e1e8")) is True


def test_check_puzzle_move_rejects_a_wrong_move():
    moves = "a7a6 e1e8"
    assert check_puzzle_move(moves, 1, chess.Move.from_uci("e1e2")) is False


def test_check_puzzle_move_rejects_an_index_past_the_end():
    moves = "a7a6 e1e8"
    assert check_puzzle_move(moves, 5, chess.Move.from_uci("e1e8")) is False


class _FakeStreamedResponse:
    """A minimal stand-in for requests.get(..., stream=True)'s return value,
    wrapping already-zstd-compressed bytes chunked the same way a real HTTP
    response body would arrive."""

    def __init__(self, compressed: bytes, chunk_size: int = 64):
        self._compressed = compressed
        self._chunk_size = chunk_size
        self.headers = {"content-length": str(len(compressed))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self._compressed), self._chunk_size):
            yield self._compressed[i:i + self._chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_puzzle_source_decompresses_the_stream_correctly(tmp_path):
    original = b"PuzzleId,FEN,Moves\naaaaa,fen-1,e1e8\nbbbbb,fen-2,f3f7\n"
    compressed = zstandard.ZstdCompressor().compress(original)
    dest = str(tmp_path / "puzzles.csv")
    progress_calls = []

    with patch("chess_tracker.puzzles.requests.get",
              return_value=_FakeStreamedResponse(compressed)):
        result_path = download_puzzle_source(
            dest_path=dest, progress_cb=lambda d, t: progress_calls.append((d, t)))

    assert result_path == dest
    with open(dest, "rb") as f:
        assert f.read() == original
    assert progress_calls  # at least one progress callback fired
    assert progress_calls[-1] == (len(compressed), len(compressed))


def test_download_puzzle_source_leaves_no_partial_file_name_as_the_result(tmp_path):
    """The .part temp file should be renamed to the real dest, never left
    behind under its temp name."""
    original = b"PuzzleId,FEN,Moves\naaaaa,fen-1,e1e8\n"
    compressed = zstandard.ZstdCompressor().compress(original)
    dest = str(tmp_path / "puzzles.csv")

    with patch("chess_tracker.puzzles.requests.get",
              return_value=_FakeStreamedResponse(compressed)):
        download_puzzle_source(dest_path=dest)

    import os
    assert os.path.isfile(dest)
    assert not os.path.isfile(dest + ".part")


def test_find_puzzle_source_prefers_env_var_override(tmp_path, monkeypatch):
    csv_path = tmp_path / "custom.csv"
    csv_path.write_text("PuzzleId,FEN,Moves\n", encoding="utf-8")
    monkeypatch.setenv("CHESS_PUZZLE_SOURCE", str(csv_path))
    assert find_puzzle_source() == str(csv_path)


def test_find_puzzle_source_returns_none_when_nothing_is_cached(monkeypatch):
    monkeypatch.delenv("CHESS_PUZZLE_SOURCE", raising=False)
    monkeypatch.setattr("chess_tracker.puzzles._DEFAULT_SOURCE_PATH",
                        "/definitely/does/not/exist.csv")
    assert find_puzzle_source() is None


def test_download_puzzle_source_raises_when_cancelled_mid_stream(tmp_path):
    original = b"x" * 10_000
    compressed = zstandard.ZstdCompressor().compress(original)
    already_cancelled = threading.Event()
    already_cancelled.set()

    with patch("chess_tracker.puzzles.requests.get",
              return_value=_FakeStreamedResponse(compressed, chunk_size=64)):
        with pytest.raises(PuzzleImportCancelled):
            download_puzzle_source(dest_path=str(tmp_path / "unused.csv"),
                                   cancel_event=already_cancelled)


def test_download_puzzle_source_accepts_a_bare_relative_filename(tmp_path, monkeypatch):
    """dest_path with no directory component (just a filename) shouldn't
    crash os.makedirs("") -- regression check for that exact bug."""
    monkeypatch.chdir(tmp_path)
    original = b"hello"
    compressed = zstandard.ZstdCompressor().compress(original)
    with patch("chess_tracker.puzzles.requests.get",
              return_value=_FakeStreamedResponse(compressed)):
        result = download_puzzle_source(dest_path="bare.csv")
    assert result == "bare.csv"
    assert (tmp_path / "bare.csv").read_bytes() == original


def test_import_puzzles_raises_when_cancelled_but_keeps_progress_made(tmp_path):
    # Needs to cross one _INSERT_BATCH_SIZE (500) boundary for the
    # cancel_event check (checked once per batch flush, not every row) to
    # actually run before the scan finishes.
    rows = [f"id{i},fen{i},e1e{2 + (i % 6)},1000,70,80,100,mateIn1,,\n" for i in range(600)]
    csv_path = _write_csv(tmp_path, *rows)

    conn = open_db(":memory:")
    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(PuzzleImportCancelled):
        import_puzzles(conn, csv_path, cancel_event=cancel_event)

    # The first batch (500 rows) should have been committed before the
    # cancellation was raised -- progress isn't thrown away.
    count = conn.execute("SELECT COUNT(*) AS n FROM puzzles").fetchone()["n"]
    assert count == 500


def test_theme_groups_and_common_themes_stay_in_sync():
    # COMMON_THEME_LABELS/COMMON_THEMES are derived from THEME_GROUPS --
    # this pins that derivation rather than the two drifting apart.
    all_codes = [code for _, items in THEME_GROUPS for code, _ in items]
    assert set(COMMON_THEME_LABELS.keys()) == set(all_codes)
    assert set(COMMON_THEMES) == set(all_codes)


def test_humanize_theme_uses_the_curated_label_when_known():
    assert humanize_theme("mateIn2") == "Mate in 2"
    assert humanize_theme("discoveredAttack") == "Discovered attack"


def test_humanize_theme_falls_back_to_a_camelcase_split_for_unknown_codes():
    # Real Lichess puzzles carry themes well outside the curated picker
    # subset (e.g. "veryLong", "rookEndgame") -- these still need a readable
    # fallback, not a raw camelCase code shown verbatim.
    assert humanize_theme("veryLong") == "Very long"
    assert humanize_theme("rookEndgame") == "Rook endgame"
    assert humanize_theme("xRayAttack") == "X ray attack"
