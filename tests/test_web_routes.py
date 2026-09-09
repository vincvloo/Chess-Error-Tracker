from fastapi.testclient import TestClient

from chess_tracker.db import open_db, save_game
from chess_tracker.web.app import create_app
from chess_tracker.web.jobs import JobAlreadyRunningError

REC = {
    "url": "https://example.com/g1", "username": "alice", "end_time": 1000,
    "date": "2024-01-01", "time_class": "blitz", "my_colour": "white",
    "my_rating": 1500, "opp_rating": 1400, "result": "win", "eco": "C00",
    "moves_played": 20, "opening_moves": 10, "middlegame_moves": 8, "endgame_moves": 2,
}
MISTAKE = {
    "game_url": REC["url"], "username": "alice", "date": REC["date"], "end_time": 1000,
    "time_class": "blitz", "my_rating": 1500, "my_colour": "white", "move_number": 5,
    "phase": "opening", "severity": "blunder", "cp_loss": 300, "category": "hung a pawn",
    "played": "e4", "best": "d4", "clock_seconds": 20.0, "fen": "fen-string",
}


def _seeded_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    conn = open_db(db_path)
    save_game(conn, REC, [MISTAKE], depth=14)
    conn.close()
    return db_path


def _empty_db(tmp_path) -> str:
    db_path = str(tmp_path / "empty.db")
    open_db(db_path).close()
    return db_path


def _fake_engine_path(tmp_path) -> str:
    path = tmp_path / "fake-stockfish"
    path.touch()
    return str(path)


def test_home_page_lists_tracked_users(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "alice" in r.text


def test_home_page_with_empty_db_shows_no_users_message(tmp_path):
    client = TestClient(create_app(_empty_db(tmp_path)))
    r = client.get("/")
    assert r.status_code == 200
    assert "No users tracked yet" in r.text


def test_dashboard_route_returns_html_for_known_user(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard", params={"users": "alice"})
    assert r.status_code == 200
    assert "Chess Mistake Explorer" in r.text
    assert "alice" in r.text


def test_dashboard_route_with_unknown_user_shows_placeholder(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard", params={"users": "nosuchuser"})
    assert r.status_code == 200
    assert "No stored games" in r.text


def test_dashboard_route_requires_users_param(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/dashboard")
    assert r.status_code == 400


def test_start_job_redirects_to_progress_page(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))
    monkeypatch.setattr(app.state.jobs, "start_job",
                        lambda *a, **k: type("S", (), {"id": "fake-job-id"})())
    client = TestClient(app, follow_redirects=False)
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/fake-job-id"


def test_start_job_requires_a_username(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "  ", "email": "you@example.com"})
    assert r.status_code == 400
    assert "Enter at least one" in r.text


def test_start_job_requires_email(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "bob", "email": ""})
    assert r.status_code == 400
    assert "Email is required" in r.text


def test_start_job_reports_missing_engine(tmp_path, monkeypatch):
    # no engine_path passed to create_app(), and find_engine() monkeypatched
    # to simulate a machine with no Stockfish installed
    monkeypatch.setattr("chess_tracker.web.routes_pages.find_engine", lambda: None)
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 400
    assert "Stockfish" in r.text


def test_start_job_rejects_when_already_running(tmp_path, monkeypatch):
    app = create_app(_seeded_db(tmp_path), engine_path=_fake_engine_path(tmp_path))

    def raise_running(*a, **k):
        raise JobAlreadyRunningError("busy")

    monkeypatch.setattr(app.state.jobs, "start_job", raise_running)
    client = TestClient(app)
    r = client.post("/jobs", data={"user": "bob", "email": "you@example.com"})
    assert r.status_code == 409


def test_job_status_endpoint_returns_404_for_unknown_job(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/api/jobs/nonexistent")
    assert r.status_code == 404


def test_job_cancel_endpoint_returns_404_for_unknown_job(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.post("/api/jobs/nonexistent/cancel")
    assert r.status_code == 404


def test_job_progress_page_404s_for_unknown_job(tmp_path):
    client = TestClient(create_app(_seeded_db(tmp_path)))
    r = client.get("/jobs/nonexistent")
    assert r.status_code == 404
