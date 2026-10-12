"""Shared test setup.

The app is imported with no database, no real Discord and known passwords,
and every test starts from a clean slate. Nothing here touches the network or
the real database: Discord, the weather lookup, map images and (where a test
asks for it) the database are all replaced with fakes.
"""
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# Must be set before the app is imported (load_dotenv never overrides a value
# that's already set, so a local .env can't switch the database on here).
os.environ.update(
    DB_HOST="", DB_NAME="", DB_USERNAME="", DB_PASSWORD="",
    OVERLAND_ACCESS_TOKEN="test-token", ADMIN_PASSWORD="test-admin",
    DISCORD_BOT_TOKEN="", DISCORD_USER_ID="", DISCORD_TEST_MODE="false",
    SIX_SEVEN_DISCORD_ID="friend-id", RECOVER_ACTIVE_RUNS="",
    PUBLIC_SITE_URL="", RAILWAY_PUBLIC_DOMAIN="", SETTINGS_SCOPE="test",
)

import app as app_module  # noqa: E402
from split_tracker import MILE_METERS  # noqa: E402

DEFAULT_SETTINGS = dict(app_module.admin_state)
TOKEN = {"Authorization": "Bearer test-token"}
ADMIN_PASSWORD = "test-admin"
START = datetime(2026, 10, 12, 15, 0, 0, tzinfo=timezone.utc)
LAT0, LON0 = 36.64, -121.80
METERS_PER_DEG_LAT = 111320.0
METERS_PER_DEG_LON = METERS_PER_DEG_LAT * math.cos(math.radians(LAT0))


def wait_for_background():
    """Let DM/map/record threads started by the app finish."""
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=10)


@pytest.fixture(autouse=True)
def fresh_app(monkeypatch):
    """A clean slate for every test: no run, default settings, no records."""
    app_module.tracker.reset()
    app_module.admin_state.clear()
    app_module.admin_state.update(DEFAULT_SETTINGS)
    app_module.settings_state.update(loaded=False, changed=False, last_attempt=float("-inf"))
    app_module.last_summary = None
    app_module.last_test_dm_time = None
    app_module.trip_announced = False
    app_module.last_overland_update = None
    app_module.stop_recording()
    app_module.recent_locations.clear()
    app_module.best_mile.update(record=None, loaded=False, last_attempt=float("-inf"))
    app_module.best_effort_leaders.update(leaders={}, loaded=False)
    app_module.run_cache.clear()
    monkeypatch.setattr(app_module.weather, "fetch_current_conditions",
                        lambda lat, lon, timeout=5: {"temperature_f": 60, "weather_code": 0, "timezone": "America/Los_Angeles"})
    monkeypatch.setattr(app_module.map_image, "render_route_png", lambda path, **kwargs: b"PNG")
    yield
    wait_for_background()


@pytest.fixture(autouse=True)
def dms(monkeypatch):
    """Every DM the app tries to send, instead of sending it."""
    sent = []

    def fake_send_dm(message, user_id=None, image_png=None, label=None):
        sent.append(SimpleNamespace(text=message, to=user_id or "you", image=image_png))
        return True

    monkeypatch.setattr(app_module, "send_dm", fake_send_dm)
    return sent


@pytest.fixture
def client():
    return app_module.app.test_client()


@pytest.fixture
def settings(client):
    """settings(goal_distance_miles=5.0, ...) - change admin settings through the real endpoint."""
    def change(**values):
        response = client.post("/admin/settings", json={"password": ADMIN_PASSWORD, **values})
        assert response.status_code == 200, response.get_json()
        return response.get_json()
    return change


def position(meters):
    """A point `meters` along a straight line heading north-east."""
    step = meters * math.sqrt(0.5)
    return LAT0 + step / METERS_PER_DEG_LAT, LON0 + step / METERS_PER_DEG_LON


@pytest.fixture
def simulate(client):
    """simulate([(miles, seconds per mile), ("stop", seconds), ...]) sends a run to /overland,
    one point every 5 seconds, then stops the trip (unless end=False).

    altitude: optional function of meters run -> altitude in meters.
    during: optional {mile: callable} - each called once when the run passes that mile.
    """
    def run(segments, end=True, altitude=None, during=None):
        t, meters, done = 0.0, 0.0, set()
        for segment in segments:
            if segment[0] == "stop":
                speed, until_t, until_m = 0.0, t + segment[1], meters
            else:
                miles, pace = segment
                speed, until_t, until_m = MILE_METERS / pace, None, meters + miles * MILE_METERS
            while (until_t is not None and t < until_t) or (until_t is None and meters < until_m):
                for mark, action in (during or {}).items():
                    if mark not in done and meters >= mark * MILE_METERS:
                        done.add(mark)
                        action()
                lat, lon = position(meters)
                properties = {
                    "timestamp": (START + timedelta(seconds=t)).isoformat().replace("+00:00", "Z"),
                    "horizontal_accuracy": 5, "speed": speed,
                }
                if altitude:
                    properties["altitude"] = altitude(meters)
                response = client.post("/overland", headers=TOKEN, json={"trip": {"mode": "running"}, "locations": [{
                    "type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": properties,
                }]})
                assert response.status_code == 200
                t += 5
                meters += speed * 5
        if end:
            client.post("/overland", headers=TOKEN, json={"trip": None, "locations": []})
        wait_for_background()
    return run


@pytest.fixture
def fake_db(monkeypatch):
    """Switch run saving on, with every database call replaced by a recorder.
    Returns the object holding the calls and canned answers."""
    db = app_module.db
    state = SimpleNamespace(calls=[], next_id=100, existing={7: "completed", 8: "completed"},
                            test_runs=[41, 42], listed=[])

    def record(name, result=None):
        def fake(*args, **kwargs):
            state.calls.append((name, args, kwargs))
            return result(*args, **kwargs) if callable(result) else result
        return fake

    def create_run(*args):
        state.next_id += 1
        state.existing[state.next_id] = "active"
        return state.next_id

    monkeypatch.setattr(app_module, "PERSISTENCE_ENABLED", True)
    monkeypatch.setattr(db, "create_run", record("create_run", create_run))
    monkeypatch.setattr(db, "save_point_chunk", record("save_point_chunk"))
    monkeypatch.setattr(db, "finish_run", record("finish_run"))
    monkeypatch.setattr(db, "delete_run", record("delete_run", lambda run_id: state.existing.pop(run_id, None) is not None))
    monkeypatch.setattr(db, "delete_test_runs", record("delete_test_runs", lambda keep: list(state.test_runs)))
    monkeypatch.setattr(db, "list_runs", record("list_runs", lambda include_all: state.listed))
    monkeypatch.setattr(db, "load_settings", record("load_settings", lambda keys: {}))
    monkeypatch.setattr(db, "save_settings", record("save_settings"))
    monkeypatch.setattr(db, "save_conditions", record("save_conditions"))
    monkeypatch.setattr(db, "mark_other_active_runs_interrupted", record("mark_other_active_runs_interrupted", 0))
    monkeypatch.setattr(db, "find_recoverable_run", record("find_recoverable_run", None))
    monkeypatch.setattr(db, "find_latest_completed_run", record("find_latest_completed_run", None))
    monkeypatch.setattr(db, "load_best_mile", record("load_best_mile", None))
    monkeypatch.setattr(db, "has_best_efforts_table", record("has_best_efforts_table", True))
    monkeypatch.setattr(db, "load_best_effort_leaders", record("load_best_effort_leaders", {}))
    state.names = lambda: [name for name, _, _ in state.calls]
    return state
