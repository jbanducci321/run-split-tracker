import hmac
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from datetime import timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

import db
import map_image
import weather
from discord_notifier import send_dm
from split_tracker import (
    ALGORITHM_VERSION, MILE_METERS, RunTracker, format_duration, format_pace, parse_timestamp,
)

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

# Restart recovery: resume a run in progress from its saved points after the
# app restarts mid-run. Opt-in, and only meant for the deployed (Railway)
# copy - a local copy shares the same database and must never take over a
# live run.
RECOVERY_ENABLED = PERSISTENCE_ENABLED and os.environ.get("RECOVER_ACTIVE_RUNS", "").lower() == "true"
RECOVERY_WINDOW_SECONDS = 120  # run's first point vs. the Overland trip's start time
logger.info("Restart recovery %s", "enabled" if RECOVERY_ENABLED else "disabled")

app = Flask(__name__)

OVERLAND_ACCESS_TOKEN = os.environ.get("OVERLAND_ACCESS_TOKEN", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# TEST_MODE_INTERVAL_SECONDS: sends a DM every N seconds instead of only at
# real checkpoints - useful for validating pace math in small increments
# without a full run.
TEST_MODE_INTERVAL_SECONDS = int(os.environ.get("TEST_MODE_INTERVAL_SECONDS", "15"))

MAX_GOAL_MILES = 99.99
MIN_TARGET_PACE_SECONDS = 60             # 1:00/mi - the admin dropdowns offer 1-10 minutes, 0-59 seconds
MAX_TARGET_PACE_SECONDS = 10 * 60 + 59   # 10:59/mi

# Live, password-changeable settings, saved to rst_settings so they survive
# restarts. These values (and the DISCORD_TEST_MODE env var) are only the
# defaults until anything has been saved.
admin_state = {
    "discord_test_mode": os.environ.get("DISCORD_TEST_MODE", "false").lower() == "true",
    "goal_distance_miles": None,
    "target_pace_enabled": False,
    "target_pace_seconds": None,  # kept while disabled, so toggling back on restores it
    "six_seven_enabled": False,
    "six_seven_test_mode": False,  # sends the 6.7 DM to you instead of SIX_SEVEN_DISCORD_ID
}

# Once per trip, the first time it passes 6.7 miles, DM SIX_SEVEN_DISCORD_ID
# (a friend) a message plus a picture of the route so far.
SIX_SEVEN_MILES = 6.7
RUNNER_NAME = "Jacob"


def public_site_url():
    """The dashboard's public address: PUBLIC_SITE_URL if set (e.g. a custom
    domain), else the domain Railway sets automatically. None when running locally."""
    url = os.environ.get("PUBLIC_SITE_URL")
    if url:
        return url.rstrip("/")
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else None

# The database is shared by the deployed app and any local copy, so each
# saves its settings under its own scope - a goal set while testing locally
# can't change the live settings. Railway names its environment
# ("production"); anywhere else defaults to "local".
SETTINGS_SCOPE = os.environ.get("SETTINGS_SCOPE") or os.environ.get("RAILWAY_ENVIRONMENT_NAME") or "local"
settings_state = {"loaded": False, "changed": False, "last_attempt": float("-inf")}

tracker = RunTracker()
last_summary = None  # most recently finished trip, kept until a new one starts
last_test_dm_time = None  # timestamp (from GPS data, not wall clock) of the last test-mode DM
trip_announced = False  # whether this trip's "Tracking started" DM has gone out
announce_thread = None


def effective_target_pace():
    return admin_state["target_pace_seconds"] if admin_state["target_pace_enabled"] else None


def six_seven_message(average_pace_display):
    goal = admin_state["goal_distance_miles"]
    goal_part = f" out of {goal:g} miles" if goal else ""
    message = f"{RUNNER_NAME} has run {SIX_SEVEN_MILES:g} miles{goal_part} in {average_pace_display or 'an unknown pace'}"
    site = public_site_url()
    if site:
        # Angle brackets keep the link clickable but stop Discord adding a
        # preview card next to the route image.
        message += f"\n\nYou can see the current progress here: <{site}>"
    return message


def send_six_seven(message, path, recipient_id, who):
    """Render the route map and send the 6.7 DM. Slow (map tiles), so callers usually run it in a thread."""
    image = map_image.render_route_png(path)  # logs its own failure; the DM still goes out without it
    sent = send_dm(message, user_id=recipient_id, image_png=image, label=f"{who} [6.7 DM]")
    return sent, image is not None


def setting_key(name):
    return f"{SETTINGS_SCOPE}:{name}"


def clean_setting(name, value):
    """Re-validate a saved setting (it may have been edited by hand in the database)."""
    if name in ("discord_test_mode", "target_pace_enabled", "six_seven_enabled", "six_seven_test_mode"):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if name == "goal_distance_miles":
        return validate_goal_distance(value)
    return None if value is None else validate_target_pace(value)


def load_settings():
    """Restore saved admin settings. Retries (at most once a minute) if the
    database was unreachable, but only until something is changed in the
    admin panel - after that, the in-memory values are the newest ones."""
    if not PERSISTENCE_ENABLED or settings_state["loaded"] or settings_state["changed"]:
        return
    if time.monotonic() - settings_state["last_attempt"] < CHUNK_FLUSH_SECONDS:
        return
    settings_state["last_attempt"] = time.monotonic()
    try:
        stored = db.load_settings([setting_key(name) for name in admin_state])
    except Exception:
        logger.exception("Settings: couldn't load saved settings - using defaults, will retry")
        return

    restored = {}
    for name in admin_state:
        raw = stored.get(setting_key(name))
        if raw is None:
            continue
        try:
            restored[name] = clean_setting(name, json.loads(raw))
        except (ValueError, TypeError):
            logger.warning("Settings: ignoring invalid saved %s = %r", name, raw)
    admin_state.update(restored)
    if admin_state["target_pace_enabled"] and admin_state["target_pace_seconds"] is None:
        admin_state["target_pace_enabled"] = False
    settings_state["loaded"] = True
    logger.info("Settings: loaded %s (scope %r)", restored or "nothing saved yet", SETTINGS_SCOPE)


def persist_settings(updates):
    """Save changed settings. Returns True/False, or None when saving is off."""
    if not PERSISTENCE_ENABLED or not updates:
        return None
    settings_state["changed"] = True
    try:
        db.save_settings({setting_key(name): json.dumps(value) for name, value in updates.items()})
    except Exception:
        logger.exception("Settings: couldn't save %s - applied until the next restart only", sorted(updates))
        return False
    logger.info("Settings: saved %s", updates)
    return True


def target_note(pace_seconds):
    """' (8s faster than target)' style suffix for DMs, or '' with no target set."""
    target = effective_target_pace()
    if target is None or pace_seconds is None:
        return ""
    diff = round(pace_seconds - target)
    if diff == 0:
        return " (on target)"
    gap = f"{abs(diff)}s" if abs(diff) < 60 else f"{abs(diff) // 60}:{abs(diff) % 60:02d}"
    return f" ({gap} {'slower' if diff > 0 else 'faster'} than target)"


def announce_trip_start(run_id, lat, lon, goal, target):
    """Background: fetch start weather, send the 'Tracking started' DM, save the conditions."""
    conditions = weather.fetch_current_conditions(lat, lon)
    message = "Tracking started"
    if conditions:
        message += f" - {weather.describe(conditions)}"
    if goal:
        message += f". Goal: {goal:.2f} mi"
    if target:
        message += f". Target pace: {format_pace(target)}"
    send_dm(message)

    if not (PERSISTENCE_ENABLED and conditions):
        return
    if run_id is None:
        logger.warning("Weather: not saved - the run row wasn't created yet")
        return
    try:
        db.save_conditions(run_id, conditions)
        logger.info("DB: run %s start conditions saved", run_id)
    except Exception:
        logger.exception("DB: run %s start conditions couldn't be saved", run_id)

# Locations Overland sends while no trip is active (only happens with its
# always-on tracking turned on). Memory only, never saved, never part of a
# run - used solely by the 6.7 test button, so the map can be checked
# without starting a trip.
RECENT_LOCATION_MAX_AGE_SECONDS = 10 * 60
recent_locations = deque(maxlen=60)  # (lat, lon, monotonic time received)

# Database state for the trip in progress.
recording = {"run_id": None, "buffer": [], "chunk_index": 0, "last_db_attempt": float("-inf")}

# Overland sends a point every few seconds; instead of a log line per update,
# tally them and log one summary about once a minute.
PING_SUMMARY_SECONDS = 60
ping_window = {"started": time.monotonic(), "updates": 0, "points": 0, "outcomes": Counter()}


def log_ping_summary(force=False):
    """One log line per ~minute: updates and points received, what happened to
    each point (counted, or why it was skipped), and progress so far."""
    elapsed = time.monotonic() - ping_window["started"]
    if ping_window["updates"] == 0 or (elapsed < PING_SUMMARY_SECONDS and not force):
        return
    outcomes = ", ".join(f"{n} {name}" for name, n in ping_window["outcomes"].most_common()) or "none usable"
    progress = ""
    if tracker.active:
        stats = tracker.current_stats()
        pace = "PAUSED" if stats["paused"] else f"pace {stats['current_pace_display'] or '-'}"
        progress = f" | {stats['distance_miles']:.2f} mi, moving {stats['moving_display']}, {pace}"
    logger.info(
        "Pings (last %ds): %d updates, %d points - %s%s",
        round(elapsed), ping_window["updates"], ping_window["points"], outcomes, progress,
    )
    ping_window.update(started=time.monotonic(), updates=0, points=0, outcomes=Counter())


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
            effective_target_pace(),
            admin_state["discord_test_mode"],
            ALGORITHM_VERSION,
        )
    except Exception:
        logger.exception("DB: couldn't create the run row - retrying in %ds", CHUNK_FLUSH_SECONDS)
        return
    logger.info("DB: run %s started", recording["run_id"])
    flush_points(force=True)  # get the first points saved now, so there's something to recover from

    if RECOVERY_ENABLED:
        # This is a fresh trip, so any run still marked active is one that
        # couldn't be resumed after a restart.
        try:
            stale = db.mark_other_active_runs_interrupted(recording["run_id"])
            if stale:
                logger.warning("DB: marked %d leftover active run(s) as interrupted", stale)
        except Exception:
            logger.exception("DB: couldn't mark leftover active runs as interrupted")


