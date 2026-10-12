"""Discord DMs during and after a run, sent through the real /overland
endpoint: what goes out, and every switch that turns one off."""
from datetime import datetime

import pytest

import app as app_module

SAVED_RECORD = {"pace_seconds": 420.5, "run_id": 30, "mile": 1,
                "started_at": datetime(2026, 9, 30, 21, 46), "utc_offset_minutes": -420}


def kinds(dms):
    """What each sent DM was, by how it starts."""
    prefixes = [("Tracking started", "start"), ("Mile ", "mile"), ("Pace check", "pace check"), ("Pace:", "half"),
                ("Halfway to goal", "goal half"), ("Goal reached", "goal"), ("New fastest mile", "record"),
                ("Run complete", "summary"), ("Best efforts", "efforts"), ("Jacob has run 6.7", "6.7")]
    return [next((kind for prefix, kind in prefixes if dm.text.startswith(prefix)), "?") for dm in dms]


@pytest.fixture
def goal_and_target(settings):
    settings(goal_distance_miles=2.0, target_pace_seconds=450, target_pace_enabled=True)


def test_a_normal_run_sends_every_kind_of_update(simulate, dms, goal_and_target):
    simulate([(2.1, 440)])
    assert kinds(dms) == ["start", "half", "mile", "goal half", "half", "mile", "goal", "summary"]
    assert dms[0].text.startswith("Tracking started - 60°F, clear. Goal: 2.00 mi. Target pace: 7:30/mi")
    assert "faster than target" in dms[2].text  # "Mile 1 - Pace: 7:2x/mi (xs faster than target)"
    assert dms[-1].image == b"PNG"  # the summary carries the route map
    assert all(dm.to == "you" for dm in dms)


@pytest.mark.parametrize("switch, kind", [
    ("dm_tracking_started", "start"), ("dm_mile_splits", "mile"), ("dm_half_mile", "half"),
    ("dm_goal_halfway", "goal half"), ("dm_goal_reached", "goal"), ("dm_run_summary", "summary"),
])
def test_each_switch_turns_off_only_its_own_dm(simulate, dms, settings, goal_and_target, switch, kind):
    settings(**{switch: False})
    simulate([(2.1, 440)])
    everything = ["start", "half", "mile", "goal half", "half", "mile", "goal", "summary"]
    assert kinds(dms) == [k for k in everything if k != kind]


def test_target_comparison_switch(simulate, dms, settings, goal_and_target):
    settings(dm_target_comparison=False)
    simulate([(1.1, 440)])
    assert not any("target)" in dm.text for dm in dms if not dm.text.startswith("Tracking"))


def test_mute_silences_everything_including_the_67_dm(simulate, dms, settings):
    settings(dm_muted=True, six_seven_enabled=True)
    simulate([(6.8, 440)])
    assert dms == []


def test_the_67_dm_goes_to_the_friend_once(simulate, dms, settings):
    settings(six_seven_enabled=True, dm_mile_splits=False, dm_half_mile=False,
             dm_tracking_started=False, dm_run_summary=False)
    simulate([(6.8, 440)])
    assert kinds(dms) == ["6.7"]
    assert dms[0].to == "friend-id" and dms[0].image == b"PNG"


def test_goal_switched_on_late_sends_no_stale_dm(simulate, dms, settings):
    settings(goal_distance_miles=2.0)
    settings(goal_enabled=False)
    simulate([(2.2, 440)], during={1.3: lambda: settings(goal_enabled=True)})  # halfway (1.0 mi) already passed
    assert "goal half" not in kinds(dms)
    assert "goal" in kinds(dms)


def test_new_record_dm(simulate, dms, settings):
    app_module.best_mile.update(record=SAVED_RECORD, loaded=True)
    settings(dm_half_mile=False, dm_tracking_started=False, dm_run_summary=False)
    simulate([(1.0, 415), (1.0, 450), (1.05, 405)])  # beats 7:00.5 at mile 1, then again at mile 3
    records = [dm.text for dm in dms if dm.text.startswith("New fastest")]
    assert len(records) == 2
    assert records[1].endswith(f"(previous best {records[0].split(' - ')[1].split(' ')[0]})")


def test_pace_check_nudges_once_then_rearms(simulate, dms, settings):
    settings(target_pace_seconds=450, target_pace_enabled=True, dm_pace_check=True, dm_mile_splits=False,
             dm_half_mile=False, dm_tracking_started=False, dm_run_summary=False)
    simulate([(1.0, 450), (1.2, 480), (0.5, 450), (0.8, 485)])  # on pace, slow, recover, slow again
    nudges = [dm.text for dm in dms if dm.text.startswith("Pace check")]
    assert len(nudges) == 2
    assert "slower than your 7:30/mi target" in nudges[0]


def test_test_mode_sends_frequent_updates_and_never_a_record(simulate, dms, settings):
    app_module.best_mile.update(record=SAVED_RECORD, loaded=True)
    settings(discord_test_mode=True, dm_tracking_started=False, dm_run_summary=False)
    simulate([(1.05, 400)])  # a 6:40 mile - a record if it counted
    assert kinds(dms).count("half") > 20  # "Pace: ..." every 15 s
    assert "record" not in kinds(dms)


def test_no_summary_for_an_accidental_start_stop(simulate, dms):
    simulate([(0.02, 450)])
    assert "summary" not in kinds(dms)


def test_67_test_button_only_ever_messages_you(client, simulate, dms, settings):
    simulate([(0.3, 450)])
    dms.clear()
    response = client.post("/admin/six-seven-test", json={"password": "test-admin"})
    assert response.get_json() == {"sent": True, "with_map": True, "map_source": "last run"}
    assert dms[0].to == "you"
