import hmac
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from split_tracker import RunTracker, parse_timestamp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run-split-tracker")

app = Flask(__name__)

OVERLAND_ACCESS_TOKEN = os.environ.get("OVERLAND_ACCESS_TOKEN", "")

tracker = RunTracker()


def is_authorized(auth_header: str) -> bool:
    expected = f"Bearer {OVERLAND_ACCESS_TOKEN}"
    return bool(OVERLAND_ACCESS_TOKEN) and hmac.compare_digest(auth_header, expected)


@app.get("/")
def health():
    return jsonify(status="ok")


@app.post("/overland")
def receive_overland_batch():
    if not is_authorized(request.headers.get("Authorization", "")):
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    locations = payload.get("locations", [])
    trip_active = bool(payload.get("trip"))

    if not trip_active:
        if tracker.active:
            summary = tracker.end()
            logger.info("Trip ended. Splits recorded: %s", [s["pace_display"] for s in summary])
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
        split = tracker.process_point(lat, lon, timestamp, accuracy)
        if split:
            logger.info("MILE %d SPLIT: %s", split["mile"], split["pace_display"])

    if points:
        remaining = tracker.next_split_mile * 1609.344 - tracker.cumulative_meters
        logger.info(
            "progress: %.0fm total, %.0fm to mile %d",
            tracker.cumulative_meters, max(remaining, 0), tracker.next_split_mile,
        )

    return jsonify(result="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