def tracker_input(props, lat, lon):
    """(lat, lon, timestamp, accuracy, speed) for RunTracker.process_point, or None if unusable."""
    timestamp_raw = props.get("timestamp")
    if not timestamp_raw or lat is None or lon is None:
        return None
    return lat, lon, parse_timestamp(timestamp_raw), props.get("horizontal_accuracy"), props.get("speed")


def replay_points(run_tracker, saved):
    """Feed a run's saved raw points through a tracker - straight in, never
    through the DM code, so nothing already announced gets announced again."""
    inputs = [tracker_input(p, p.get("lat"), p.get("lon")) for p in saved]
    for point in sorted((p for p in inputs if p), key=lambda p: p[2]):
        run_tracker.process_point(*point)


def restore_last_run():
    """At startup, put the most recent finished run back on the dashboard -
    otherwise every deploy/restart would blank it until the next run ends.
    Rebuilt from its saved raw points, so it's identical to what was shown."""
    global last_summary
    try:
        found = db.find_latest_completed_run()
        if not found:
            return
        run_id, test_mode = found
        saved, _ = db.load_points(run_id)
        rebuilt = RunTracker()
        replay_points(rebuilt, saved)
        summary = rebuilt.end()
    except Exception:
        logger.exception("Startup: couldn't restore the last run to the dashboard")
        return
    summary.update(run_id=run_id, test_mode_used=bool(test_mode))
    # A trip that started (or ended) while this was loading is newer - keep it.
    if last_summary is None and not tracker.active:
        last_summary = summary
        logger.info("Startup: showing last run %s on the dashboard (%.2f mi)", run_id, summary["distance_miles"])


