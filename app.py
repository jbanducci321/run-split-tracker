import hmac
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("run-split-tracker")

app = Flask(__name__)

OVERLAND_ACCESS_TOKEN = os.environ.get("OVERLAND_ACCESS_TOKEN", "")


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
    logger.info("Received %d location point(s)", len(locations))

    for feature in locations:
        props = feature.get("properties", {})
        lon, lat = feature.get("geometry", {}).get("coordinates", [None, None])
        logger.info(
            "point lat=%s lon=%s time=%s accuracy=%sm speed=%s motion=%s",
            lat, lon, props.get("timestamp"),
            props.get("horizontal_accuracy"), props.get("speed"), props.get("motion"),
        )

    # Overland expects this exact shape back to consider the batch delivered
    return jsonify(result="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
