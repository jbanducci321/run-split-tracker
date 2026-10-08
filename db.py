import gzip
import json
import logging
import os
import threading

import pymysql

logger = logging.getLogger("run-split-tracker")

REQUIRED_ENV = ("DB_HOST", "DB_NAME", "DB_USERNAME", "DB_PASSWORD")

# Raw point fields saved in rst_point_chunks, in this order. The list is
# stored inside every chunk too, so old chunks stay readable if fields are
# added later.
POINT_FIELDS = [
    "timestamp", "lat", "lon", "horizontal_accuracy", "vertical_accuracy", "altitude",
    "speed", "speed_accuracy", "course", "course_accuracy", "motion",
]

_lock = threading.Lock()
_conn = None


def is_configured():
    return all(os.environ.get(key) for key in REQUIRED_ENV)


def connect():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=15,
        write_timeout=15,
    )


def _transaction(work):
    """Run work(cursor) as one transaction on the shared connection."""
    global _conn
    with _lock:
        # RDS closes connections left idle past its wait_timeout, so check the
        # connection is still alive (and reconnect if not) before every use.
        if _conn is None:
            _conn = connect()
        else:
            _conn.ping(reconnect=True)
        try:
            with _conn.cursor() as cur:
                result = work(cur)
            _conn.commit()
            return result
        except Exception:
            try:
                _conn.rollback()
            except Exception:
                _conn = None  # connection itself is broken; open a fresh one next time
            raise


def raw_point(feature):
    """One Overland GeoJSON feature -> a row of POINT_FIELDS values, unfiltered."""
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    location = {
        "lon": coords[0] if len(coords) > 0 else None,
        "lat": coords[1] if len(coords) > 1 else None,
    }
    return [location[f] if f in location else props.get(f) for f in POINT_FIELDS]


def encode_points(points):
    payload = {"fields": POINT_FIELDS, "points": points}
    return gzip.compress(json.dumps(payload, separators=(",", ":")).encode())


def decode_points(blob):
    return json.loads(gzip.decompress(blob))


def create_run(started_at, utc_offset_minutes, goal_distance_miles, target_pace_seconds, test_mode, algorithm_version):
    def work(cur):
        cur.execute(
            "INSERT INTO rst_runs (status, started_at, utc_offset_minutes, goal_distance_miles,"
            " target_pace_seconds, test_mode, algorithm_version) VALUES ('active', %s, %s, %s, %s, %s, %s)",
            (started_at, utc_offset_minutes, goal_distance_miles, target_pace_seconds, int(test_mode), algorithm_version),
        )
        return cur.lastrowid
    return _transaction(work)


def save_point_chunk(run_id, chunk_index, points):
    # ON DUPLICATE KEY makes a retry after an ambiguous failure harmless.
    _transaction(lambda cur: cur.execute(
        "INSERT INTO rst_point_chunks (run_id, chunk_index, point_count, points_gz)"
        " VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE run_id = run_id",
        (run_id, chunk_index, len(points), encode_points(points)),
    ))


MYSQL_UNKNOWN_COLUMN = 1054


def finish_run(run_id, ended_at, summary, goal_reached, test_mode_used):
    splits = [(run_id, s["mile"], 1.0, s["pace_seconds"], 0, s.get("lat"), s.get("lon")) for s in summary["splits"]]
    partial = summary.get("partial_split")
    if partial:
        splits.append((run_id, len(summary["splits"]) + 1, partial["distance_miles"], partial["pace_seconds"], 1, None, None))

    def work(cur):
        cur.execute(
            "UPDATE rst_runs SET status = 'completed', ended_at = %s, distance_miles = %s,"
            " moving_seconds = %s, elapsed_seconds = %s, avg_pace_seconds = %s, goal_reached = %s,"
            " test_mode = GREATEST(test_mode, %s)"  # test mode switched on mid-run still marks the run
            " WHERE id = %s",
            (
                ended_at,
                summary["distance_miles"],
                round(summary["moving_seconds"]),
                round(summary["elapsed_seconds"]),
                summary["average_pace_seconds"],
                int(goal_reached),
                int(test_mode_used),
                run_id,
            ),
        )
        if not splits:
            return
        try:
            cur.executemany(
                "INSERT INTO rst_splits (run_id, mile_number, distance_miles, pace_seconds, is_partial, lat, lon)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE pace_seconds = VALUES(pace_seconds),"
                " distance_miles = VALUES(distance_miles), lat = VALUES(lat), lon = VALUES(lon)",
                splits,
            )
        except pymysql.err.OperationalError as exc:
            if exc.args[0] != MYSQL_UNKNOWN_COLUMN:
                raise
            # rst_splits doesn't have the lat/lon columns yet - save the splits
            # without their locations rather than lose the run's summary.
            logger.warning("DB: rst_splits has no lat/lon columns yet - saving run %s splits without locations", run_id)
            cur.executemany(
                "INSERT INTO rst_splits (run_id, mile_number, distance_miles, pace_seconds, is_partial)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE pace_seconds = VALUES(pace_seconds),"
                " distance_miles = VALUES(distance_miles)",
                [row[:5] for row in splits],
            )
    _transaction(work)


def find_latest_completed_run():
    """(id, test_mode) of the most recently finished run, or None."""
    def work(cur):
        cur.execute(
            "SELECT id, test_mode FROM rst_runs WHERE status = 'completed'"
            " ORDER BY ended_at DESC, id DESC LIMIT 1"
        )
        return cur.fetchone()
    return _transaction(work)


