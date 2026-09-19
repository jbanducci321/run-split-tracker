import hmac
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from discord_notifier import send_dm
from split_tracker import MILE_METERS, RunTracker, format_duration, parse_timestamp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run-split-tracker")

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
        elapsed = stats["elapsed_seconds"] or 0
        if remaining_miles > 0 and stats["distance_miles"] > 0.01 and elapsed > 0:
            average_pace_seconds = elapsed / stats["distance_miles"]
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
            last_summary = tracker.end()
            logger.info(
                "Trip ended. Distance=%.2fmi Splits=%s",
                last_summary["distance_miles"],
                [s["pace_display"] for s in last_summary["splits"]],
            )
        return jsonify(result="ok")

    points = []
    for feature in locations:
        props = feature.get("properties", {})
        timestamp_raw = props.get("timestamp")
        if not timestamp_raw:
            continue
        lon, lat = feature.get("geometry", {}).get("coordinates", [None, None])
        if lat is None or lon is None:
            continue
        points.append((lat, lon, parse_timestamp(timestamp_raw), props.get("horizontal_accuracy")))

    points.sort(key=lambda p: p[2])

    for lat, lon, timestamp, accuracy in points:
        event = tracker.process_point(lat, lon, timestamp, accuracy)

        if admin_state["discord_test_mode"]:
            due = (
                last_test_dm_time is None
                or (timestamp - last_test_dm_time).total_seconds() >= TEST_MODE_INTERVAL_SECONDS
            )
            if tracker.last_speed_mps is not None and due:
                pace = tracker.current_stats()["current_pace_display"]
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
        if goal and not tracker.goal_notified and tracker.cumulative_meters / MILE_METERS >= goal:
            tracker.goal_notified = True
            logger.info("GOAL REACHED: %.2f mi", goal)
            send_dm(f"Goal reached! {goal:.2f} mi")

    if points and tracker.last_speed_mps is not None:
        stats = tracker.current_stats()
        logger.info(
            "progress: %.2fmi total, current pace %s",
            stats["distance_miles"], stats["current_pace_display"],
        )

    return jsonify(result="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
