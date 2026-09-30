-- Run Split Tracker database schema (MySQL 8 / InnoDB).
-- Every table is prefixed rst_ because the database is shared with other projects.

-- One row per tracked run (an Overland trip from Start to Stop).
CREATE TABLE rst_runs (
    id INT NOT NULL AUTO_INCREMENT,
    source VARCHAR(20) NOT NULL DEFAULT 'tracker',   -- 'tracker', or later 'strava_import' for backfilled runs
    status VARCHAR(20) NOT NULL DEFAULT 'active',    -- 'active' until the trip ends, then 'completed';
                                                      -- 'reset' (admin reset mid-run) or 'interrupted'
                                                      -- (app restarted and the trip couldn't be resumed)
    started_at DATETIME NOT NULL,                     -- UTC
    ended_at DATETIME NULL,                           -- UTC
    utc_offset_minutes SMALLINT NULL,
    distance_miles DECIMAL(6,3) NULL,
    moving_seconds INT NULL,
    elapsed_seconds INT NULL,
    avg_pace_seconds DECIMAL(6,1) NULL,               -- per mile, based on moving time
    goal_distance_miles DECIMAL(4,2) NULL,
    goal_reached TINYINT(1) NOT NULL DEFAULT 0,
    target_pace_seconds SMALLINT NULL,
    test_mode TINYINT(1) NOT NULL DEFAULT 0,          -- lets testing-phase runs be filtered out of analysis
    excluded TINYINT(1) NOT NULL DEFAULT 0,           -- manually mark bad runs (forgot to stop the trip, etc.)
    algorithm_version VARCHAR(20) NOT NULL,           -- which tracker logic produced the stored numbers
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_started_at (started_at)
);

-- One row per completed mile, plus the trailing partial mile.
CREATE TABLE rst_splits (
    id INT NOT NULL AUTO_INCREMENT,
    run_id INT NOT NULL,
    mile_number SMALLINT NOT NULL,
    distance_miles DECIMAL(5,3) NOT NULL,             -- 1.000, or the trailing partial
    pace_seconds DECIMAL(6,1) NOT NULL,               -- per mile, based on moving time
    is_partial TINYINT(1) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_run_mile (run_id, mile_number),
    CONSTRAINT fk_rst_splits_run FOREIGN KEY (run_id) REFERENCES rst_runs (id) ON DELETE CASCADE
);

-- Raw GPS points exactly as Overland sent them (unfiltered), gzip-compressed
-- JSON, saved in roughly one-minute chunks. Keeping the raw data means old
-- runs can be reprocessed whenever the tracking algorithm improves.
CREATE TABLE rst_point_chunks (
    run_id INT NOT NULL,
    chunk_index INT NOT NULL,
    point_count SMALLINT NOT NULL,
    points_gz MEDIUMBLOB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, chunk_index),
    CONSTRAINT fk_rst_chunks_run FOREIGN KEY (run_id) REFERENCES rst_runs (id) ON DELETE CASCADE
);

-- Weather at the start of a run (can't be re-measured later, unlike
-- anything derivable from rst_runs such as weekly mileage).
CREATE TABLE rst_run_conditions (
    run_id INT NOT NULL,
    temperature_f DECIMAL(4,1) NULL,
    feels_like_f DECIMAL(4,1) NULL,
    humidity_pct TINYINT NULL,
    wind_mph DECIMAL(4,1) NULL,
    wind_direction_deg SMALLINT NULL,
    precipitation_in DECIMAL(4,2) NULL,
    weather_code SMALLINT NULL,                       -- Open-Meteo WMO code: clear, cloudy, rain...
    is_day TINYINT(1) NULL,
    source VARCHAR(20) NOT NULL DEFAULT 'open-meteo',
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id),
    CONSTRAINT fk_rst_conditions_run FOREIGN KEY (run_id) REFERENCES rst_runs (id) ON DELETE CASCADE
);

-- Admin settings (goal distance, target pace, test mode) so they survive restarts.
CREATE TABLE rst_settings (
    setting_key VARCHAR(50) NOT NULL,
    setting_value VARCHAR(255) NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (setting_key)
);
