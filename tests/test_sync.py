import datetime
import threading
from urllib import parse

import pytest
from myfitnesspal.exceptions import MyfitnesspalLoginError

from myfitnesspal_mcp import sync

TODAY = datetime.date(2026, 7, 8)


def test_days_to_fetch_gaps_only():
    existing = {"2026-07-07", "2026-07-05"}
    days = sync.days_to_fetch(existing, lookback=4, today=TODAY)
    assert days == [TODAY, datetime.date(2026, 7, 6)]


def test_days_to_fetch_always_includes_today():
    days = sync.days_to_fetch({TODAY.isoformat()}, lookback=1, today=TODAY)
    assert days == [TODAY]


def test_first_number_prefers_first_present_key():
    assert sync.first_number({"carbohydrates": 10}, "carbohydrates", "carbs") == 10.0
    assert sync.first_number({"carbs": 5}, "carbohydrates", "carbs") == 5.0
    assert sync.first_number({}, "carbohydrates", "carbs") is None


class FakeEntry:
    def __init__(self, name, totals):
        self.name = name
        self.totals = totals


class FakeMeal:
    def __init__(self, name, entries):
        self.name = name
        self.entries = entries


class FakeDay:
    def __init__(self):
        self.totals = {
            "calories": 2100,
            "protein": 150,
            "carbohydrates": 200,
            "fat": 70,
        }
        self.goals = {"calories": 2200}
        self.water = 750
        self.meals = [
            FakeMeal(
                "breakfast", [FakeEntry("Oats", {"calories": 300, "protein": 10})]
            ),
            FakeMeal("lunch", []),
        ]


class FakeJsonResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeSyncSession:
    def __init__(self, client):
        self.client = client
        self.measurement_queries = []

    def get(self, url, **kwargs):
        if "v2/measurements" in url:
            query = dict(parse.parse_qsl(parse.urlsplit(url).query))
            self.measurement_queries.append(query)
            return FakeJsonResponse(self._weights_page(query))
        return FakeJsonResponse({"item": {"body": self.client.note_body}})

    def _weights_page(self, query):
        start = datetime.date.fromisoformat(query["from"])
        end = datetime.date.fromisoformat(query["to"])
        items = [
            {"type": "Weight", "value": value, "date": day.isoformat()}
            for day, value in sorted(self.client.weights.items(), reverse=True)
            if start <= day <= end
        ]
        return {"items": items, "has_more": False, "total_entries": len(items)}


class FakeSyncClient:
    BASE_URL_SECURE = "https://www.myfitnesspal.com/"
    BASE_API_URL = "https://api.myfitnesspal.com/"

    def __init__(self, note_body="today felt great"):
        self.fetched = []
        self.weights = {}
        self.note_body = note_body
        self.access_token = "fake-token"
        self.user_id = "user-1"
        self.effective_username = "tester"
        self.session = FakeSyncSession(self)

    def get_date(self, day):
        self.fetched.append(day)
        return FakeDay()


def test_refresh_day_populates_store(store):
    client = FakeSyncClient()
    sync.refresh_day(store, client, TODAY)
    nutrition = store.nutrition(TODAY.isoformat())
    assert nutrition["calories"] == 2100.0
    assert nutrition["carbs"] == 200.0
    assert nutrition["water_ml"] == 750.0
    assert nutrition["goal_calories"] == 2200.0
    entries = store.diary(TODAY.isoformat())
    assert entries == [
        {
            "meal": "Breakfast",
            "name": "Oats",
            "calories": 300.0,
            "protein": 10.0,
            "carbs": None,
            "fat": None,
        }
    ]
    assert store.note(TODAY.isoformat()) == "today felt great"


def test_poll_skips_when_synced_today(store, monkeypatch):
    client = FakeSyncClient()
    store.mark_synced(TODAY)
    sync.poll(store, client, today=TODAY)
    assert client.fetched == []
    sync.poll(store, client, force=True, days=1, today=TODAY)
    assert client.fetched != []


