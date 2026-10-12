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


# Elevation gain. The phone reports altitude in whole meters (+/-3 m, GPS
# blended with the barometer), so summing every point-to-point rise counts
# the 1 m flicker as climbing - thousands of fake feet per run. Instead,
# altitude is averaged over ELEVATION_SMOOTHING_METERS of route, and a climb
# or descent only counts once it moves ELEVATION_THRESHOLD_METERS past the
# last turning point. Tuned against Strava on three runs (382/254/373 ft vs
# 380/266/353 ft here; per-mile net changes within 2 ft).
ELEVATION_SMOOTHING_METERS = 80
ELEVATION_THRESHOLD_METERS = 4
FEET_PER_METER = 3.28084

# Before smoothing, glitches are dropped: the phone occasionally jumps between
# two altitude readings ~12 m apart for a few seconds (seen at the start of a
# run, as its altitude settles). A reading more than ALTITUDE_GLITCH_METERS
# from the median of the readings within ALTITUDE_GLITCH_WINDOW_SECONDS
# either side of it is dropped. Real hills change gradually, so on a steady
# climb the median is the reading itself and nothing real is lost.
ALTITUDE_GLITCH_WINDOW_SECONDS = 30
ALTITUDE_GLITCH_METERS = 8


# Best efforts: the fastest stretch of each standard distance anywhere in a
# run, timed with moving time like splits. (key, display name, meters)
BEST_EFFORT_DISTANCES = [
    ("400m", "400 m", 400.0),
    ("half_mile", "½ mile", MILE_METERS / 2),
    ("1k", "1K", 1000.0),
    ("1_mile", "1 mile", MILE_METERS),
    ("2_mile", "2 mile", 2 * MILE_METERS),
    ("5k", "5K", 5000.0),
    ("10k", "10K", 10000.0),
    ("15k", "15K", 15000.0),
    ("10_mile", "10 mile", 10 * MILE_METERS),
    ("20k", "20K", 20000.0),
    ("half_marathon", "Half marathon", 21097.5),
    ("30k", "30K", 30000.0),
    ("marathon", "Marathon", 42195.0),
]
# Faster than this pace (seconds per mile) over any stretch is a GPS glitch, not running.
FASTEST_PLAUSIBLE_PACE_SECONDS = 4 * 60


