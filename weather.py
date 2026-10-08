import logging

import requests

logger = logging.getLogger("run-split-tracker")

# Open-Meteo: free for non-commercial use, no API key. One call per run.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "weather_code,wind_speed_10m,wind_direction_10m,is_day"
)

# WMO weather codes -> short spoken-friendly description (for the start DM).
WEATHER_DESCRIPTIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def fetch_current_conditions(lat, lon, timeout=5):
    """Current weather at a location, shaped like an rst_run_conditions row, or None on failure."""
    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "current": CURRENT_FIELDS,
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "auto",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        current = data["current"]
    except Exception as exc:
        logger.warning("Weather: fetch failed (%s: %s)", type(exc).__name__, exc)
        return None

    return {
        "temperature_f": current.get("temperature_2m"),
        "feels_like_f": current.get("apparent_temperature"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_mph": current.get("wind_speed_10m"),
        "wind_direction_deg": current.get("wind_direction_10m"),
        "precipitation_in": current.get("precipitation"),
        "weather_code": current.get("weather_code"),
        "is_day": current.get("is_day"),
        "utc_offset_minutes": data["utc_offset_seconds"] // 60 if "utc_offset_seconds" in data else None,
        "timezone": data.get("timezone"),  # e.g. "America/Los_Angeles" - where the run is, from "timezone": "auto"
    }


def describe(conditions):
    """e.g. '58°F, partly cloudy, wind 7 mph W' - for the tracking-started DM."""
    parts = []
    if conditions.get("temperature_f") is not None:
        parts.append(f"{round(conditions['temperature_f'])}°F")
    sky = WEATHER_DESCRIPTIONS.get(conditions.get("weather_code"))
    if sky:
        parts.append(sky)
    wind = conditions.get("wind_mph")
    if wind is not None and round(wind) > 0:
        direction = conditions.get("wind_direction_deg")
        compass = f" {COMPASS[round(direction / 45) % 8]}" if direction is not None else ""
        parts.append(f"wind {round(wind)} mph{compass}")
    return ", ".join(parts)