def test_concurrent_polls_fetch_once(store):
    today_fetch_started = threading.Event()
    let_fetch_finish = threading.Event()

    class SlowClient(FakeSyncClient):
        def get_date(self, day):
            if day == TODAY:
                today_fetch_started.set()
                let_fetch_finish.wait(5)
            return super().get_date(day)

    client = SlowClient()
    first = threading.Thread(target=sync.poll, args=(store, client, 2, False, TODAY))
    first.start()
    assert today_fetch_started.wait(5)
    second = threading.Thread(target=sync.poll, args=(store, client, 2, False, TODAY))
    second.start()
    let_fetch_finish.set()
    first.join(5)
    second.join(5)

    assert client.fetched.count(TODAY) == 1


def test_poll_records_weights(store):
    client = FakeSyncClient()
    client.weights = {TODAY: 80.0, TODAY - datetime.timedelta(days=400): 90.0}
    sync.poll(store, client, days=3, force=True, today=TODAY)
    assert store.nutrition(TODAY.isoformat())["weight"] == 80.0


def test_poll_backfills_weight_for_already_cached_day(store):
    """A weigh-in lands on a day whose nutrition is already cached.

    Such a day is absent from `days_to_fetch`, so keying the measurement query
    off the fetched days would strand the weight permanently.
    """
    window_start = TODAY - datetime.timedelta(days=2)
    yesterday = TODAY - datetime.timedelta(days=1)
    for day in (window_start, yesterday):
        store.upsert_nutrition(day.isoformat(), calories=2000.0)
        store.mark_diary_synced(day.isoformat())

    client = FakeSyncClient()
    client.weights = {yesterday: 79.5}
    sync.poll(store, client, days=3, force=True, today=TODAY)

    assert client.fetched == [TODAY]
    assert client.session.measurement_queries[-1]["from"] == window_start.isoformat()
    assert store.nutrition(yesterday.isoformat())["weight"] == 79.5


def test_poll_fetches_past_day_that_only_has_a_weigh_in(store):
    """`fitness_log_weight` on a past date writes a weight-only row. Gap-fill
    must still fetch that day's diary rather than treat the row as cached."""
    yesterday = TODAY - datetime.timedelta(days=1)
    store.upsert_nutrition(yesterday.isoformat(), weight=80.0)

    client = FakeSyncClient()
    sync.poll(store, client, days=2, force=True, today=TODAY)

    assert client.fetched == [TODAY, yesterday]
    nutrition = store.nutrition(yesterday.isoformat())
    assert nutrition["calories"] == 2100.0
    assert nutrition["weight"] == 80.0


def test_poll_retries_day_whose_fetch_failed(store):
    """A transient failure in refresh_day followed by the weight backfill used
    to leave a weight-only row that blocked every later gap-fill."""
    yesterday = TODAY - datetime.timedelta(days=1)

    class FlakyClient(FakeSyncClient):
        def __init__(self):
            super().__init__()
            self.failing = {yesterday}

        def get_date(self, day):
            if day in self.failing:
                raise RuntimeError("MFP returned 502")
            return super().get_date(day)

    client = FlakyClient()
    client.weights = {yesterday: 80.0}
    sync.poll(store, client, days=2, force=True, today=TODAY)
    assert store.nutrition(yesterday.isoformat())["calories"] is None

    client.failing.clear()
    client.fetched.clear()
    sync.poll(store, client, days=2, force=True, today=TODAY)
    assert yesterday in client.fetched
    assert store.nutrition(yesterday.isoformat())["calories"] == 2100.0


def test_poll_propagates_auth_errors(store):
    class ExpiredClient(FakeSyncClient):
        def get_date(self, day):
            raise MyfitnesspalLoginError("session expired")

    with pytest.raises(MyfitnesspalLoginError):
        sync.poll(store, ExpiredClient(), days=2, force=True)


def test_poll_skips_bad_days_without_auth_errors(store):
    class FlakyClient(FakeSyncClient):
        def get_date(self, day):
            self.fetched.append(day)
            if len(self.fetched) == 1:
                raise ValueError("parse error")
            return FakeDay()

    client = FlakyClient()
    sync.poll(store, client, days=2, force=True)
    assert len(client.fetched) == 2
