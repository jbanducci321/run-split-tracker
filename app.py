import hmac
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from discord_notifier import send_dm
from split_tracker import RunTracker, parse_timestamp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run-split-tracker")

app = Flask(__name__)

OVERLAND_ACCESS_TOKEN = os.environ.get("OVERLAND_ACCESS_TOKEN", "")

# Testing toggle: sends a DM every TEST_MODE_INTERVAL_SECONDS instead of only
# at real checkpoints - useful for validating pace math in small increments
# without a full run. DISCORD_TEST_MODE is just the startup default; the
# password-protected /admin/test-mode route below can flip it live without a
# restart. Any restart/redeploy resets it back to whatever the env var says.
TEST_MODE_INTERVAL_SECONDS = int(os.environ.get("TEST_MODE_INTERVAL_SECONDS", "15"))
TEST_MODE_PASSWORD = os.environ.get("TEST_MODE_PASSWORD", "")

admin_state = {
    "discord_test_mode": os.environ.get("DISCORD_TEST_MODE", "false").lower() == "true",
}

tracker = RunTracker()
last_summary = None  # most recently finished trip, kept until a new one starts
last_test_dm_time = None  # timestamp (from GPS data, not wall clock) of the last test-mode DM


def is_authorized(auth_header: str) -> bool:
    expected = f"Bearer {OVERLAND_ACCESS_TOKEN}"
    return bool(OVERLAND_ACCESS_TOKEN) and hmac.compare_digest(auth_header, expected)


def is_admin_password_correct(password: str) -> bool:
    return bool(TEST_MODE_PASSWORD) and hmac.compare_digest(password or "", TEST_MODE_PASSWORD)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/status")
def status():
    if tracker.active:
        return jsonify(tracker.current_stats())
    return jsonify(last_summary or tracker.current_stats())


@app.post("/admin/test-mode")
def admin_test_mode():
    data = request.get_json(silent=True) or {}
    if not is_admin_password_correct(data.get("password", "")):
        return jsonify(error="unauthorized"), 401

    if "enabled" in data:
        admin_state["discord_test_mode"] = bool(data["enabled"])

    return jsonify(discord_test_mode=admin_state["discord_test_mode"])


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
            continue  # skip the real checkpoint DMs below while test mode is on

        if not event:
            continue

        if event["type"] == "split":
            logger.info("MILE %d SPLIT: %s", event["mile"], event["pace_display"])
            send_dm(f"Mile {event['mile']} - Pace: {event['pace_display']}")
        elif event["pace_display"]:
            logger.info("Halfway checkpoint at mile %.1f: %s", event["mile"], event["pace_display"])
            send_dm(f"Pace: {event['pace_display']}")

    if points and tracker.last_speed_mps is not None:
        stats = tracker.current_stats()
        logger.info(
            "progress: %.2fmi total, current pace %s",
            stats["distance_miles"], stats["current_pace_display"],
        )

    return jsonify(result="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