# Fastest full mile ever, from real runs only (see db.load_best_mile). Read
# from the database at startup and after each run - never per page refresh.
MIN_RECORD_PACE_SECONDS = 4 * 60  # a "mile" faster than 4:00 is bad data, not a record
best_mile = {"record": None, "loaded": False, "last_attempt": float("-inf")}


def refresh_best_mile():
    best_mile["last_attempt"] = time.monotonic()
    try:
        best_mile["record"] = db.load_best_mile(MIN_RECORD_PACE_SECONDS)
    except Exception:
        logger.exception("Records: couldn't load the fastest mile ever - will retry")
        return
    best_mile["loaded"] = True
    record = best_mile["record"]
    logger.info(
        "Records: fastest mile ever %s",
        f"{format_pace(record['pace_seconds'])} (run {record['run_id']}, mile {record['mile']})" if record else "- none yet",
    )


def run_in_background(target):
    threading.Thread(target=target, daemon=True).start()


def startup_from_database():
    restore_last_run()
    refresh_best_mile()


def recover_run(trip):
    """After a restart, resume the run for this same Overland trip from its saved points.

    Any failure or mismatch falls back to starting fresh - exactly what
    happens without recovery - so this can never leave things worse.
    """
    global trip_announced
    trip_start_raw = (trip or {}).get("start")
    if not RECOVERY_ENABLED or not trip_start_raw:
        return
    try:
        found = db.find_recoverable_run(to_utc_naive(parse_timestamp(trip_start_raw)), RECOVERY_WINDOW_SECONDS)
        if not found:
            return
        run_id, goal, target, test_mode = found
        saved, next_chunk = db.load_points(run_id)
        replay_points(tracker, saved)
    except Exception:
        logger.exception("Recovery: failed - starting this trip fresh")
        tracker.reset()
        return

    recording.update(run_id=run_id, buffer=[], chunk_index=next_chunk, last_db_attempt=time.monotonic())
    # Saved settings are the newest (they include any mid-run changes). Only
    # if they couldn't be loaded, fall back to what the run row recorded at
    # the start of this trip.
    if not settings_state["loaded"]:
        admin_state["goal_distance_miles"] = float(goal) if goal is not None else None
        admin_state["discord_test_mode"] = bool(test_mode)
        if target is not None:
            admin_state.update(target_pace_seconds=int(target), target_pace_enabled=True)
    distance = tracker.cumulative_meters / MILE_METERS
    goal = admin_state["goal_distance_miles"]
    tracker.goal_half_notified = bool(goal) and distance >= goal / 2
    tracker.goal_notified = bool(goal) and distance >= goal
    tracker.test_mode_used = tracker.test_mode_used or bool(test_mode)
    trip_announced = True
    try:
        db.set_algorithm_version(run_id, ALGORITHM_VERSION)  # replay just recomputed it with this version
    except Exception:
        logger.exception("Recovery: couldn't update run %s algorithm_version", run_id)
    logger.warning(
        "Recovery: resumed run %s after a restart - replayed %d saved points (%.2f mi, %d splits)",
        run_id, len(saved), distance, len(tracker.splits),
    )


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
        db.finish_run(run_id, to_utc_naive(ended_at), summary, goal_reached, summary["test_mode_used"])
        logger.info("DB: run %s completed (%.2f mi, %d splits)", run_id, summary["distance_miles"], len(summary["splits"]))
    except Exception:
        logger.exception("DB: run %s couldn't be marked completed - raw points are saved, summary is not", run_id)
    stop_recording()
    run_in_background(refresh_best_mile)  # this run may hold the new record


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


