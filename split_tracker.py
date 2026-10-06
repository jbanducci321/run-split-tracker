import math
from collections import deque
from datetime import datetime

# Saved with every run. Bump it whenever a change would alter the numbers the
# tracker produces from the same raw points, so stored runs can be told apart
# (and reprocessed from their raw points) later.
ALGORITHM_VERSION = "v4-autopause"

MILE_METERS = 1609.344
CHECKPOINT_METERS = MILE_METERS / 2  # report every half mile: halfway pace, then full-mile split

# Points less precise than this (meters) are dropped rather than trusted.
MAX_ACCURACY_METERS = 25

# Two consecutive RAW points implying a faster pace than this are treated as
# a GPS glitch (a "teleport"), not a real runner, and dropped before they can
# ever reach the smoothing buffer.
MAX_PLAUSIBLE_SPEED_MPS = 8.0

# Simple moving average applied to accepted raw points, used only for the
# on-screen map path. Distance/pace math uses raw points instead (see
# MIN_MOVEMENT_METERS below) - smoothing pulls points toward the inside of
# curves, which quietly shortens measured distance on anything but a
# straight line, so it's kept out of the numbers that matter.
SMOOTHING_WINDOW = 3

# A raw point closer than this to the last point we counted is treated as
# GPS jitter, not real movement, and doesn't advance the distance total.
# Needed because distance math no longer benefits from smoothing's noise
# damping. Tuned against simulated jitter (+/-4m) on a tight turn at a 5s
# reporting interval - values 3-6m all performed similarly (~1.8% avg error
# vs. ~4-5.5% for the old smoothed approach); 5m was picked as the middle
# of that range. Re-tune if the reporting interval changes.
MIN_MOVEMENT_METERS = 5.0

# Auto-pause, like Strava's: if the distance anchor hasn't advanced for longer
# than this, you've stopped (crosswalk, water break) and that time is left
# out of moving time. At a 5s reporting interval, even a slow walk advances
# the anchor every ~5s, so 10s only trips on a real stop.
PAUSE_AFTER_SECONDS = 10

