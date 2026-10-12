"""The run math itself: distance, splits, auto-pause, GPS filtering, mile
markers, best efforts, elevation and GAP - fed straight into RunTracker."""
from datetime import timedelta

import pytest

from conftest import START, position
from split_tracker import (
    FEET_PER_METER, MILE_METERS, ClimbCounter, RunTracker, format_duration, format_pace, grade_cost_ratio,
)


def feed(tracker, segments, altitude=None, every=5):
    """[(miles, seconds per mile) | ("stop", seconds), ...] -> points every `every` seconds."""
    t, meters = 0.0, 0.0
    for segment in segments:
        if segment[0] == "stop":
            steps, speed = int(segment[1] / every), 0.0
        else:
            miles, pace = segment
            speed = MILE_METERS / pace
            steps = round(miles * MILE_METERS / (speed * every))
        for _ in range(steps):
            lat, lon = position(meters)
            tracker.process_point(lat, lon, START + timedelta(seconds=t), 5, speed,
                                  altitude(meters) if altitude else None)
            t += every
            meters += speed * every
    return tracker


def finished(segments, **kwargs):
    return feed(RunTracker(), segments, **kwargs).end()


# --- formatting ---------------------------------------------------------------

def test_format_pace_and_duration():
    assert format_pace(450) == "7:30/mi"
    assert format_pace(419.6) == "7:00/mi"
    assert format_duration(59) == "0:59"
    assert format_duration(5969) == "1:39:29"


# --- distance, splits, moving time ----------------------------------------------

def test_steady_run_distance_and_splits():
    summary = finished([(3.05, 450)])
    assert summary["distance_miles"] == pytest.approx(3.05, abs=0.02)
    assert [s["mile"] for s in summary["splits"]] == [1, 2, 3]
    for split in summary["splits"][1:]:  # the first mile includes the very first stride
        assert split["pace_seconds"] == pytest.approx(450, abs=3)
    assert summary["average_pace_seconds"] == pytest.approx(450, abs=4)


def test_a_stop_is_left_out_of_moving_time():
    summary = finished([(0.5, 450), ("stop", 120), (0.6, 450)])
    assert summary["elapsed_seconds"] - summary["moving_seconds"] == pytest.approx(120, abs=15)
    assert summary["average_pace_seconds"] == pytest.approx(450, abs=10)  # the stop doesn't slow the pace


def test_poor_accuracy_and_gps_jumps_are_ignored():
    tracker = feed(RunTracker(), [(0.5, 450)])
    distance = tracker.cumulative_meters
    last = tracker.last_seen_time
    tracker.process_point(*position(distance), last + timedelta(seconds=5), 80, 3.5)  # 80 m accuracy
    assert tracker.last_outcome == "poor accuracy"
    tracker.process_point(*position(distance + 2000), last + timedelta(seconds=10), 5, 3.5)  # 2 km in 10 s
    assert tracker.last_outcome == "GPS jump"
    assert tracker.cumulative_meters == distance


def test_standing_still_adds_no_distance():
    tracker = feed(RunTracker(), [(0.3, 450)])
    distance = tracker.cumulative_meters
    lat, lon = position(distance)
    for i in range(20):  # GPS wobbling a few meters around a crosswalk
        wobble = (i % 3 - 1) * 0.00002
        tracker.process_point(lat + wobble, lon - wobble, START + timedelta(seconds=1000 + 5 * i), 5, 0.0)
    assert tracker.cumulative_meters == distance


def test_mile_markers_sit_on_the_drawn_route():
    summary = finished([(2.2, 450)])
    for split in summary["splits"]:
        assert 0 < split["path_end"] <= len(summary["path"])
        expected_lat, _ = position(split["mile"] * MILE_METERS)
        assert split["lat"] == pytest.approx(expected_lat, abs=0.0002)  # within ~20 m of the true mile mark


# --- best efforts ---------------------------------------------------------------

def test_best_efforts_cover_only_distances_run():
    keys = [e["key"] for e in finished([(3.3, 450)])["best_efforts"]]
    assert keys == ["400m", "half_mile", "1k", "1_mile", "2_mile", "5k"]


def test_best_effort_finds_the_fast_stretch_inside_a_run():
    summary = finished([(1.0, 500), (1.0, 420), (1.0, 500)])
    mile = next(e for e in summary["best_efforts"] if e["key"] == "1_mile")
    assert mile["seconds"] == pytest.approx(420, abs=3)
    assert mile["seconds"] <= min(s["pace_seconds"] for s in summary["splits"]) + 0.1  # never slower than a split
    assert mile["start_meters"] == pytest.approx(MILE_METERS, abs=60)  # it's the middle mile


# --- elevation ------------------------------------------------------------------

def test_climb_counter_ignores_small_wiggles():
    counter = ClimbCounter(threshold=4)
    for altitude in [100, 102, 100, 103, 101, 100]:  # all inside 4 m
        counter.add(altitude)
    assert counter.gain == 0 and counter.loss == 0
    for altitude in [105, 110, 112, 106]:  # a real 12 m climb, then a 6 m drop
        counter.add(altitude)
    assert counter.gain == pytest.approx(12)
    assert counter.loss == pytest.approx(6)


def test_steady_climb_is_measured():
    climb = lambda meters: 50 + 20 * meters / MILE_METERS  # 20 m (66 ft) per mile
    summary = finished([(1.5, 450)], altitude=climb)
    assert summary["elevation_gain_ft"] == pytest.approx(30 * FEET_PER_METER, rel=0.1)
    assert summary["elevation_loss_ft"] < 5


def test_one_meter_flicker_is_not_climbing():
    flicker = lambda meters: 65 + (int(meters / 17) % 2)  # the phone's 65/66 m flicker
    summary = finished([(1.2, 450)], altitude=flicker)
    assert summary["elevation_gain_ft"] == 0


def test_altitude_glitch_is_dropped():
    def glitchy(meters):  # flat, except a few seconds reading 12 m low (seen on a real run)
        return 52 if 300 <= meters < 340 else 64
    summary = finished([(1.2, 450)], altitude=glitchy)
    assert summary["elevation_gain_ft"] < 5


# --- GAP ------------------------------------------------------------------------

def test_grade_cost_ratio():
    assert grade_cost_ratio(0) == pytest.approx(1)
    assert grade_cost_ratio(0.05) > 1 > grade_cost_ratio(-0.03)


def test_gap_matches_pace_on_flat_ground():
    summary = finished([(2.1, 450)], altitude=lambda m: 60)
    for split in summary["splits"]:
        assert split["gap_seconds"] == pytest.approx(split["pace_seconds"], abs=0.5)


def test_gap_is_faster_uphill_and_slower_downhill():
    up = finished([(1.1, 480)], altitude=lambda m: 50 + 40 * m / MILE_METERS)["splits"][0]
    down = finished([(1.1, 420)], altitude=lambda m: 90 - 40 * m / MILE_METERS)["splits"][0]
    assert up["gap_seconds"] < up["pace_seconds"]
    assert down["gap_seconds"] > down["pace_seconds"]


def test_no_altitude_means_no_elevation_or_gap():
    summary = finished([(1.1, 450)])
    assert summary["elevation_gain_ft"] is None
    assert summary["average_gap_display"] is None
    assert summary["splits"][0]["gap_display"] is None