def validate_target_pace(value):
    """Target pace in whole seconds per mile, 1:00-10:59, or raises ValueError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("target pace must be a whole number of seconds")
    if not MIN_TARGET_PACE_SECONDS <= value <= MAX_TARGET_PACE_SECONDS:
        raise ValueError("target pace must be between 1:00 and 10:59 per mile")
    return value


def record_status(splits, run_id, counts_for_records):
    """(the fastest mile ever, for the dashboard card; the mile of the shown run to star, or None).

    The shown run's fastest mile takes the record if it beats the saved one
    (strictly - a tie stays with whoever set it first), or if it already is
    the saved one. Test-mode runs never count.
    """
    saved = best_mile["record"]
    eligible = [s for s in splits if s["pace_seconds"] >= MIN_RECORD_PACE_SECONDS] if counts_for_records else []
    fastest = min(eligible, key=lambda s: s["pace_seconds"], default=None)  # earliest mile wins a tie
    if fastest and (
        saved is None
        or fastest["pace_seconds"] < saved["pace_seconds"]
        or (saved["run_id"] == run_id and saved["mile"] == fastest["mile"])
    ):
        return {"pace_display": fastest["pace_display"], "this_run": True, "mile": fastest["mile"], "date_display": None}, fastest["mile"]
    if saved:
        local_start = saved["started_at"] + timedelta(minutes=saved["utc_offset_minutes"] or 0)
        return {
            "pace_display": format_pace(saved["pace_seconds"]),
            "this_run": False,
            "mile": saved["mile"],
            "date_display": f"{local_start:%b} {local_start.day}, {local_start.year}",
        }, None
    return None, None


def build_status_payload(stats, run_id=None, counts_for_records=False):
    goal = admin_state["goal_distance_miles"]
    stats = dict(stats)
    stats.pop("run_id", None)
    stats.pop("test_mode_used", None)
    stats["record"], stats["record_mile"] = record_status(stats["splits"], run_id, counts_for_records)
    stats["target_pace_seconds"] = effective_target_pace()
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
    if PERSISTENCE_ENABLED and not best_mile["loaded"] and time.monotonic() - best_mile["last_attempt"] >= CHUNK_FLUSH_SECONDS:
        best_mile["last_attempt"] = time.monotonic()
        run_in_background(refresh_best_mile)  # the startup load failed - retry, at most once a minute

    if tracker.active:
        stats, run_id, test_mode_used = tracker.current_stats(), recording["run_id"], tracker.test_mode_used
    elif last_summary:
        stats, run_id, test_mode_used = last_summary, last_summary.get("run_id"), last_summary.get("test_mode_used", True)
    else:
        stats, run_id, test_mode_used = tracker.current_stats(), None, True
    return jsonify(build_status_payload(stats, run_id, counts_for_records=not test_mode_used))


@app.post("/admin/reset")
def admin_reset():
    global last_summary, last_test_dm_time, trip_announced

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
    trip_announced = False
    return jsonify(result="ok")


@app.post("/admin/settings")
def admin_settings():
    data = request.get_json(silent=True) or {}
    if not is_admin_password_correct(data.get("password", "")):
        return jsonify(error="unauthorized"), 401

    load_settings()  # no-op once loaded; retries if the database was down at startup

    # Validate everything first so a bad value can't leave settings half-applied.
    updates = {}
    try:
        if "discord_test_mode" in data:
            updates["discord_test_mode"] = bool(data["discord_test_mode"])
        if "goal_distance_miles" in data:
            updates["goal_distance_miles"] = validate_goal_distance(data["goal_distance_miles"])
        if "target_pace_seconds" in data:
            updates["target_pace_seconds"] = validate_target_pace(data["target_pace_seconds"])
        if "target_pace_enabled" in data:
            updates["target_pace_enabled"] = bool(data["target_pace_enabled"])
            if updates["target_pace_enabled"] and updates.get(
                "target_pace_seconds", admin_state["target_pace_seconds"]
            ) is None:
                raise ValueError("pick a target pace before enabling it")
        for name in ("six_seven_enabled", "six_seven_test_mode"):
            if name in data:
                updates[name] = bool(data[name])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    admin_state.update(updates)
    saved = persist_settings(updates)

    return jsonify(
        discord_test_mode=admin_state["discord_test_mode"],
        goal_distance_miles=admin_state["goal_distance_miles"],
        target_pace_enabled=admin_state["target_pace_enabled"],
        target_pace_seconds=admin_state["target_pace_seconds"],
        six_seven_enabled=admin_state["six_seven_enabled"],
        six_seven_test_mode=admin_state["six_seven_test_mode"],
        six_seven_configured=bool(os.environ.get("SIX_SEVEN_DISCORD_ID")),
        saved=saved,  # False = applied, but couldn't be stored (resets on restart)
    )


@app.post("/admin/six-seven-test")
def admin_six_seven_test():
    """Send the 6.7 DM to you right now (never to the friend), so it can be
    checked without a run. The text uses the current or last run's pace; the
    map uses, in order: the trip in progress, your location from outside a
    trip (Overland's always-on tracking, last 10 min), or the last run."""
    data = request.get_json(silent=True) or {}
    if not is_admin_password_correct(data.get("password", "")):
        return jsonify(error="unauthorized"), 401

    stats = tracker.current_stats() if tracker.active else (last_summary or tracker.current_stats())
    cutoff = time.monotonic() - RECENT_LOCATION_MAX_AGE_SECONDS
    recent = [(lat, lon) for lat, lon, received in recent_locations if received >= cutoff]
    if tracker.active:
        path, map_source = [tuple(p) for p in stats["path"]], "current run"
    elif recent:
        path, map_source = recent, "location (no trip active)"
    else:
        path, map_source = [tuple(p) for p in stats["path"]], "last run"
    if not path:
        map_source = None

    sent, with_map = send_six_seven(
        six_seven_message(stats["average_pace_display"]), path, None, f"you (test button, map: {map_source or 'none'})",
    )
    if not sent:
        return jsonify(error="the DM couldn't be sent - check the logs"), 502
    return jsonify(sent=True, with_map=with_map, map_source=map_source)


@app.post("/overland")
def receive_overland_batch():
    global last_summary, last_test_dm_time, trip_announced, announce_thread

    if not is_authorized(request.headers.get("Authorization", "")):
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or {}
    locations = payload.get("locations", [])
    trip_active = bool(payload.get("trip"))

    if not trip_active:
        for feature in locations:  # kept only for the 6.7 test button's map
            lon, lat = (feature.get("geometry", {}).get("coordinates") or [None, None])[:2]
            if lat is not None and lon is not None:
                recent_locations.append((lat, lon, time.monotonic()))
        if tracker.active:
            log_ping_summary(force=True)  # the trip's last partial minute
            trip_announced = False
            ended_at, goal_reached, test_mode_used = tracker.last_seen_time, tracker.goal_notified, tracker.test_mode_used
            last_summary = tracker.end()
            last_summary.update(run_id=recording["run_id"], test_mode_used=test_mode_used)
            logger.info(
                "Trip ended. Distance=%.2fmi Splits=%s",
                last_summary["distance_miles"],
                [s["pace_display"] for s in last_summary["splits"]],
            )
            if PERSISTENCE_ENABLED:
                finish_recording(last_summary, ended_at, goal_reached)
        return jsonify(result="ok")

    if not tracker.active and recording["run_id"] is None:
        load_settings()  # a trip is starting - make sure its goal/target are the saved ones
        recover_run(payload.get("trip"))  # no-op unless this trip was cut off by a restart

    if PERSISTENCE_ENABLED:
        recording["buffer"].extend(db.raw_point(feature) for feature in locations)

    points = []
    for feature in locations:
        lon, lat = (feature.get("geometry", {}).get("coordinates") or [None, None])[:2]
        point = tracker_input(feature.get("properties", {}), lat, lon)
        if point:
            points.append(point)
        else:
            ping_window["outcomes"]["missing time/location"] += 1
    if ping_window["updates"] == 0:
        ping_window["started"] = time.monotonic()  # window starts at its first update, not at idle time
    ping_window["updates"] += 1
    ping_window["points"] += len(locations)

    points.sort(key=lambda p: p[2])

    # Save right away after any point that triggered a checkpoint DM, so a
    # restart can't replay short of it and send that DM a second time.
    dm_checkpoint_hit = False

    for lat, lon, timestamp, accuracy, speed in points:
        meters_before = tracker.cumulative_meters if tracker.active else 0.0
        event = tracker.process_point(lat, lon, timestamp, accuracy, speed)
        if admin_state["discord_test_mode"]:
            tracker.test_mode_used = True  # on at any point = not a real run, for records
        ping_window["outcomes"][tracker.last_outcome] += 1
        dm_checkpoint_hit = dm_checkpoint_hit or event is not None

        # 6.7 DM: fires on the crossing itself, so it happens once per trip
        # (distance only grows) and switching it on after 6.7 doesn't fire it.
        six_seven_meters = SIX_SEVEN_MILES * MILE_METERS
        if admin_state["six_seven_enabled"] and meters_before < six_seven_meters <= tracker.cumulative_meters:
            dm_checkpoint_hit = True
            friend_id = os.environ.get("SIX_SEVEN_DISCORD_ID")
            if admin_state["six_seven_test_mode"]:
                recipient, who = None, "you (6.7 test mode)"
            elif friend_id:
                recipient, who = friend_id, "SIX_SEVEN_DISCORD_ID"
            else:
                recipient = who = None
                logger.warning("6.7 DM skipped - SIX_SEVEN_DISCORD_ID isn't set (turn on 6.7 test mode to send it to yourself)")
            if who:
                message = six_seven_message(tracker.current_stats()["average_pace_display"])
                threading.Thread(
                    target=send_six_seven, args=(message, list(tracker.path), recipient, who), daemon=True,
                ).start()

        if admin_state["discord_test_mode"]:
            due = (
                last_test_dm_time is None
                or (timestamp - last_test_dm_time).total_seconds() >= TEST_MODE_INTERVAL_SECONDS
            )
            if due:
                stats = tracker.current_stats()
                pace = stats["current_pace_display"]  # None while paused
                if pace:
                    logger.info("TEST MODE update: %s", pace)
                    send_dm(f"Pace: {pace}{target_note(stats['current_pace_seconds'])}")
                    last_test_dm_time = timestamp
        elif event:
            if event["type"] == "split":
                logger.info("MILE %d SPLIT: %s", event["mile"], event["pace_display"])
                send_dm(f"Mile {event['mile']} - Pace: {event['pace_display']}{target_note(event['pace_seconds'])}")
            else:
                stats = tracker.current_stats()
                logger.info("Halfway checkpoint at mile %.1f: avg pace %s", event["mile"], stats["average_pace_display"])
                if stats["average_pace_display"]:
                    send_dm(f"Pace: {stats['average_pace_display']}{target_note(stats['average_pace_seconds'])}")

        goal = admin_state["goal_distance_miles"]
        distance_miles = tracker.cumulative_meters / MILE_METERS
        if goal and not tracker.goal_half_notified and distance_miles >= goal / 2:
            tracker.goal_half_notified = True
            dm_checkpoint_hit = True
            stats = tracker.current_stats()
            logger.info("GOAL HALFWAY: %.2f / %.2f mi", goal / 2, goal)
            send_dm(
                f"Halfway to goal! {goal / 2:.2f} / {goal:.2f} mi - Avg pace: {stats['average_pace_display']}"
                f"{target_note(stats['average_pace_seconds'])}"
            )
        if goal and not tracker.goal_notified and distance_miles >= goal:
            tracker.goal_notified = True
            dm_checkpoint_hit = True
            logger.info("GOAL REACHED: %.2f mi", goal)
            send_dm(f"Goal reached! {goal:.2f} mi")

    if PERSISTENCE_ENABLED and tracker.active:
        ensure_run_recorded()
        flush_points(force=dm_checkpoint_hit)

    # First accepted point of a new trip: announce it (weather lookup runs in
    # the background so it can't slow down Overland's request).
    if tracker.active and not trip_announced and tracker.path:
        trip_announced = True
        start_lat, start_lon = tracker.path[0]
        announce_thread = threading.Thread(
            target=announce_trip_start,
            args=(recording["run_id"], start_lat, start_lon, admin_state["goal_distance_miles"], effective_target_pace()),
            daemon=True,
        )
        announce_thread.start()

    log_ping_summary()
    return jsonify(result="ok")


load_settings()
if PERSISTENCE_ENABLED:
    # Background, so a slow database can't hold up the app starting.
    best_mile["last_attempt"] = time.monotonic()  # /status shouldn't start a second load meanwhile
    run_in_background(startup_from_database)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
