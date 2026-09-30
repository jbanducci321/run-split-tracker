"""Checks the database connection and schema from your machine.

Run with:  python check_db.py

Every write it makes is rolled back at the end, so it leaves no data behind.
"""
import sys

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402  (needs the .env values loaded first)

TABLES = ["rst_runs", "rst_splits", "rst_point_chunks", "rst_run_conditions", "rst_settings"]


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


missing = [key for key in db.REQUIRED_ENV if not db.os.environ.get(key)]
step("Environment variables set", not missing, f"missing: {', '.join(missing)}" if missing else "")

try:
    conn = db.connect()
except Exception as exc:
    step("Connect", False, f"{type(exc).__name__}: {exc}\n"
         "       If phpMyAdmin works but this doesn't, your network may block port 3306.")
step("Connect", True)

try:
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        step("Query", True, f"MySQL {version}")

        cur.execute("SHOW TABLES LIKE 'rst\\_%%'")
        found = {row[0] for row in cur.fetchall()}
        absent = [t for t in TABLES if t not in found]
        step("Tables exist", not absent, f"missing: {', '.join(absent)}" if absent else ", ".join(TABLES))

        cur.execute(
            "INSERT INTO rst_runs (status, started_at, algorithm_version) VALUES ('active', UTC_TIMESTAMP(), 'check')"
        )
        run_id = cur.lastrowid
        cur.execute(
            "INSERT INTO rst_splits (run_id, mile_number, distance_miles, pace_seconds) VALUES (%s, 1, 1.0, 450.0)",
            (run_id,),
        )
        sample = [["2026-01-01T12:00:00Z", 40.0, -75.0, 8, 4, 12.5, 3.6, 0.5, 90.0, 5.0, ["running"]]]
        cur.execute(
            "INSERT INTO rst_point_chunks (run_id, chunk_index, point_count, points_gz) VALUES (%s, 0, 1, %s)",
            (run_id, db.encode_points(sample)),
        )
        cur.execute("INSERT INTO rst_run_conditions (run_id, temperature_f) VALUES (%s, 58.0)", (run_id,))

        cur.execute(
            "SELECT r.id, s.pace_seconds, c.points_gz, w.temperature_f FROM rst_runs r"
            " JOIN rst_splits s ON s.run_id = r.id"
            " JOIN rst_point_chunks c ON c.run_id = r.id"
            " JOIN rst_run_conditions w ON w.run_id = r.id WHERE r.id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        points_back = db.decode_points(row[2])["points"] if row else None
        step("Write + read back (all 4 run tables)", bool(row) and points_back == sample,
             "run, split, compressed points, and weather row round-tripped")
finally:
    conn.rollback()
    conn.close()

print("\nAll checks passed. Test rows were rolled back - nothing was left in the database.")
