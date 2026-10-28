import datetime
import json
import logging
from pathlib import Path

from myfitnesspal_mcp import measurements

TODAY = datetime.date(2026, 7, 8)
FIXTURES = Path(__file__).parent / "fixtures"


def cap_requests(client, limit):
    """A paging bug fails the test instead of hanging it."""
    uncapped_get = client.session.get

    def get(url, **kwargs):
        if len(client.session.calls) >= limit:
            raise AssertionError(f"more than {limit} requests")
        return uncapped_get(url, **kwargs)

    client.session.get = get


def weight(item_id, value, day=TODAY):
    return {"id": item_id, "type": "Weight", "value": value, "date": day.isoformat()}


def test_get_weights_pages_and_keeps_only_weight(client, make_response):
    first, second = json.loads((FIXTURES / "measurements_pages.json").read_text())
    client.session.route("GET", "offset=0", make_response(json_data=first))
    client.session.route("GET", "offset=2", make_response(json_data=second))
    cap_requests(client, 3)

    weights = measurements.get_weights(client, datetime.date(2026, 7, 1), TODAY)

    assert weights == {TODAY: 180.4, datetime.date(2026, 7, 5): 181.0}
    urls = [url for method, url, _ in client.session.calls if method == "GET"]
    assert len(urls) == 2
    assert all(url.startswith(client.BASE_API_URL + "v2/measurements?") for url in urls)
    assert "from=2026-07-01&to=2026-07-08" in urls[0]


def test_get_weights_stops_on_an_empty_page(client, make_response):
    client.session.route(
        "GET",
        "v2/measurements",
        make_response(json_data={"items": [], "has_more": True, "total_entries": 1}),
    )
    cap_requests(client, 2)

    assert measurements.get_weights(client, TODAY, TODAY) == {}
    assert len(client.session.calls) == 1


def test_get_weights_stops_when_offset_is_ignored(client, make_response, caplog):
    page = {"items": [weight("m-1", 180.0)], "has_more": True, "total_entries": 1200}
    client.session.route("GET", "v2/measurements", make_response(json_data=page))
    cap_requests(client, 3)

    with caplog.at_level(logging.WARNING):
        weights = measurements.get_weights(client, TODAY, TODAY)

    assert weights == {TODAY: 180.0}
    assert len(client.session.calls) == 2
    assert "repeated a page" in caplog.text


def test_get_weights_skips_weights_without_a_value(client, make_response):
    yesterday = TODAY - datetime.timedelta(days=1)
    page = {
        "items": [weight("m-2", None), weight("m-1", 181.2, yesterday)],
        "has_more": False,
        "total_entries": 2,
    }
    client.session.route("GET", "v2/measurements", make_response(json_data=page))

    assert measurements.get_weights(client, yesterday, TODAY) == {yesterday: 181.2}


def test_set_weight_posts_v2_items(client, make_response):
    client.session.route(
        "POST",
        "v2/measurements",
        make_response(
            status_code=200,
            json_data={
                "items": [
                    {
                        "type": "Weight",
                        "value": 175.0,
                        "date": "2026-07-08",
                        "unit": "pounds",
                    }
                ]
            },
        ),
    )
    result = measurements.set_weight(client, TODAY, 175.0)
    assert result == {"day": "2026-07-08", "weight": 175.0, "unit": "pounds"}
    method, url, kwargs = client.session.calls[-1]
    assert "v2/measurements" in url
    assert kwargs["json"] == {
        "items": [{"type": "Weight", "value": 175.0, "date": "2026-07-08"}]
    }
