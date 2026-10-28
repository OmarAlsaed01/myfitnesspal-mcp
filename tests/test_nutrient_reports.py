import datetime

import pytest

from myfitnesspal_mcp import nutrient_reports
from myfitnesspal_mcp.nutrients import EXTRA_NUTRIENTS

TODAY = datetime.date(2026, 7, 8)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("7/08", TODAY),
        ("7/8", TODAY),
        ("7/9", TODAY + datetime.timedelta(days=1)),
        ("7/07", TODAY - datetime.timedelta(days=1)),
        ("7/10", None),
        ("13/40", None),
        ("7-8", None),
        ("", None),
        (None, None),
    ],
)
def test_mfp_today_is_ours_or_a_day_either_side(label, expected):
    assert nutrient_reports.mfp_today(label, TODAY) == expected


def test_every_extra_nutrient_has_a_report():
    assert set(nutrient_reports.REPORT_NAMES) == set(EXTRA_NUTRIENTS)


def test_report_url_quotes_the_report_name(client):
    url = nutrient_reports.report_url(client, "saturated_fat", 7)
    assert url == (
        "https://www.myfitnesspal.com/api/services/reports/results/"
        "nutrition/Saturated%20Fat/7.json"
    )


def _results(*pairs):
    return {"outcome": {"results": [{"date": d, "total": t} for d, t in pairs]}}


def test_daily_totals_covers_since_through_today(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Potassium/4.json",
        make_response(
            json_data=_results(("7/06", 3000), ("7/07", 3150.5), ("7/08", None))
        ),
    )

    totals = nutrient_reports.daily_totals(
        client, "potassium", datetime.date(2026, 7, 6), TODAY
    )

    assert totals == {
        datetime.date(2026, 7, 6): 3000.0,
        datetime.date(2026, 7, 7): 3150.5,
    }


def test_daily_totals_drops_days_before_since(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Iron/2.json",
        make_response(json_data=_results(("7/07", 30), ("7/08", 45))),
    )

    totals = nutrient_reports.daily_totals(client, "iron", TODAY, TODAY)

    assert totals == {TODAY: 45.0}


def test_daily_totals_reaches_since_when_mfp_is_a_day_ahead(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Iron/2.json",
        make_response(json_data=_results(("7/08", 45), ("7/09", 10))),
    )

    totals = nutrient_reports.daily_totals(client, "iron", TODAY, TODAY)

    assert totals[TODAY] == 45.0


def test_daily_totals_dates_days_across_new_year(client, make_response):
    new_year = datetime.date(2027, 1, 1)
    client.session.route(
        "GET",
        "nutrition/Sodium/",
        make_response(json_data=_results(("12/30", 1), ("12/31", 2), ("1/01", 3))),
    )

    totals = nutrient_reports.daily_totals(
        client, "sodium", datetime.date(2026, 12, 30), new_year
    )

    assert totals == {
        datetime.date(2026, 12, 30): 1.0,
        datetime.date(2026, 12, 31): 2.0,
        new_year: 3.0,
    }


def test_daily_totals_reads_new_years_day_from_an_account_a_day_ahead(
    client, make_response
):
    client.session.route(
        "GET", "nutrition/Sodium/1.json", make_response(json_data=_results(("1/01", 3)))
    )

    totals = nutrient_reports.daily_totals(
        client, "sodium", datetime.date(2027, 1, 1), datetime.date(2026, 12, 31)
    )

    assert totals == {datetime.date(2027, 1, 1): 3.0}


def test_daily_totals_dates_an_account_a_day_behind(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Sodium/",
        make_response(json_data=_results(("7/06", 1), ("7/07", 2))),
    )

    totals = nutrient_reports.daily_totals(
        client, "sodium", datetime.date(2026, 7, 6), TODAY
    )

    assert totals == {datetime.date(2026, 7, 6): 1.0, datetime.date(2026, 7, 7): 2.0}


def test_daily_totals_skips_a_result_out_of_place(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Sodium/",
        make_response(json_data=_results(("7/05", 1), ("7/07", 2), ("7/08", 3))),
    )

    totals = nutrient_reports.daily_totals(
        client, "sodium", datetime.date(2026, 7, 6), TODAY
    )

    assert totals == {datetime.date(2026, 7, 7): 2.0, TODAY: 3.0}


def test_daily_totals_ignores_a_report_that_ends_elsewhere(client, make_response):
    client.session.route(
        "GET",
        "nutrition/Sodium/",
        make_response(json_data=_results(("6/30", 1), ("7/01", 2))),
    )

    assert nutrient_reports.daily_totals(client, "sodium", TODAY, TODAY) == {}


@pytest.mark.parametrize(
    ("since", "days"),
    [
        (datetime.date(2026, 7, 9), 1),
        (datetime.date(2020, 1, 1), nutrient_reports.MAX_LOOKBACK_DAYS),
    ],
)
def test_daily_totals_keeps_the_window_mfp_accepts(client, make_response, since, days):
    client.session.route("GET", "nutrition/Sugar/", make_response(json_data={}))

    assert nutrient_reports.daily_totals(client, "sugar", since, TODAY) == {}
    [(_, url, _)] = client.session.calls
    assert url.endswith(f"/Sugar/{days}.json")