class ClimbCounter:
    """Total gain/loss of a (smoothed) altitude series, ignoring wiggles smaller than the threshold."""

    def __init__(self, threshold):
        self.threshold = threshold
        self.ref = None  # the last turning point (or the newest high/low while moving that way)
        self.direction = 0  # 1 climbing, -1 descending, 0 not yet known
        self.gain = 0.0
        self.loss = 0.0

    def add(self, altitude):
        if self.ref is None:
            self.ref = altitude
            return
        rise = altitude - self.ref
        if self.direction == 1:
            if rise > 0:
                self.gain += rise  # a new high on the way up
                self.ref = altitude
            elif -rise >= self.threshold:
                self.loss += -rise  # turned downhill
                self.direction, self.ref = -1, altitude
        elif self.direction == -1:
            if rise < 0:
                self.loss += -rise  # a new low on the way down
                self.ref = altitude
            elif rise >= self.threshold:
                self.gain += rise  # turned uphill
                self.direction, self.ref = 1, altitude
        elif abs(rise) >= self.threshold:
            if rise > 0:
                self.gain += rise
            else:
                self.loss += -rise
            self.direction, self.ref = (1 if rise > 0 else -1), altitude


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
        self.timezone = None  # e.g. "America/Los_Angeles", set by app.py from the start-of-run weather lookup
        self.pace_alert_armed = True  # pace-check DM state, managed by app.py
        self.last_pace_alert_moving = None
        # (cumulative meters, moving seconds) after each counted point - for recent_pace().
        # ~30 min of points at a 5s interval, far more than any window used.
        self.progress = deque(maxlen=400)

        # Elevation (see ELEVATION_SMOOTHING_METERS). Each accepted point's
        # altitude is smoothed once the route has gone half a window past it.
        self.elev_track_meters = 0.0  # point-to-point distance along accepted points (the smoothing axis)
        self.elev_pending = []  # (seconds, track meters, run meters, altitude m, path index) awaiting the glitch check
        self.elev_checked = 0  # how many of elev_pending have been checked
        self.elev_samples = []  # glitch-free: (track meters, run meters, altitude m, path index)
        self.elev_window = [0, 0, 0.0]  # lo index, hi index, altitude sum of samples[lo:hi]
        self.elev_profile = []  # smoothed: (run meters, altitude m, gain so far m, loss so far m, path index)
        self.climb = ClimbCounter(ELEVATION_THRESHOLD_METERS)

        # Best efforts (see BEST_EFFORT_DISTANCES): every (meters, moving
        # seconds, path index) the run has passed through, plus per distance
        # a sliding-window start pointer and the best stretch found so far.
        self.effort_samples = []
        self.effort_window_start = {key: 0 for key, _, _ in BEST_EFFORT_DISTANCES}  # for stretches ending at a sample
        self.effort_next_start = {key: 0 for key, _, _ in BEST_EFFORT_DISTANCES}  # for stretches starting at a sample
        self.efforts = {}  # key -> (seconds, start meters, path index at start, path index at end)

    def _consider_effort(self, key, meters, seconds, start_meters, path_start, path_end):
        if seconds < meters / MILE_METERS * FASTEST_PLAUSIBLE_PACE_SECONDS:
            return  # impossibly fast - GPS trouble, not a real effort
        best = self.efforts.get(key)
        if best is None or seconds < best[0]:
            self.efforts[key] = (seconds, start_meters, path_start, path_end)

    def _update_efforts(self):
        """Check every stretch of each distance that the newest sample completes.

        Moving time is linear between samples, so the fastest stretch always
        starts or ends exactly at a sample - checking both kinds finds it
        exactly. Two-pointer slides keep this cheap however long the run gets.
        """
        samples = self.effort_samples
        end_meters, end_moving, end_path = samples[-1]
        prev_meters, prev_moving, _ = samples[-2]
        for key, _, meters in BEST_EFFORT_DISTANCES:
            if end_meters < meters:
                break  # distances are in increasing order
            # Stretches ENDING at the newest sample: the start falls between samples i and i+1.
            i = self.effort_window_start[key]
            while samples[i + 1][0] <= end_meters - meters:
                i += 1
            self.effort_window_start[key] = i
            (m0, t0, path0), (m1, t1, _) = samples[i], samples[i + 1]
            start_meters = end_meters - meters
            start_moving = t0 + (t1 - t0) * (start_meters - m0) / (m1 - m0)
            self._consider_effort(key, meters, end_moving - start_moving, start_meters, path0, end_path)
            # Stretches STARTING at a sample and ending inside the newest segment.
            k = self.effort_next_start[key]
            while samples[k][0] + meters <= end_meters:
                stretch_end = samples[k][0] + meters
                finish_moving = prev_moving + (end_moving - prev_moving) * (stretch_end - prev_meters) / (end_meters - prev_meters)
                self._consider_effort(key, meters, finish_moving - samples[k][1], samples[k][0], samples[k][2], end_path)
                k += 1
            self.effort_next_start[key] = k

    def best_efforts(self):
        """This run's best effort at every distance it has covered, shortest distance first."""
        out = []
        for key, name, meters in BEST_EFFORT_DISTANCES:
            effort = self.efforts.get(key)
            if effort:
                seconds, start_meters, path_start, path_end = effort
                out.append({
                    "key": key, "name": name, "seconds": round(seconds, 1),
                    "time_display": format_duration(seconds),
                    "pace_display": format_pace(seconds / (meters / MILE_METERS)),
                    "start_meters": round(start_meters, 1),
                    "path_start": path_start, "path_end": path_end,  # the stretch on the map path
                })
        return out

    def _add_altitude(self, altitude, step_meters, timestamp):
        self.elev_track_meters += step_meters
        if altitude is None:
            return
        self.elev_pending.append(
            (timestamp.timestamp(), self.elev_track_meters, self.cumulative_meters, float(altitude), len(self.path) - 1)
        )
        self._drop_altitude_glitches(final=False)

    def _drop_altitude_glitches(self, final):
        """Check every pending reading whose window (or, at the end of the
        run, every remaining one) is complete; pass the good ones on to smoothing."""
        pending, window = self.elev_pending, ALTITUDE_GLITCH_WINDOW_SECONDS
        passed = False
        while self.elev_checked < len(pending):
            k = self.elev_checked
            seconds, track, run_meters, altitude, path_index = pending[k]
            if not final and pending[-1][0] < seconds + window:
                break  # readings from the next half-minute haven't all arrived yet
            nearby = [altitude]
            j = k - 1
            while j >= 0 and pending[j][0] >= seconds - window:
                nearby.append(pending[j][3])
                j -= 1
            j = k + 1
            while j < len(pending) and pending[j][0] <= seconds + window:
                nearby.append(pending[j][3])
                j += 1
            nearby.sort()
            median = nearby[len(nearby) // 2]
            if abs(altitude - median) <= ALTITUDE_GLITCH_METERS:
                self.elev_samples.append((track, run_meters, altitude, path_index))
                passed = True
            self.elev_checked += 1
        if passed or final:
            self._smooth_altitudes(final=final)

    def _smooth_altitudes(self, final):
        """Smooth every sample whose full window is now known (or, at the end
        of the run, every remaining one) and feed it to the climb counter."""
        samples, half = self.elev_samples, ELEVATION_SMOOTHING_METERS / 2
        lo, hi, total = self.elev_window
        while len(self.elev_profile) < len(samples):
            track, run_meters, _, path_index = samples[len(self.elev_profile)]
            if not final and samples[-1][0] < track + half:
                break  # the window ahead of this sample isn't complete yet
            while hi < len(samples) and samples[hi][0] <= track + half:
                total += samples[hi][2]
                hi += 1
            while samples[lo][0] < track - half:
                total -= samples[lo][2]
                lo += 1
            altitude = total / (hi - lo)
            self.climb.add(altitude)
            self.elev_profile.append((run_meters, altitude, self.climb.gain, self.climb.loss, path_index))
        self.elev_window = [lo, hi, total]

    def _elevation_at(self, run_meters):
        """(gain so far m, loss so far m, altitude m) at the last smoothed point at or before run_meters."""
        lo, hi = 0, len(self.elev_profile)  # binary search - run meters only grow along the profile
        while lo < hi:
            mid = (lo + hi) // 2
            if self.elev_profile[mid][0] <= run_meters:
                lo = mid + 1
            else:
                hi = mid
        index = lo - 1
        if index < 0:
            return 0.0, 0.0, None
        _, altitude, gain, loss, _ = self.elev_profile[index]
        return gain, loss, altitude

    def _elevation_between(self, start_meters, end_meters):
        """{gain, loss in feet} between two points of the run (None values if not smoothed that far yet)."""
        if not self.elev_profile or self.elev_profile[-1][0] < end_meters:
            return {"elevation_gain_ft": None, "elevation_loss_ft": None}
        gain_start, loss_start, _ = self._elevation_at(start_meters)
        gain_end, loss_end, _ = self._elevation_at(end_meters)
        return {
            "elevation_gain_ft": round((gain_end - gain_start) * FEET_PER_METER),
            "elevation_loss_ft": round((loss_end - loss_start) * FEET_PER_METER),
        }

    def _elevation_profile_for_chart(self, max_points=300):
        """[[miles, feet, path index], ...], thinned evenly to at most max_points."""
        profile = self.elev_profile
        if len(profile) > max_points:
            step = (len(profile) - 1) / (max_points - 1)
            profile = [profile[round(i * step)] for i in range(max_points)]
        return [[round(p[0] / MILE_METERS, 3), round(p[1] * FEET_PER_METER), p[4]] for p in profile]

    def recent_pace(self, window_meters):
        """Moving-time pace (seconds per mile) over roughly the last
        window_meters of the run, or None until that much has been covered."""
        if not self.progress:
            return None
        meters_now, moving_now = self.progress[-1]
        for meters_then, moving_then in reversed(self.progress):
            covered = meters_now - meters_then
            if covered >= window_meters:
                return (moving_now - moving_then) / (covered / MILE_METERS)
        return None

    def _snap_to_path(self, lat, lon):
        """The closest spot on the recent part of the drawn (smoothed) path, so
        a mile marker sits on the line instead of a few meters off it.

        Returns (lat, lon, path_end): path_end is how many path points come
        before that spot, so path[:path_end] + [spot] is the route up to it.
        """
        recent_start = max(0, len(self.path) - 6)
        recent = self.path[recent_start:]
        if len(recent) < 2:
            return lat, lon, len(self.path)
        # Flat-earth approximation - fine over the few tens of meters involved.
        scale = math.cos(math.radians(lat))
        best, best_dist = None, float("inf")
        for i, ((lat1, lon1), (lat2, lon2)) in enumerate(zip(recent, recent[1:])):
            dx, dy = (lon2 - lon1) * scale, lat2 - lat1
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (((lon - lon1) * scale) * dx + (lat - lat1) * dy) / length_sq))
            p_lat, p_lon = lat1 + dy * t, lon1 + (lon2 - lon1) * t
            dist = ((p_lon - lon) * scale) ** 2 + (p_lat - lat) ** 2
            if dist < best_dist:
                best, best_dist = (p_lat, p_lon, recent_start + i + 1), dist
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
            "started_at": self.start_time.isoformat() if self.start_time else None,  # first GPS point
            "timezone": self.timezone,
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
                    # The mile's stretch of the map path: the previous marker,
                    # path[previous path_end:path_end], then this marker.
                    "path_end": s["path_end"],
                    "at_display": format_duration(s["crossing_moving"]),  # moving time when the mile was finished
                    # Filled in once smoothing has passed the mile mark (~40 m after it).
                    **self._elevation_between((s["mile"] - 1) * MILE_METERS, s["mile"] * MILE_METERS),
                }
                for s in self.splits
            ],
            "fastest_split": {"mile": fastest["mile"], "pace_display": fastest["pace_display"]} if fastest else None,
            "partial_split": {
                "distance_miles": round(partial_distance_miles, 2),
                "pace_seconds": round(partial_pace_seconds, 1),
                "pace_display": format_pace(partial_pace_seconds),
                **self._elevation_between((self.next_split_mile - 1) * MILE_METERS, self.cumulative_meters),
            } if partial_pace_seconds else None,
            "path": [[lat, lon] for lat, lon in self.path],
            # Total climbing/descending so far (None until there's altitude data).
            "elevation_gain_ft": round(self.climb.gain * FEET_PER_METER) if self.elev_profile else None,
            "elevation_loss_ft": round(self.climb.loss * FEET_PER_METER) if self.elev_profile else None,
            "elevation_profile": self._elevation_profile_for_chart(),
            "best_efforts": self.best_efforts(),
        }

    def end(self):
        self._drop_altitude_glitches(final=True)  # the last ~30 s / ~40 m had no window ahead of them yet
        summary = self.current_stats()
        summary["active"] = False
        summary["paused"] = False
        # Instantaneous pace of the last few steps (usually slowing to a stop)
        # is meaningless once the run is over.
        summary["current_pace_display"] = None
        summary["current_pace_seconds"] = None
        self.reset()
        return summary

    def process_point(self, lat, lon, timestamp, accuracy, speed=None, altitude=None):
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
        raw_distance = 0.0
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
        self._add_altitude(altitude, raw_distance, timestamp)  # every accepted point, moving or not

        if self.distance_anchor is None:
            self.distance_anchor = (lat, lon, timestamp)
            self.effort_samples.append((0.0, 0.0, len(self.path) - 1))  # efforts can start at the very start
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
        self.progress.append((self.cumulative_meters, self.moving_seconds))
        self.effort_samples.append((self.cumulative_meters, self.moving_seconds, len(self.path) - 1))
        self._update_efforts()

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
            crossing_lat, crossing_lon, path_end = self._snap_to_path(
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
                "path_end": path_end,
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