# The phone's own speed reading (GPS doppler, m/s) below which you're treated
# as standing still. More reliable than position at a stop, where a few
# meters of GPS wobble can otherwise look like movement.
STATIONARY_SPEED_MPS = 0.5


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
        self.last_outcome = None
        self.start_time = None
        self.last_seen_time = None

        self.raw_buffer = deque(maxlen=SMOOTHING_WINDOW)
        self.last_raw_point = None  # (lat, lon, timestamp) - pre-smoothing, for outlier checks

        self.last_smoothed_point = None  # (lat, lon) - post-smoothing, for the map path only
        self.path = []  # [(lat, lon), ...] smoothed points, for the map

        self.distance_anchor = None  # (lat, lon, timestamp) - raw point distance is measured from
        self.cumulative_meters = 0.0
        self.last_speed_mps = None
        self.moving_seconds = 0.0  # moving time up to the current distance anchor
        self.moving_speed_mps = None  # speed of the last un-paused segment
        self.next_split_mile = 1
        self.next_checkpoint_meters = CHECKPOINT_METERS
        self.splits = []
        self.goal_notified = False  # per-trip flags, set by app.py once each goal DM is sent
        self.goal_half_notified = False
        self.test_mode_used = False  # set by app.py - a run with test mode on at any point never counts for records

    def _snap_to_path(self, lat, lon):
        """The closest spot on the recent part of the drawn (smoothed) path, so
        a mile marker sits on the line instead of a few meters off it."""
        recent = self.path[-6:]
        if len(recent) < 2:
            return lat, lon
        # Flat-earth approximation - fine over the few tens of meters involved.
        scale = math.cos(math.radians(lat))
        best, best_dist = (lat, lon), float("inf")
        for (lat1, lon1), (lat2, lon2) in zip(recent, recent[1:]):
            dx, dy = (lon2 - lon1) * scale, lat2 - lat1
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (((lon - lon1) * scale) * dx + (lat - lat1) * dy) / length_sq))
            p_lat, p_lon = lat1 + dy * t, lon1 + (lon2 - lon1) * t
            dist = ((p_lon - lon) * scale) ** 2 + (p_lat - lat) ** 2
            if dist < best_dist:
                best, best_dist = (p_lat, p_lon), dist
        return best

    def current_stats(self):
        distance_miles = self.cumulative_meters / MILE_METERS
        elapsed_seconds = (
            (self.last_seen_time - self.start_time).total_seconds()
            if self.start_time and self.last_seen_time else 0
        )
        # Time since the last confirmed movement counts as moving until it
        # passes the auto-pause threshold.
        paused = False
        moving_seconds = self.moving_seconds
        if self.distance_anchor and self.last_seen_time:
            since_anchor = (self.last_seen_time - self.distance_anchor[2]).total_seconds()
            paused = self.active and since_anchor > PAUSE_AFTER_SECONDS
            if not paused:
                moving_seconds += since_anchor

        average_pace_seconds = moving_seconds / distance_miles if distance_miles > 0.01 else None
        current_pace_seconds = (
            MILE_METERS / self.last_speed_mps if self.last_speed_mps and not paused else None
        )

        # Distance/pace since the last completed mile - Strava's trailing
        # partial split. Mile boundaries land exactly on whole-mile marks, so
        # the last split's distance is just the completed mile count.
        partial_distance_miles = distance_miles - (self.next_split_mile - 1)
        partial_moving = moving_seconds - (self.splits[-1]["crossing_moving"] if self.splits else 0.0)
        partial_pace_seconds = partial_moving / partial_distance_miles if partial_distance_miles > 0.02 else None

        fastest = min(self.splits, key=lambda s: s["pace_seconds"]) if self.splits else None

        return {
            "active": self.active,
            "paused": paused,
            "distance_miles": round(distance_miles, 3),
            "elapsed_seconds": elapsed_seconds,
            "elapsed_display": format_duration(elapsed_seconds),
            "moving_seconds": moving_seconds,
            "moving_display": format_duration(moving_seconds),
            "average_pace_seconds": round(average_pace_seconds, 1) if average_pace_seconds else None,
            "average_pace_display": format_pace(average_pace_seconds) if average_pace_seconds else None,
            "current_pace_seconds": round(current_pace_seconds, 1) if current_pace_seconds else None,
            "current_pace_display": format_pace(current_pace_seconds) if current_pace_seconds else None,
            "splits": [
                {
                    "mile": s["mile"],
                    "pace_seconds": round(s["pace_seconds"], 1),
                    "pace_display": s["pace_display"],
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "at_display": format_duration(s["crossing_moving"]),  # moving time when the mile was finished
                }
                for s in self.splits
            ],
            "fastest_split": {"mile": fastest["mile"], "pace_display": fastest["pace_display"]} if fastest else None,
            "partial_split": {
                "distance_miles": round(partial_distance_miles, 2),
                "pace_seconds": round(partial_pace_seconds, 1),
                "pace_display": format_pace(partial_pace_seconds),
            } if partial_pace_seconds else None,
            "path": [[lat, lon] for lat, lon in self.path],
        }

    def end(self):
        summary = self.current_stats()
        summary["active"] = False
        summary["paused"] = False
        # Instantaneous pace of the last few steps (usually slowing to a stop)
        # is meaningless once the run is over.
        summary["current_pace_display"] = None
        summary["current_pace_seconds"] = None
        self.reset()
        return summary

    def process_point(self, lat, lon, timestamp, accuracy, speed=None):
        """Feed one GPS point in. Returns a checkpoint event or None, and sets
        last_outcome to what happened to the point (for the ping summary log)."""
        if not self.active:
            self.reset()
            self.active = True
            self.start_time = timestamp
        self.last_outcome = "counted"

        if accuracy is not None and accuracy > MAX_ACCURACY_METERS:
            self.last_outcome = "poor accuracy"
            return None

        self.last_seen_time = timestamp

        elapsed = None
        prev_time = None
        if self.last_raw_point is not None:
            prev_lat, prev_lon, prev_time = self.last_raw_point
            elapsed = (timestamp - prev_time).total_seconds()
            if elapsed <= 0:
                self.last_outcome = "duplicate/out of order"
                return None
            raw_distance = haversine_meters(prev_lat, prev_lon, lat, lon)
            if raw_distance / elapsed > MAX_PLAUSIBLE_SPEED_MPS:
                self.last_outcome = "GPS jump"
                return None  # keep it out of the smoothing buffer entirely

        self.last_raw_point = (lat, lon, timestamp)

        # Smoothed path, for the map only - not used for distance/pace math.
        self.raw_buffer.append((lat, lon))
        smoothed_lat = sum(p[0] for p in self.raw_buffer) / len(self.raw_buffer)
        smoothed_lon = sum(p[1] for p in self.raw_buffer) / len(self.raw_buffer)
        self.last_smoothed_point = (smoothed_lat, smoothed_lon)
        self.path.append((smoothed_lat, smoothed_lon))

        if self.distance_anchor is None:
            self.distance_anchor = (lat, lon, timestamp)
            return None

        # iOS reports -1 when speed is unknown, so only trust non-negative values.
        if speed is not None and 0 <= speed < STATIONARY_SPEED_MPS:
            self.last_outcome = "standing still"
            return None  # phone says you're standing still - GPS wobble isn't distance

        anchor_lat, anchor_lon, anchor_time = self.distance_anchor
        distance = haversine_meters(anchor_lat, anchor_lon, lat, lon)
        if distance < MIN_MOVEMENT_METERS:
            self.last_outcome = "jitter (<5m moved)"
            return None  # not enough movement yet to be confident this is real, not jitter

        anchor_elapsed = (timestamp - anchor_time).total_seconds()
        if anchor_elapsed <= PAUSE_AFTER_SECONDS:
            moving_portion = anchor_elapsed
            self.moving_speed_mps = distance / anchor_elapsed
        elif self.moving_speed_mps:
            # You stopped somewhere in this gap - only the time it would take
            # to cover this distance at your recent pace counts as moving.
            moving_portion = min(anchor_elapsed, distance / self.moving_speed_mps)
        else:
            moving_portion = PAUSE_AFTER_SECONDS  # stood still before your first real stride
        self.last_speed_mps = distance / moving_portion

        distance_before = self.cumulative_meters
        moving_before = self.moving_seconds
        self.cumulative_meters += distance
        self.moving_seconds += moving_portion
        self.distance_anchor = (lat, lon, timestamp)

        if self.cumulative_meters < self.next_checkpoint_meters:
            return None

        # Interpolate exactly where along this segment the checkpoint fell,
        # instead of crediting it to whenever this particular ping arrived.
        fraction = (self.next_checkpoint_meters - distance_before) / distance
        crossing_time = anchor_time + (timestamp - anchor_time) * fraction
        crossing_moving = moving_before + moving_portion * fraction
        checkpoint_miles = self.next_checkpoint_meters / MILE_METERS
        is_full_mile = round(checkpoint_miles * 2) % 2 == 0
        self.next_checkpoint_meters += CHECKPOINT_METERS

        if is_full_mile:
            # Split pace uses moving time, so a stop mid-mile doesn't count
            # against that mile - matches how Strava reports splits.
            split_start_moving = self.splits[-1]["crossing_moving"] if self.splits else 0.0
            split_seconds = crossing_moving - split_start_moving
            # Where the mile was finished, for the map's mile markers.
            crossing_lat, crossing_lon = self._snap_to_path(
                anchor_lat + (lat - anchor_lat) * fraction, anchor_lon + (lon - anchor_lon) * fraction,
            )
            event = {
                "type": "split",
                "mile": self.next_split_mile,
                "pace_seconds": split_seconds,
                "pace_display": format_pace(split_seconds),
                "crossing_time": crossing_time,
                "crossing_moving": crossing_moving,
                "lat": round(crossing_lat, 6),
                "lon": round(crossing_lon, 6),
            }
            self.splits.append(event)
            self.next_split_mile += 1
            return event

        # Halfway through the current mile - the caller decides what to report
        # (whole-trip average pace, per app.py) - this isn't a recorded split
        # and doesn't affect split timing math.
        return {
            "type": "halfway",
            "mile": checkpoint_miles,
            "crossing_time": crossing_time,
        }
