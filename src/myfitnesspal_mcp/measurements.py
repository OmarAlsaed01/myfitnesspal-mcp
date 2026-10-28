import logging
from datetime import date

from .mfp_web import api_headers, api_url, get_json, require_status

logger = logging.getLogger(__name__)


def get_weights(client, start: date, end: date) -> dict[date, float]:
    """The library's get_measurements scrapes /measurements/edit, a Next.js
    page that needs the NextAuth session. The saved session's NextAuth part
    stops working within a day or two of auth (/api/auth/session comes back
    empty) while the Bearer token keeps working, so from then on the page
    307s to /account/logout. This API uses the Bearer token. It filters by
    `from`/`to`, both inclusive, and returns up to 500 items a page, paged by
    `offset`. It silently ignores `type` and other date param names, so the
    filter happens here. Values are in the account's display unit, as with
    set_weight (checked on a pounds account)."""
    weights: dict[date, float] = {}
    seen_ids: set = set()
    offset = 0
    while True:
        params = {"from": start.isoformat(), "to": end.isoformat(), "offset": offset}
        page = get_json(client, api_url(client, "v2/measurements", params=params))
        items = (page or {}).get("items") or []
        ids = {item.get("id") for item in items}
        if not ids - seen_ids:
            # A page with nothing new ends the loop, so MFP ignoring `offset`
            # can't page forever.
            if items:
                logger.warning(
                    "v2/measurements repeated a page at offset %d; keeping %d weights",
                    offset,
                    len(weights),
                )
            return weights
        seen_ids |= ids
        for item in items:
            if item.get("type") == "Weight" and item.get("value") is not None:
                weights[date.fromisoformat(item["date"])] = float(item["value"])
        offset += len(items)
        if not page.get("has_more"):
            return weights


def set_weight(client, day: date, value: float) -> dict:
    """Value is in the account's display unit (kg or lbs): the v2 API stores
    and echoes whatever unit the account is configured with."""
    resp = client.session.post(
        api_url(client, "v2/measurements"),
        json={"items": [{"type": "Weight", "value": value, "date": day.isoformat()}]},
        headers=api_headers(
            client, {"Accept": "application/json", "Content-Type": "application/json"}
        ),
    )
    require_status(resp, "/v2/measurements", (200, 201))
    item = resp.json()["items"][0]
    return {"day": item["date"], "weight": item["value"], "unit": item.get("unit")}
