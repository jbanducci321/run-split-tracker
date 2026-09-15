import math
from datetime import datetime

MILE_METERS = 1609.344

# Points less precise than this (meters) are dropped rather than trusted.
MAX_ACCURACY_METERS = 25

# Two accepted points implying a faster pace than this are treated as a GPS
# glitch (a "teleport"), not a real runner, and the later point is dropped.
MAX_PLAUSIBLE_SPEED_MPS = 8.0


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
        self.last_point = None  # (lat, lon, timestamp)
        self.cumulative_meters = 0.0
        self.next_split_mile = 1
        self.splits = []

    def end(self):
        summary = list(self.splits)
        self.reset()
        return summary

    def process_point(self, lat, lon, timestamp, accuracy):
        if not self.active:
            self.reset()
            self.active = True
            self.start_time = timestamp

        if accuracy is not None and accuracy > MAX_ACCURACY_METERS:
            return None

        if self.last_point is None:
            self.last_point = (lat, lon, timestamp)
            return None

        prev_lat, prev_lon, prev_time = self.last_point
        elapsed = (timestamp - prev_time).total_seconds()
        if elapsed <= 0:
            return None  # out-of-order or duplicate point

        distance = haversine_meters(prev_lat, prev_lon, lat, lon)
        if distance / elapsed > MAX_PLAUSIBLE_SPEED_MPS:
            return None  # discard rather than let a GPS jump corrupt the total

        distance_before = self.cumulative_meters
        self.cumulative_meters += distance
        self.last_point = (lat, lon, timestamp)

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