def load_best_mile(min_pace_seconds):
    """The fastest full mile ever, from real runs only: completed, never in
    test mode, not manually excluded, and no faster than min_pace_seconds
    (anything quicker is treated as bad data, not a real mile).

    Returns a dict (pace_seconds, run_id, mile, started_at, utc_offset_minutes) or None.
    Ties go to the earlier run - whoever set the time first holds the record.
    """
    def work(cur):
        cur.execute(
            "SELECT s.pace_seconds, s.run_id, s.mile_number, r.started_at, r.utc_offset_minutes"
            " FROM rst_splits s JOIN rst_runs r ON r.id = s.run_id"
            " WHERE s.is_partial = 0 AND r.status = 'completed' AND r.test_mode = 0 AND r.excluded = 0"
            " AND s.pace_seconds >= %s"
            " ORDER BY s.pace_seconds ASC, r.started_at ASC, s.mile_number ASC LIMIT 1",
            (min_pace_seconds,),
        )
        return cur.fetchone()
    row = _transaction(work)
    if not row:
        return None
    pace, run_id, mile, started_at, offset = row
    return {
        "pace_seconds": float(pace), "run_id": run_id, "mile": mile,
        "started_at": started_at, "utc_offset_minutes": offset,
    }


CONDITION_COLUMNS = (
    "temperature_f", "feels_like_f", "humidity_pct", "wind_mph", "wind_direction_deg",
    "precipitation_in", "weather_code", "is_day",
)


def save_conditions(run_id, conditions):
    values = [conditions.get(c) for c in CONDITION_COLUMNS]

    def work(cur):
        cur.execute(
            f"INSERT INTO rst_run_conditions (run_id, {', '.join(CONDITION_COLUMNS)})"
            f" VALUES (%s, {', '.join(['%s'] * len(CONDITION_COLUMNS))})"
            " ON DUPLICATE KEY UPDATE run_id = run_id",
            (run_id, *values),
        )
        # Overland timestamps are UTC, so the weather lookup is where the
        # run's local time offset comes from.
        if conditions.get("utc_offset_minutes") is not None:
            cur.execute(
                "UPDATE rst_runs SET utc_offset_minutes = %s WHERE id = %s AND utc_offset_minutes IS NULL",
                (conditions["utc_offset_minutes"], run_id),
            )
        if conditions.get("timezone"):
            try:
                cur.execute(
                    "UPDATE rst_runs SET timezone = %s WHERE id = %s AND timezone IS NULL",
                    (conditions["timezone"], run_id),
                )
            except pymysql.err.OperationalError as exc:
                if exc.args[0] != MYSQL_UNKNOWN_COLUMN:
                    raise
                logger.warning("DB: rst_runs has no timezone column yet - run %s time zone not saved", run_id)
    _transaction(work)


def load_run_timezone(run_id):
    """A run's saved time zone name, or None (not saved, or no timezone column yet)."""
    def work(cur):
        cur.execute("SELECT timezone FROM rst_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return row[0] if row else None
    try:
        return _transaction(work)
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != MYSQL_UNKNOWN_COLUMN:
            raise
        return None


def load_settings(keys):
    """{setting_key: setting_value} for whichever of these keys are saved."""
    if not keys:
        return {}

    def work(cur):
        cur.execute(
            f"SELECT setting_key, setting_value FROM rst_settings WHERE setting_key IN ({', '.join(['%s'] * len(keys))})",
            tuple(keys),
        )
        return dict(cur.fetchall())
    return _transaction(work)


def save_settings(values):
    """Upsert {setting_key: setting_value} pairs."""
    _transaction(lambda cur: cur.executemany(
        "INSERT INTO rst_settings (setting_key, setting_value) VALUES (%s, %s)"
        " ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)",
        list(values.items()),
    ))


def set_run_status(run_id, status):
    _transaction(lambda cur: cur.execute("UPDATE rst_runs SET status = %s WHERE id = %s", (status, run_id)))


def find_recoverable_run(trip_start, window_seconds):
    """The still-'active' run that started within window_seconds of this Overland trip's start, if any.

    A run's started_at is its first point's timestamp, which lands seconds
    after the trip's own start - so a match means it's the same trip.
    """
    def work(cur):
        cur.execute(
            "SELECT id, goal_distance_miles, target_pace_seconds, test_mode FROM rst_runs"
            " WHERE status = 'active' AND source = 'tracker'"
            " AND started_at BETWEEN %s - INTERVAL %s SECOND AND %s + INTERVAL %s SECOND"
            " ORDER BY started_at DESC LIMIT 1",
            (trip_start, window_seconds, trip_start, window_seconds),
        )
        return cur.fetchone()
    return _transaction(work)


def load_points(run_id):
    """Every saved raw point for a run, in chunk order, as dicts - plus the next free chunk index."""
    def work(cur):
        cur.execute(
            "SELECT chunk_index, points_gz FROM rst_point_chunks WHERE run_id = %s ORDER BY chunk_index",
            (run_id,),
        )
        return cur.fetchall()
    rows = _transaction(work)
    points = []
    for _, blob in rows:
        data = decode_points(blob)
        points.extend(dict(zip(data["fields"], p)) for p in data["points"])
    return points, (rows[-1][0] + 1 if rows else 0)


def mark_other_active_runs_interrupted(keep_run_id):
    """Only one trip can be in progress, so any other 'active' run is a dead one. Returns how many were marked."""
    return _transaction(lambda cur: cur.execute(
        "UPDATE rst_runs SET status = 'interrupted' WHERE status = 'active' AND source = 'tracker' AND id <> %s",
        (keep_run_id,),
    ))


def set_algorithm_version(run_id, version):
    _transaction(lambda cur: cur.execute(
        "UPDATE rst_runs SET algorithm_version = %s WHERE id = %s", (version, run_id),
    ))
