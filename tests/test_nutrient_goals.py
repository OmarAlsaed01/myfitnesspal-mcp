import datetime

import pytest

from myfitnesspal_mcp import nutrient_goals, sync

TUESDAY = datetime.date(2026, 7, 7)
WEDNESDAY = datetime.date(2026, 7, 8)


def _goal(carbs=220.0, *, sodium=2300.0, fiber=30.0, trans_fat=0.0, **extra):
    return {
        "energy": {"value": 2200.0, "unit": "calories"},
        "carbohydrates": carbs,
        "protein": 160.0,
        "fat": 70.0,
        "sodium": sodium,
        "potassium": 3500.0,
        "fiber": fiber,
        "sugar": 90.0,
        "trans_fat": trans_fat,
        **extra,
    }


def goals_payload(*, valid_from="2026-07-01", valid_to="2100-01-01", daily=None):
    return {
        "items": [
            {
                "valid_from": valid_from,
                "valid_to": valid_to,
                "default_goal": _goal(),
                "daily_goals": daily or [],
            }
        ]
    }


@pytest.fixture
def goals_client(client, make_response):
    client.session.route(
        "GET",
        "v2/nutrient-goals",
        make_response(
            json_data=goals_payload(
                daily=[{"day_of_week": "tuesday", **_goal(carbs=260.0, fiber=35.0)}]
            )
        ),
    )
    return client


def _goal_calls(client):
    return [url for method, url, _ in client.session.calls if "nutrient-goals" in url]


def test_goal_period_reads_each_weekday_and_falls_back_to_the_default(goals_client):
    period = nutrient_goals.goal_period(goals_client, WEDNESDAY)
    assert _goal_calls(goals_client) == [
        "https://api.myfitnesspal.com/v2/nutrient-goals?date=2026-07-08"
    ]
    assert (period["valid_from"], period["valid_to"]) == ("2026-07-01", "2100-01-01")
    tuesday, wednesday = period["goals"][1], period["goals"][2]
    assert (tuesday["carbs"], tuesday["fiber"]) == (260.0, 35.0)
    assert (wednesday["carbs"], wednesday["fiber"]) == (220.0, 30.0)
    assert wednesday["sodium"] == 2300.0
    assert wednesday["protein"] == 160.0
    assert wednesday["trans_fat"] is None
    assert wednesday["iron"] is None


def test_day_goals_add_the_micro_goals_of_the_covering_period(goals_client, store):
    store.save_goal_period(
        nutrient_goals.goal_period(goals_client, WEDNESDAY), fetched_on="2026-07-08"
    )
    store.upsert_nutrition(
        "2026-07-07", calories=1800.0, goal_calories=2200.0, fiber=12.0
    )

    record = store.day_record("2026-07-07")

    assert record["goals"]["calories"] == 2200.0
    assert record["goals"]["fiber"] == 35.0
    assert record["goals"]["sodium"] == 2300.0
    assert "trans_fat" not in record["goals"]
    assert record["remaining"]["fiber"] == 23.0
    assert record["remaining"]["sodium"] is None


def test_day_goals_without_a_period_keep_only_calories_and_macros(store):
    store.upsert_nutrition("2026-07-07", goal_calories=2200.0)
    assert list(store.goals("2026-07-07")) == ["calories", "protein", "carbs", "fat"]


def test_saving_a_goal_change_drops_the_overlapping_period(store):
    week = [{"fiber": 30.0}] * 7
    store.save_goal_period(
        {"valid_from": "2026-07-01", "valid_to": "2100-01-01", "goals": week},
        fetched_on="2026-07-01",
    )
    store.save_goal_period(
        {"valid_from": "2026-07-06", "valid_to": "2100-01-01", "goals": week},
        fetched_on="2026-07-08",
    )
    assert store.goal_period("2026-07-03") is None
    assert store.goal_period("2026-07-08")["valid_from"] == "2026-07-06"


def test_refresh_goal_period_fetches_once_per_period_a_day(goals_client, store):
    for day in (TUESDAY, WEDNESDAY, WEDNESDAY):
        sync.refresh_goal_period(
            store, goals_client, day, {"carbohydrates": 220}, today=WEDNESDAY
        )
    assert len(_goal_calls(goals_client)) == 1


def test_refresh_goal_period_refetches_an_open_period_on_a_later_day(
    goals_client, store
):
    sync.refresh_goal_period(store, goals_client, WEDNESDAY, {}, today=WEDNESDAY)
    sync.refresh_goal_period(
        store, goals_client, WEDNESDAY, {}, today=datetime.date(2026, 7, 9)
    )
    assert len(_goal_calls(goals_client)) == 2


def test_refresh_goal_period_trusts_a_closed_period(client, store, make_response):
    client.session.route(
        "GET",
        "v2/nutrient-goals",
        make_response(json_data=goals_payload(valid_to="2026-07-10")),
    )
    later = datetime.date(2026, 8, 1)
    sync.refresh_goal_period(store, client, WEDNESDAY, {}, today=WEDNESDAY)
    sync.refresh_goal_period(store, client, WEDNESDAY, {}, today=later)
    assert len(_goal_calls(client)) == 1


def test_refresh_goal_period_refetches_when_diary_macro_goals_moved(
    goals_client, store
):
    sync.refresh_goal_period(
        store, goals_client, WEDNESDAY, {"carbohydrates": 220}, today=WEDNESDAY
    )
    sync.refresh_goal_period(
        store, goals_client, WEDNESDAY, {"carbohydrates": 250}, today=WEDNESDAY
    )
    assert len(_goal_calls(goals_client)) == 2
