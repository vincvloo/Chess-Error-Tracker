import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from chess_tracker.chesscom import ChessComClient, ChessComError


def _client() -> ChessComClient:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE archives (
            url TEXT PRIMARY KEY, username TEXT, month TEXT, etag TEXT,
            last_modified TEXT, body TEXT, game_count INTEGER,
            fetched_at TEXT, complete INTEGER DEFAULT 0
        );
    """)
    return ChessComClient("test@example.com", conn, pause=0)


def test_archives_raises_chesscomerror_on_404():
    client = _client()
    response = MagicMock(status_code=404)
    with patch("chess_tracker.chesscom.requests.get", return_value=response):
        with pytest.raises(ChessComError, match="No such Chess.com user"):
            client.archives("nosuchuser")


def test_archives_raises_chesscomerror_on_403():
    client = _client()
    response = MagicMock(status_code=403)
    with patch("chess_tracker.chesscom.requests.get", return_value=response):
        with pytest.raises(ChessComError, match="403"):
            client.archives("someuser")


def test_archives_returns_list_on_success():
    client = _client()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "archives": ["https://api.chess.com/pub/player/someuser/games/2024/01"]
    }
    with patch("chess_tracker.chesscom.requests.get", return_value=response):
        result = client.archives("someuser")
    assert result == ["https://api.chess.com/pub/player/someuser/games/2024/01"]
