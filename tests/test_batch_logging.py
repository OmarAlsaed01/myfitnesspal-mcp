import datetime
import json

import pytest
from conftest import diary_after_add

from myfitnesspal_mcp import batch_logging, diary, food_logging

TODAY = datetime.date(2026, 7, 8)


@pytest.fixture
def logging_client(library, diary_html):
    library.session.route("GET", "food/diary?date=", diary_after_add(diary_html))
    return library


def food_adds(client):
    return [
        kwargs["data"] for m, url, kwargs in client.session.calls if "food/add" in url
    ]


def diary_reads(client):
    return [url for m, url, _ in client.session.calls if "food/diary" in url]


def test_batch_logs_every_kind_of_item_in_order(logging_client, store):
    store.set_pin("oat milk", "444", "40", "Oat Milk", "1 cup")
    logging_client.food_details[999] = {
        "calories": 105.0,
        "verified": True,
        "nutrition": {"protein": 1.3, "carbohydrates": 27.0, "fat": 0.4},
        "serving_sizes": [
            {"value": 1.0, "unit": "medium", "nutrition_multiplier": 1.0},
            {"value": 1.0, "unit": "large", "nutrition_multiplier": 1.15},
        ],
    }
    draft = food_logging.draft_food(logging_client, store, "banana", TODAY)

    result = batch_logging.log_batch(
        logging_client,
        store,
        TODAY,
        "lunch",
        [
            {"draft_id": draft["draft_id"], "option": 1, "serving": 1},
            {"food_id": "555", "weight_id": "50", "name": "Rice", "quantity": 2},
            {"query": "oat milk", "meal": "snacks"},
            {"custom": "Turkey Chili"},
        ],
    )

    assert (result["logged_count"], result["failed_count"]) == (4, 0)
    assert [r["entry"] for r in result["results"]] == [
        "items[0]",
        "items[1]",
        "items[2]",
        "items[3]",
    ]
    assert [r["logged"] for r in result["results"]] == [
        "Banana",
        "Rice",
        "Oat Milk",
        "Turkey Chili",
    ]
    assert [r["meal"] for r in result["results"]] == [
        "lunch",
        "lunch",
        "snacks",
        "lunch",
    ]
    adds = food_adds(logging_client)
    assert [add["food_entry[food_id]"] for add in adds[:3]] == ["111", "555", "444"]
    assert adds[1]["food_entry[quantity]"] == "2.0"
    assert store.pin("banana")["weight_id"] == "20"


def test_batch_reads_the_diary_once_per_add_plus_once_before(logging_client, store):
    batch_logging.log_batch(
        logging_client,
        store,
        TODAY,
        "dinner",
        micros={"sodium": 1200, "fiber": 8},
        macros={"protein": 42},
    )
    assert len(food_adds(logging_client)) == 3
    assert len(diary_reads(logging_client)) == 4


def test_unit_shortcuts_skip_zeros_and_log_amounts_as_quantities(logging_client, store):
    result = batch_logging.log_batch(
        logging_client,
        store,
        TODAY,
        "dinner",
        macros={"protein": 42, "carbs": 80, "fat": 0},
        micros={"sodium": 2200, "potassium": None, "fiber": 5, "sugar": 10},
    )
    assert [r["entry"] for r in result["results"]] == [
        "macros.protein",
        "macros.carbs",
        "micros.sodium",
        "micros.fiber",
        "micros.sugar",
    ]
    adds = food_adds(logging_client)
    sodium = batch_logging.DEFAULT_UNIT_FOODS["sodium"]
    assert adds[2]["food_entry[food_id]"] == sodium["food_id"]
    assert adds[2]["food_entry[weight_id]"] == sodium["weight_id"]
    assert [add["food_entry[quantity]"] for add in adds] == [
        "42.0",
        "80.0",
        "2200.0",
        "5.0",
        "10.0",
    ]


def test_unit_foods_can_be_pointed_elsewhere(logging_client, store, monkeypatch):
    monkeypatch.setenv(
        "MFP_UNIT_FOODS", json.dumps({"fiber": {"food_id": 9001, "weight_id": 9002}})
    )
    batch_logging.log_batch(logging_client, store, TODAY, "lunch", micros={"fiber": 3})
    [add] = food_adds(logging_client)
    assert (add["food_entry[food_id]"], add["food_entry[weight_id]"]) == (
        "9001",
        "9002",
    )


@pytest.mark.parametrize(
    "override, message",
    [
        ("not json", "not valid JSON"),
        ('{"iron": {"food_id": 1, "weight_id": 2}}', "unknown nutrients"),
        ('{"fiber": {"food_id": 1}}', "needs food_id and weight_id"),
    ],
)
def test_bad_unit_food_overrides_fail_before_logging(
    logging_client, store, monkeypatch, override, message
):
    monkeypatch.setenv("MFP_UNIT_FOODS", override)
    with pytest.raises(ValueError, match=message):
        batch_logging.log_batch(
            logging_client, store, TODAY, "lunch", micros={"fiber": 3}
        )
    assert food_adds(logging_client) == []


def test_a_failing_item_does_not_stop_the_others(logging_client, store):
    result = batch_logging.log_batch(
        logging_client,
        store,
        TODAY,
        "lunch",
        [
            {"draft_id": "gone", "option": 1},
            {"query": "bread"},
            {"food_id": "555", "weight_id": "50"},
        ],
        micros={"sugar": 4},
    )
    assert (result["logged_count"], result["failed_count"]) == (2, 2)
    gone, bread, rice, sugar = result["results"]
    assert gone["ok"] is False and "not found or expired" in gone["error"]
    assert bread["ok"] is False and "fitness_draft_food" in bread["error"]
    assert rice["ok"] is True and sugar["ok"] is True
    assert len(food_adds(logging_client)) == 2


def test_a_failed_read_back_is_reported_and_the_batch_goes_on(
    logging_client, store, diary_html
):
    added = diary_after_add(diary_html)

    def flaky_diary(calls):
        reads = sum(1 for m, url, _ in calls if "food/diary" in url)
        if reads == 2:
            raise diary.DiarySignedOut("diary page has no meal sections")
        return added(calls)

    logging_client.session.route("GET", "food/diary?date=", flaky_diary)
    result = batch_logging.log_batch(
        logging_client, store, TODAY, "lunch", micros={"sodium": 100, "fiber": 2}
    )
    sodium, fiber = result["results"]
    assert sodium["ok"] is False and "could not be read back" in sodium["error"]
    assert fiber["ok"] is True
    assert len(food_adds(logging_client)) == 2


@pytest.mark.parametrize(
    "items, macros, message",
    [
        ([{"query": "a", "food_id": "1", "weight_id": "2"}], None, "exactly one"),
        ([{}], None, "exactly one"),
        ([{"draft_id": "d"}], None, "option is required"),
        ([{"food_id": "1"}], None, "weight_id is required"),
        ([{"query": "a", "quantity": 0}], None, "quantity must be above 0"),
        (None, {"protein": -5}, "must be zero or more"),
        (None, {"alcohol": 5}, "unknown macros"),
        (None, None, "nothing to log"),
    ],
)
def test_bad_input_fails_before_anything_is_posted(
    logging_client, store, items, macros, message
):
    with pytest.raises(ValueError, match=message):
        batch_logging.log_batch(
            logging_client, store, TODAY, "lunch", items, macros=macros
        )
    assert logging_client.session.calls == []
