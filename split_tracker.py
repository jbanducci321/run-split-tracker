import math
from collections import deque
from datetime import datetime

MILE_METERS = 1609.344

# Points less precise than this (meters) are dropped rather than trusted.
MAX_ACCURACY_METERS = 25

# Two consecutive RAW points implying a faster pace than this are treated as
# a GPS glitch (a "teleport"), not a real runner, and dropped before they can
# ever reach the smoothing buffer.
MAX_PLAUSIBLE_SPEED_MPS = 8.0

# Simple moving average applied to accepted raw points before they're used
# for distance math, to damp normal GPS jitter (a few meters of "wander" per
# reading) instead of letting it accumulate into phantom distance.
SMOOTHING_WINDOW = 3


def haversine_meters(lat1, lon1, lat2, lon2):
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_pace(seconds):
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}/mi"


def format_duration(seconds):
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class RunTracker:
    """Tracks one active run at a time and detects mile-split crossings.

    Assumes a single gunicorn worker process (state lives in memory) -
    this is a personal single-user tracker, not a multi-tenant service.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.start_time = None
        self.last_seen_time = None

        self.raw_buffer = deque(maxlen=SMOOTHING_WINDOW)
        self.last_raw_point = None  # (lat, lon, timestamp) - pre-smoothing, for outlier checks

        self.last_smoothed_point = None  # (lat, lon) - post-smoothing, used for distance math
        self.path = []  # [(lat, lon), ...] smoothed points, for a map later

        self.cumulative_meters = 0.0
        self.last_speed_mps = None
        self.next_split_mile = 1
        self.splits = []

    def current_stats(self):
        distance_miles = self.cumulative_meters / MILE_METERS
        elapsed_seconds = (
            (self.last_seen_time - self.start_time).total_seconds()
            if self.start_time and self.last_seen_time else 0
        )
        average_pace_seconds = elapsed_seconds / distance_miles if distance_miles > 0.01 else None
        current_pace_seconds = MILE_METERS / self.last_speed_mps if self.last_speed_mps else None

        return {
            "active": self.active,
            "distance_miles": round(distance_miles, 3),
            "elapsed_seconds": elapsed_seconds,
            "elapsed_display": format_duration(elapsed_seconds),
            "average_pace_display": format_pace(average_pace_seconds) if average_pace_seconds else None,
            "current_pace_display": format_pace(current_pace_seconds) if current_pace_seconds else None,
            "splits": [{"mile": s["mile"], "pace_display": s["pace_display"]} for s in self.splits],
            "path": [[lat, lon] for lat, lon in self.path],
        }

    def end(self):
        summary = self.current_stats()
        summary["active"] = False
        self.reset()
        return summary

    def process_point(self, lat, lon, timestamp, accuracy):
        if not self.active:
            self.reset()
            self.active = True
            self.start_time = timestamp

        if accuracy is not None and accuracy > MAX_ACCURACY_METERS:
            return None

        self.last_seen_time = timestamp

        elapsed = None
        prev_time = None
        if self.last_raw_point is not None:
            prev_lat, prev_lon, prev_time = self.last_raw_point
            elapsed = (timestamp - prev_time).total_seconds()
            if elapsed <= 0:
                return None  # out-of-order or duplicate point
            raw_distance = haversine_meters(prev_lat, prev_lon, lat, lon)
            if raw_distance / elapsed > MAX_PLAUSIBLE_SPEED_MPS:
                return None  # GPS jump - keep it out of the smoothing buffer entirely

        self.last_raw_point = (lat, lon, timestamp)
        self.raw_buffer.append((lat, lon))
        smoothed_lat = sum(p[0] for p in self.raw_buffer) / len(self.raw_buffer)
        smoothed_lon = sum(p[1] for p in self.raw_buffer) / len(self.raw_buffer)

        if self.last_smoothed_point is None or elapsed is None:
            self.last_smoothed_point = (smoothed_lat, smoothed_lon)
            self.path.append((smoothed_lat, smoothed_lon))
            return None

        prev_s_lat, prev_s_lon = self.last_smoothed_point
        distance = haversine_meters(prev_s_lat, prev_s_lon, smoothed_lat, smoothed_lon)
        self.last_speed_mps = distance / elapsed

        distance_before = self.cumulative_meters
        self.cumulative_meters += distance
        self.last_smoothed_point = (smoothed_lat, smoothed_lon)
        self.path.append((smoothed_lat, smoothed_lon))

        target_meters = self.next_split_mile * MILE_METERS
        if self.cumulative_meters < target_meters or distance <= 0:
            return None

        # Interpolate exactly where along this segment the mile boundary
        # fell, instead of crediting the whole split to whenever this
        # particular ping happened to arrive.
        fraction = (target_meters - distance_before) / distance
        crossing_time = prev_time + (timestamp - prev_time) * fraction
        split_start = self.splits[-1]["crossing_time"] if self.splits else self.start_time
        split_seconds = (crossing_time - split_start).total_seconds()

        split = {
            "mile": self.next_split_mile,
            "pace_seconds": split_seconds,
            "pace_display": format_pace(split_seconds),
            "crossing_time": crossing_time,
        }
        self.splits.append(split)
        self.next_split_mile += 1
        return split
