"""The web endpoints: Overland's token, admin passwords and validation, the
dashboard data, run deletion, and the rule that test runs are never saved."""
import pytest

import app as app_module
from conftest import ADMIN_PASSWORD, TOKEN


# --- passwords -------------------------------------------------------------------

def test_overland_needs_its_token(client):
    assert client.post("/overland", json={"locations": []}).status_code == 401
    assert client.post("/overland", headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401
    assert client.post("/overland", headers=TOKEN, json={"trip": None, "locations": []}).get_json() == {"result": "ok"}


@pytest.mark.parametrize("path", [
    "/admin/settings", "/admin/reset", "/admin/six-seven-test", "/admin/runs/7/delete", "/admin/runs/delete-test",
])
def test_admin_endpoints_need_the_password(client, path):
    assert client.post(path, json={"password": "nope"}).status_code == 401
    assert client.post(path, json={}).status_code == 401


# --- settings ----------------------------------------------------------------------

@pytest.mark.parametrize("values, error", [
    ({"goal_distance_miles": 0}, "greater than 0"),
    ({"goal_distance_miles": 100}, "at most 99.99"),
    ({"goal_distance_miles": 1.234}, "2 decimal places"),
    ({"target_pace_seconds": 30}, "between 1:00 and 10:59"),
    ({"target_pace_enabled": True}, "pick a target pace"),
    ({"goal_enabled": True}, "set a goal distance"),
])
def test_invalid_settings_are_rejected_and_nothing_changes(client, values, error):
    before = dict(app_module.admin_state)
    response = client.post("/admin/settings", json={"password": ADMIN_PASSWORD, **values})
    assert response.status_code == 400 and error in response.get_json()["error"]
    assert app_module.admin_state == before


def test_setting_a_goal_switches_it_on_and_off_keeps_the_distance(settings):
    assert settings(goal_distance_miles=13.1)["goal_enabled"] is True
    off = settings(goal_enabled=False)
    assert off["goal_enabled"] is False and off["goal_distance_miles"] == 13.1


def test_switches_are_saved_and_returned(settings):
    response = settings(dm_mile_splits=False, dark_map=True, dm_muted=True)
    assert (response["dm_mile_splits"], response["dark_map"], response["dm_muted"]) == (False, True, True)


# --- the dashboard -------------------------------------------------------------------

def test_status_during_and_after_a_run(client, simulate):
    simulate([(1.2, 450)], end=False)
    live = client.get("/status").get_json()
    assert live["active"] is True
    assert live["seconds_since_update"] is not None and live["seconds_since_update"] < 5
    assert [s["mile"] for s in live["splits"]] == [1]
    assert live["started_at"].startswith("2026-10-12T15:00:00")

    client.post("/overland", headers=TOKEN, json={"trip": None, "locations": []})
    after = client.get("/status").get_json()
    assert after["active"] is False and after["seconds_since_update"] is None
    assert after["distance_miles"] == pytest.approx(1.2, abs=0.02)
    assert after["fastest_split"]["mile"] == 1
    assert {e["key"] for e in after["best_efforts"]} >= {"400m", "1_mile"}


def test_reset_clears_the_dashboard(client, simulate):
    simulate([(0.5, 450)])
    assert client.post("/admin/reset", json={"password": ADMIN_PASSWORD}).status_code == 200
    assert client.get("/status").get_json()["distance_miles"] == 0


def test_pages_load(client):
    for path in ("/", "/runs", "/runs/31", "/health"):
        assert client.get(path).status_code == 200


# --- test runs are never saved ----------------------------------------------------------

def test_a_normal_run_is_saved(simulate, fake_db):
    simulate([(1.1, 450)])
    names = fake_db.names()
    assert names.count("create_run") == 1 and names.count("finish_run") == 1 and "save_point_chunk" in names


def test_test_mode_from_the_start_saves_nothing(simulate, settings, fake_db):
    settings(discord_test_mode=True)
    simulate([(0.6, 450)])
    assert not {"create_run", "save_point_chunk", "finish_run"} & set(fake_db.names())


def test_test_mode_switched_on_mid_run_deletes_the_saved_run(simulate, settings, fake_db):
    simulate([(0.8, 450)], during={0.4: lambda: settings(discord_test_mode=True)})
    names = fake_db.names()
    assert "create_run" in names and "delete_run" in names and "finish_run" not in names


# --- deleting runs ---------------------------------------------------------------------

def test_delete_a_run(client, fake_db):
    assert client.post("/admin/runs/7/delete", json={"password": ADMIN_PASSWORD}).get_json() == {"deleted": [7]}
    again = client.post("/admin/runs/7/delete", json={"password": ADMIN_PASSWORD})
    assert again.status_code == 404


def test_the_run_being_recorded_cant_be_deleted(client, simulate, fake_db):
    simulate([(0.3, 450)], end=False)
    run_id = app_module.recording["run_id"]
    response = client.post(f"/admin/runs/{run_id}/delete", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 409
    assert "still being recorded" in response.get_json()["error"]


def test_deleting_the_run_on_the_dashboard_clears_it(client, fake_db):
    app_module.last_summary = {"run_id": 8, "path": [], "splits": []}
    client.post("/admin/runs/8/delete", json={"password": ADMIN_PASSWORD})
    assert app_module.last_summary is None


def test_delete_all_test_runs(client, fake_db):
    response = client.post("/admin/runs/delete-test", json={"password": ADMIN_PASSWORD})
    assert response.get_json() == {"deleted": [41, 42]}


def test_deleting_without_a_database(client):
    response = client.post("/admin/runs/7/delete", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 503


# --- past runs -------------------------------------------------------------------------

def test_past_runs_list_shows_everything_only_to_the_admin(client, fake_db):
    client.get("/api/runs")
    client.get("/api/runs", headers={"X-Admin-Password": ADMIN_PASSWORD})
    client.get("/api/runs", headers={"X-Admin-Password": "wrong"})
    include_all = [kwargs["include_all"] for name, _, kwargs in fake_db.calls if name == "list_runs"]
    assert include_all == [False, True, False]
