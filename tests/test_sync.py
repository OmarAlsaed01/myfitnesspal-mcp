import datetime
import threading

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


class FakeNoteResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return {"item": {"body": self._body}}

    def raise_for_status(self):
        pass


class FakeNoteSession:
    def __init__(self, body):
        self.body = body

    def get(self, url, **kwargs):
        return FakeNoteResponse(self.body)


class FakeSyncClient:
    BASE_URL_SECURE = "https://www.myfitnesspal.com/"

    def __init__(self, note_body="today felt great"):
        self.fetched = []
        self.weights = {}
        self.access_token = "fake-token"
        self.user_id = "user-1"
        self.effective_username = "tester"
        self.session = FakeNoteSession(note_body)

    def get_date(self, day):
        self.fetched.append(day)
        return FakeDay()

    def get_measurements(self, kind, earliest):
        self.measurements_earliest = earliest
        return {day: value for day, value in self.weights.items() if day >= earliest}


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


def test_refresh_day_keeps_shown_nutrients_and_nulls_hidden_ones(store):
    class SodiumSugarDay(FakeDay):
        def __init__(self):
            super().__init__()
            self.totals = {**self.totals, "sodium": 2300, "sugar": 0}
            self.meals = [
                FakeMeal("lunch", [FakeEntry("Soup", {"calories": 200, "sodium": 900})])
            ]

    class SodiumSugarClient(FakeSyncClient):
        def get_date(self, day):
            return SodiumSugarDay()

    sync.refresh_day(store, SodiumSugarClient(), TODAY)

    nutrition = store.nutrition(TODAY.isoformat())
    assert nutrition["sodium"] == 2300.0
    assert nutrition["sugar"] == 0.0
    assert "fiber" not in nutrition
    [entry] = store.diary(TODAY.isoformat())
    assert entry["sodium"] == 900.0
    assert "sugar" not in entry
    assert "fiber" not in entry


def test_refresh_day_clears_a_total_the_diary_no_longer_shows(store):
    store.upsert_nutrition(TODAY.isoformat(), calories=1800.0, fiber=5.0)
    store.mark_diary_synced(TODAY.isoformat())

    sync.refresh_day(store, FakeSyncClient(), TODAY)

    assert "fiber" not in store.nutrition(TODAY.isoformat())


def test_diary_nutrition_reads_abbreviated_column_headers():
    nutrition = sync.diary_nutrition(
        {
            "calories": 2000,
            "carbohydrates": 210,
            "sat fat": 21,
            "polyunsat fat": 9,
            "mono fat": 14,
            "trans fat": 0,
            "cholest": 280,
            "potass.": 3400,
            "vit a": 45,
            "vitamin c": 120,
            "calcium": 60,
            "iron": 85,
            "sugar alcohols": 3,
            "vit d": 10,
        }
    )
    assert nutrition["carbs"] == 210.0
    assert nutrition["saturated_fat"] == 21.0
    assert nutrition["polyunsaturated_fat"] == 9.0
    assert nutrition["monounsaturated_fat"] == 14.0
    assert nutrition["trans_fat"] == 0.0
    assert nutrition["cholesterol"] == 280.0
    assert nutrition["potassium"] == 3400.0
    assert nutrition["vitamin_a"] == 45.0
    assert nutrition["vitamin_c"] == 120.0
    assert nutrition["calcium"] == 60.0
    assert nutrition["iron"] == 85.0
    assert nutrition["sugar"] is None
    assert nutrition["fiber"] is None


def _logged_day(store, day, **fields):
    store.upsert_nutrition(day, calories=2000.0, **fields)
    store.mark_diary_synced(day)


def _report(*days_and_totals):
    return {
        "outcome": {
            "results": [
                {"date": f"{day.month}/{day.day}", "total": total}
                for day, total in days_and_totals
            ]
        }
    }


def test_fill_hidden_nutrients_requests_only_what_the_diary_hid(
    store, client, make_response
):
    _logged_day(store, "2026-07-05", sodium=2300.0)
    store.upsert_nutrition("2026-07-06", calories=None)
    store.mark_diary_synced("2026-07-06")
    _logged_day(store, "2026-07-07", sodium=2100.0)
    store.upsert_nutrition(TODAY.isoformat(), weight=180.0)
    client.session.route(
        "GET",
        "nutrition/Potassium/5.json",
        make_response(
            json_data=_report(
                (datetime.date(2026, 7, 5), 2900),
                (datetime.date(2026, 7, 6), 0),
                (datetime.date(2026, 7, 7), 3200),
                (TODAY, 0),
            )
        ),
    )

    failed = sync.fill_hidden_nutrients(
        store,
        client,
        datetime.date(2026, 7, 1),
        TODAY,
        nutrients=("sodium", "potassium"),
        today=TODAY,
    )

    assert failed == []
    report_calls = [url for _, url, _ in client.session.calls if "reports" in url]
    assert len(report_calls) == 1
    assert store.nutrition("2026-07-05")["potassium"] == 2900.0
    assert store.nutrition("2026-07-07")["potassium"] == 3200.0
    assert store.nutrition("2026-07-07")["sodium"] == 2100.0
    assert "potassium" not in store.nutrition("2026-07-06")
    assert "potassium" not in store.nutrition(TODAY.isoformat())


def test_fill_hidden_nutrients_skips_a_failed_report(store, client, make_response):
    _logged_day(store, "2026-07-07")
    client.session.route("GET", "nutrition/Potassium/", make_response(status_code=500))
    client.session.route(
        "GET",
        "nutrition/Iron/",
        make_response(json_data=_report((datetime.date(2026, 7, 7), 40), (TODAY, 0))),
    )

    failed = sync.fill_hidden_nutrients(
        store,
        client,
        datetime.date(2026, 7, 7),
        datetime.date(2026, 7, 7),
        nutrients=("potassium", "iron"),
        today=TODAY,
    )

    assert failed == ["potassium"]
    nutrition = store.nutrition("2026-07-07")
    assert "potassium" not in nutrition
    assert nutrition["iron"] == 40.0


def test_fill_hidden_nutrients_reports_auth_failures_instead_of_raising(
    store, client, monkeypatch
):
    _logged_day(store, "2026-07-07")

    def forbidden(*args):
        raise MyfitnesspalLoginError("HTTP 403")

    monkeypatch.setattr(sync.nutrient_reports, "daily_totals", forbidden)

    failed = sync.fill_hidden_nutrients(
        store,
        client,
        datetime.date(2026, 7, 7),
        TODAY,
        nutrients=("iron",),
        today=TODAY,
    )

    assert failed == ["iron"]


def test_fill_hidden_nutrients_asks_for_at_most_the_report_window(
    store, client, make_response
):
    _logged_day(store, (TODAY - datetime.timedelta(days=362)).isoformat())
    client.session.route("GET", "nutrition/Iron/", make_response(json_data={}))

    sync.fill_hidden_nutrients(
        store,
        client,
        datetime.date(2025, 1, 1),
        TODAY,
        nutrients=("iron",),
        today=TODAY,
    )

    [(_, url, _)] = client.session.calls
    assert url.endswith(f"/Iron/{sync.nutrient_reports.MAX_LOOKBACK_DAYS}.json")


def test_fill_hidden_nutrients_stays_inside_the_report_window(store, client):
    _logged_day(store, "2025-07-08")
    store.upsert_nutrition("2026-07-07", calories=None)
    store.mark_diary_synced("2026-07-07")

    sync.fill_hidden_nutrients(
        store, client, datetime.date(2025, 7, 1), TODAY, today=TODAY
    )

    assert client.session.calls == []


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
    assert client.measurements_earliest == window_start
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
