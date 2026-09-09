"""Chess.com API client: serial, cached, conditional."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

API = "https://api.chess.com/pub"


class ChessComError(Exception):
    """Raised for Chess.com API errors the caller should report and exit on."""


class ChessComClient:
    """
    Serial, cached, conditional. Chess.com states that serial access is
    unlimited and that parallel requests are what trigger 429s, so this makes
    exactly one request at a time and sleeps between them.
    """

    def __init__(self, email: str, conn: sqlite3.Connection, pause: float = 0.6):
        self.headers = {"User-Agent": f"chess-error-tracker/2.0 ({email})"}
        self.conn = conn
        self.pause = pause
        self.requests_made = 0

    def _get(self, url: str, extra: dict | None = None) -> requests.Response:
        headers = {**self.headers, **(extra or {})}
        last_exc: requests.exceptions.RequestException | None = None
        for attempt in range(5):
            time.sleep(self.pause)
            try:
                r = requests.get(url, headers=headers, timeout=60)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(f"\n  {exc.__class__.__name__}, retrying in {wait:.0f}s")
                time.sleep(wait)
                continue
            self.requests_made += 1
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                logger.warning(f"\n  429 received, backing off {wait:.0f}s")
                time.sleep(wait)
                continue
            logger.debug(f"GET {url} -> {r.status_code}")
            return r
        if last_exc is not None:
            raise RuntimeError(f"Gave up on {url} after repeated connection errors") from last_exc
        raise RuntimeError(f"Gave up on {url} after repeated 429s")

    def archives(self, user: str) -> list[str]:
        r = self._get(f"{API}/player/{user.lower()}/games/archives")
        if r.status_code == 404:
            raise ChessComError(f"No such Chess.com user: {user}")
        if r.status_code == 403:
            raise ChessComError(
                "403 from Chess.com. The User-Agent was rejected. Pass a real --email.")
        r.raise_for_status()
        return r.json().get("archives", [])

    def month(self, url: str, user: str, current_month: str) -> list[dict]:
        """
        Games for one monthly archive.

        Past months come straight from the database with no network call.
        The current month is revalidated with an ETag, so an unchanged month
        costs a 304 and no payload.
        """
        month = url[-7:].replace("/", "-")
        row = self.conn.execute("SELECT * FROM archives WHERE url = ?", (url,)).fetchone()

        if row and row["complete"] and row["body"]:
            return json.loads(row["body"])

        extra: dict[str, str] = {}
        if row and row["etag"]:
            extra["If-None-Match"] = row["etag"]
        elif row and row["last_modified"]:
            extra["If-Modified-Since"] = row["last_modified"]

        r = self._get(url, extra)

        if r.status_code == 304 and row and row["body"]:
            if month < current_month:
                with self.conn:
                    self.conn.execute("UPDATE archives SET complete = 1 WHERE url = ?", (url,))
            return json.loads(row["body"])

        r.raise_for_status()
        games = r.json().get("games", [])
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO archives
                (url, username, month, etag, last_modified, body, game_count,
                 fetched_at, complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (url, user.lower(), month, r.headers.get("ETag"),
                  r.headers.get("Last-Modified"), json.dumps(games), len(games),
                  datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  1 if month < current_month else 0))
        return games


def collect_games(client: ChessComClient, user: str, since: str | None,
                  time_class: str | None, limit: int | None) -> list[dict]:
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    archives = client.archives(user)
    if since:
        archives = [a for a in archives if a[-7:].replace("/", "-") >= since]

    games: list[dict] = []
    cached = fetched = 0
    for url in archives:
        before = client.requests_made
        month_games = client.month(url, user, current_month)
        if client.requests_made == before:
            cached += 1
        else:
            fetched += 1
        games.extend(month_games)

    logger.info(f"  {len(archives)} months: {cached} from local store, {fetched} fetched "
                f"({client.requests_made} HTTP requests this run)")

    if time_class:
        games = [g for g in games if g.get("time_class") == time_class]
    games = [g for g in games if g.get("rules") == "chess"]
    games.sort(key=lambda g: g.get("end_time", 0), reverse=True)
    return games[:limit] if limit else games
