"""Records: the fastest-mile-split star and best-effort medals (gold/silver/bronze)."""
from datetime import datetime

import pytest

import app as app_module
from split_tracker import format_pace

SAVED_RECORD = {"pace_seconds": 420.5, "run_id": 30, "mile": 1,
                "started_at": datetime(2026, 9, 30, 21, 46), "utc_offset_minutes": -420}


def splits(*paces):
    return [{"mile": i + 1, "pace_seconds": p, "pace_display": format_pace(p)} for i, p in enumerate(paces)]


@pytest.fixture
def saved_record():
    app_module.best_mile.update(record=SAVED_RECORD, loaded=True)


# --- fastest mile split ever (the star) ------------------------------------------

@pytest.mark.parametrize("paces, run_id, counts, starred", [
    ((450, 420.4), 99, True, 2),       # a tenth faster than the record
    ((450, 420.5), 99, True, None),    # a tie stays with whoever set it first
    ((415, 450, 405), 99, True, 3),    # beaten twice in one run: only the fastest is starred
    ((230, 410), 99, True, 2),         # a sub-4:00 "mile" is bad data, not a record
    ((390,), 99, False, None),         # test-mode runs never count
    ((420.5, 430), 30, True, 1),       # the run that holds the record, shown again
])
def test_record_star(saved_record, paces, run_id, counts, starred):
    _, star = app_module.record_status(splits(*paces), run_id, counts)
    assert star == starred


def test_record_card_shows_the_saved_record_when_not_beaten(saved_record):
    card, star = app_module.record_status(splits(450, 440), 99, True)
    assert star is None
    assert card == {"pace_display": "7:00/mi", "this_run": False, "mile": 1, "date_display": "Sep 30, 2026"}


def test_no_star_until_the_record_has_loaded(monkeypatch):
    monkeypatch.setattr(app_module, "PERSISTENCE_ENABLED", True)  # loaded=False, so it isn't known yet
    assert app_module.record_status(splits(300), 99, True) == (None, None)


# --- best-effort medals -----------------------------------------------------------

LEADERS = {"5k": [
    {"seconds": 1314.0, "run_id": 60, "started_at": datetime(2026, 10, 11), "timezone": None},
    {"seconds": 1341.0, "run_id": 54, "started_at": datetime(2026, 10, 7), "timezone": None},
    {"seconds": 1362.7, "run_id": 30, "started_at": datetime(2026, 9, 30), "timezone": None},
]}


def effort(seconds, key="5k"):
    return {"key": key, "name": "5K", "seconds": seconds, "time_display": "x"}


@pytest.fixture
def leaders():
    app_module.best_effort_leaders.update(leaders=LEADERS, loaded=True)


@pytest.mark.parametrize("seconds, run_id, counts, rank", [
    (1300.0, None, True, 1),    # a new run beats everyone: gold
    (1314.0, None, True, 2),    # ties the gold exactly: the earlier run keeps it
    (1350.0, None, True, 3),
    (1400.0, None, True, None),  # slower than all three
    (1341.0, 54, True, 2),      # a saved run keeps its saved place
    (1300.0, None, False, None),  # test runs never get medals
])
def test_effort_medals(leaders, seconds, run_id, counts, rank):
    assert app_module.ranked_efforts([effort(seconds)], run_id, counts)[0]["rank"] == rank


def test_no_medals_until_leaders_load():
    assert app_module.ranked_efforts([effort(1000)], None, True)[0]["rank"] is None


def test_best_efforts_dm_lists_medals_only(leaders):
    summary = {"run_id": None, "test_mode_used": False, "best_efforts": [
        dict(effort(1350.0), time_display="22:30"),                       # bronze
        dict(effort(2700.0, key="10k"), name="10K", time_display="45:00"),  # no 10K saved yet: automatically a PR
    ]}
    assert app_module.best_efforts_message(summary) == (
        "Best efforts: 5K 22:30 (bronze), 10K 45:00 (gold - new PR!)")
    assert app_module.best_efforts_message(dict(summary, best_efforts=[effort(1400.0)])) is None  # no medals
    assert app_module.best_efforts_message(dict(summary, test_mode_used=True)) is None
