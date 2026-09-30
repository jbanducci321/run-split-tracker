import hmac
import logging
import os
import time
from datetime import timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import db
from discord_notifier import send_dm
from split_tracker import ALGORITHM_VERSION, MILE_METERS, RunTracker, format_duration, parse_timestamp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run-split-tracker")

# Raw points are buffered and written as one compressed chunk about once a
# minute - one row per point would use ~10x the (small) database storage.
CHUNK_FLUSH_SECONDS = 60
PERSISTENCE_ENABLED = db.is_configured()
if PERSISTENCE_ENABLED:
    logger.info("Run saving enabled (database %s)", os.environ["DB_NAME"])
else:
    logger.warning("Run saving DISABLED - set %s to enable it", ", ".join(db.REQUIRED_ENV))

app = Flask(__name__)

OVERLAND_ACCESS_TOKEN = os.environ.get("OVERLAND_ACCESS_TOKEN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# TEST_MODE_INTERVAL_SECONDS: sends a DM every N seconds instead of only at
# real checkpoints - useful for validating pace math in small increments
# without a full run.
TEST_MODE_INTERVAL_SECONDS = int(os.environ.get("TEST_MODE_INTERVAL_SECONDS", "15"))

MAX_GOAL_MILES = 99.99

# Live, password-changeable settings. DISCORD_TEST_MODE env var is just the
# startup default; the admin panel can flip these without a restart. Any
# restart/redeploy resets them back to whatever the env vars say.
admin_state = {
    "discord_test_mode": os.environ.get("DISCORD_TEST_MODE", "false").lower() == "true",
    "goal_distance_miles": None,
}

tracker = RunTracker()
last_summary = None  # most recently finished trip, kept until a new one starts
last_test_dm_time = None  # timestamp (from GPS data, not wall clock) of the last test-mode DM

# Database state for the trip in progress.
recording = {"run_id": None, "buffer": [], "chunk_index": 0, "last_db_attempt": float("-inf")}


def to_utc_naive(ts):
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def db_retry_due():
    # After a failed write, wait before trying again so an unreachable
    # database doesn't add a connect timeout to every Overland request.
    return time.monotonic() - recording["last_db_attempt"] >= CHUNK_FLUSH_SECONDS


def ensure_run_recorded():
    """Create the rst_runs row for the trip in progress, if it doesn't exist yet."""
    if recording["run_id"] is not None or not db_retry_due():
        return
    recording["last_db_attempt"] = time.monotonic()
    offset = tracker.start_time.utcoffset()
    try:
        recording["run_id"] = db.create_run(
            to_utc_naive(tracker.start_time),
            int(offset.total_seconds() // 60) if offset else None,
            admin_state["goal_distance_miles"],
            admin_state["discord_test_mode"],
            ALGORITHM_VERSION,
        )
    except Exception:
        logger.exception("DB: couldn't create the run row - retrying in %ds", CHUNK_FLUSH_SECONDS)
        return
    logger.info("DB: run %s started", recording["run_id"])


def flush_points(force=False):
    run_id = recording["run_id"]
    if run_id is None or not recording["buffer"] or not (force or db_retry_due()):
        return
    recording["last_db_attempt"] = time.monotonic()
    chunk, index = recording["buffer"], recording["chunk_index"]
    try:
        db.save_point_chunk(run_id, index, chunk)
    except Exception:
        logger.exception("DB: run %s chunk %d failed - keeping %d points buffered", run_id, index, len(chunk))
        return
    logger.info("DB: run %s chunk %d saved (%d points)", run_id, index, len(chunk))
    recording["buffer"] = []
    recording["chunk_index"] += 1


def stop_recording():
    recording.update(run_id=None, buffer=[], chunk_index=0, last_db_attempt=float("-inf"))


def finish_recording(summary, ended_at, goal_reached):
    run_id = recording["run_id"]
    if run_id is None:
        if PERSISTENCE_ENABLED:
            logger.error("DB: trip ended but was never saved (database unreachable the whole run)")
        stop_recording()
        return
    flush_points(force=True)
    if recording["buffer"]:
        logger.error("DB: run %s lost %d unsaved points at trip end", run_id, len(recording["buffer"]))
    try:
        db.finish_run(run_id, to_utc_naive(ended_at), summary, goal_reached)
        logger.info("DB: run %s completed (%.2f mi, %d splits)", run_id, summary["distance_miles"], len(summary["splits"]))
    except Exception:
        logger.exception("DB: run %s couldn't be marked completed - raw points are saved, summary is not", run_id)
    stop_recording()


def is_authorized(auth_header: str) -> bool:
    expected = f"Bearer {OVERLAND_ACCESS_TOKEN}"
    return bool(OVERLAND_ACCESS_TOKEN) and hmac.compare_digest(auth_header, expected)


def is_admin_password_correct(password: str) -> bool:
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(password or "", ADMIN_PASSWORD)


def validate_goal_distance(value):
    """Returns a clean float, None (to clear the goal), or raises ValueError."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("goal distance must be a number")
    if value <= 0 or value > MAX_GOAL_MILES:
        raise ValueError(f"goal distance must be greater than 0 and at most {MAX_GOAL_MILES}")
    rounded = round(value, 2)
    if abs(rounded - value) > 1e-9:
        raise ValueError("goal distance may have at most 2 decimal places")
    return rounded


def build_status_payload(stats):
    goal = admin_state["goal_distance_miles"]
    stats = dict(stats)
    stats["goal_distance_miles"] = goal
    stats["goal_progress_percent"] = None
    stats["eta_display"] = None

    if goal:
        stats["goal_progress_percent"] = round(min(stats["distance_miles"] / goal, 1) * 100, 1)

        remaining_miles = goal - stats["distance_miles"]
        moving = stats["moving_seconds"] or 0
        if remaining_miles > 0 and stats["distance_miles"] > 0.01 and moving > 0:
            average_pace_seconds = moving / stats["distance_miles"]
            stats["eta_display"] = format_duration(remaining_miles * average_pace_seconds)

    return stats


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/status")
def status():
    stats = tracker.current_stats() if tracker.active else (last_summary or tracker.current_stats())
    return jsonify(build_status_payload(stats))


@app.post("/admin/reset")
def admin_reset():
    global last_summary, last_test_dm_time

    data = request.get_json(silent=True) or {}
    if not is_admin_password_correct(data.get("password", "")):
        return jsonify(error="unauthorized"), 401

    # Clears the current/last run's location data only - goal distance and
    # test mode stay as you set them, since those are settings, not run data.
    # A run being recorded keeps its saved points but is marked 'reset'.
    if recording["run_id"] is not None:
        flush_points(force=True)
        try:
            db.set_run_status(recording["run_id"], "reset")
            logger.info("DB: run %s marked reset", recording["run_id"])
        except Exception:
            logger.exception("DB: couldn't mark run %s as reset", recording["run_id"])
        stop_recording()
    tracker.reset()
    last_summary = None
    last_test_dm_time = None
    return jsonify(result="ok")


@app.post("/admin/settings")
def admin_settings():
    data = request.get_json(silent=True) or {}
    if not is_admin_password_correct(data.get("password", "")):
        return jsonify(error="unauthorized"), 401

    if "discord_test_mode" in data:
        admin_state["discord_test_mode"] = bool(data["discord_test_mode"])

    if "goal_distance_miles" in data:
        try:
            admin_state["goal_distance_miles"] = validate_goal_distance(data["goal_distance_miles"])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    return jsonify(
        discord_test_mode=admin_state["discord_test_mode"],
        goal_distance_miles=admin_state["goal_distance_miles"],
    )


@app.post("/overland")
def receive_overland_batch():
    global last_summary, last_test_dm_time

    if not is_authorized(request.headers.get("Authorization", "")):
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    locations = payload.get("locations", [])
    trip_active = bool(payload.get("trip"))

    if not trip_active:
        if tracker.active:
            ended_at, goal_reached = tracker.last_seen_time, tracker.goal_notified
            last_summary = tracker.end()
            logger.info(
                "Trip ended. Distance=%.2fmi Splits=%s",
                last_summary["distance_miles"],
                [s["pace_display"] for s in last_summary["splits"]],
            )
            if PERSISTENCE_ENABLED:
                finish_recording(last_summary, ended_at, goal_reached)
        return jsonify(result="ok")

    if PERSISTENCE_ENABLED:
        recording["buffer"].extend(db.raw_point(feature) for feature in locations)

    points = []
    for feature in locations:
        props = feature.get("properties", {})
        timestamp_raw = props.get("timestamp")
        if not timestamp_raw:
            continue
        lon, lat = feature.get("geometry", {}).get("coordinates", [None, None])
        if lat is None or lon is None:
            continue
        points.append((
            lat, lon, parse_timestamp(timestamp_raw), props.get("horizontal_accuracy"), props.get("speed"),
        ))

    points.sort(key=lambda p: p[2])

    for lat, lon, timestamp, accuracy, speed in points:
        event = tracker.process_point(lat, lon, timestamp, accuracy, speed)

        if admin_state["discord_test_mode"]:
            due = (
                last_test_dm_time is None
                or (timestamp - last_test_dm_time).total_seconds() >= TEST_MODE_INTERVAL_SECONDS
            )
            if due:
                pace = tracker.current_stats()["current_pace_display"]  # None while paused
                if pace:
                    logger.info("TEST MODE update: %s", pace)
                    send_dm(f"Pace: {pace}")
                    last_test_dm_time = timestamp
        elif event:
            if event["type"] == "split":
                logger.info("MILE %d SPLIT: %s", event["mile"], event["pace_display"])
                send_dm(f"Mile {event['mile']} - Pace: {event['pace_display']}")
            else:
                avg_pace = tracker.current_stats()["average_pace_display"]
                logger.info("Halfway checkpoint at mile %.1f: avg pace %s", event["mile"], avg_pace)
                if avg_pace:
                    send_dm(f"Pace: {avg_pace}")

        goal = admin_state["goal_distance_miles"]
        distance_miles = tracker.cumulative_meters / MILE_METERS
        if goal and not tracker.goal_half_notified and distance_miles >= goal / 2:
            tracker.goal_half_notified = True
            avg_pace = tracker.current_stats()["average_pace_display"]
            logger.info("GOAL HALFWAY: %.2f / %.2f mi", goal / 2, goal)
            send_dm(f"Halfway to goal! {goal / 2:.2f} / {goal:.2f} mi - Avg pace: {avg_pace}")
        if goal and not tracker.goal_notified and distance_miles >= goal:
            tracker.goal_notified = True
            logger.info("GOAL REACHED: %.2f mi", goal)
            send_dm(f"Goal reached! {goal:.2f} mi")

    if PERSISTENCE_ENABLED and tracker.active:
        ensure_run_recorded()
        flush_points()

    if points and tracker.last_speed_mps is not None:
        stats = tracker.current_stats()
        logger.info(
            "progress: %.2fmi total, moving %s, current pace %s",
            stats["distance_miles"], stats["moving_display"],
            "PAUSED" if stats["paused"] else stats["current_pace_display"],
        )

    return jsonify(result="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
