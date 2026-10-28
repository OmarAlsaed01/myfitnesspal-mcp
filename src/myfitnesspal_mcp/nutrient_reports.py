from datetime import date, timedelta
from urllib import parse

from . import mfp_web

REPORT_NAMES = {
    "saturated_fat": "Saturated Fat",
    "polyunsaturated_fat": "Polyunsaturated Fat",
    "monounsaturated_fat": "Monounsaturated Fat",
    "trans_fat": "Trans Fat",
    "cholesterol": "Cholesterol",
    "sodium": "Sodium",
    "potassium": "Potassium",
    "fiber": "Fiber",
    "sugar": "Sugar",
    "vitamin_a": "Vitamin A",
    "vitamin_c": "Vitamin C",
    "calcium": "Calcium",
    "iron": "Iron",
}

# A report lists one result per day, oldest first, ending on MFP's today in
# the account's time zone, each labelled "M/DD" with no year. Totals matched
# the diary page's own columns for days up to 360 back (Oct 2026), so the
# window stops just short of a year.
MAX_LOOKBACK_DAYS = 364


def report_url(client, nutrient: str, days: int) -> str:
    name = parse.quote(REPORT_NAMES[nutrient])
    return mfp_web.site_url(
        client, f"api/services/reports/results/nutrition/{name}/{days}.json"
    )


def month_day(label: str | None) -> tuple[int, int] | None:
    try:
        month, day = (int(part) for part in str(label).split("/"))
    except ValueError:
        return None
    return month, day


def mfp_today(last_label: str | None, today: date) -> date | None:
    """The day the report ends on: ours, or one either side of it for an
    account in another time zone."""
    for candidate in (today, today + timedelta(days=1), today - timedelta(days=1)):
        if month_day(last_label) == (candidate.month, candidate.day):
            return candidate
    return None


def daily_totals(client, nutrient: str, since: date, today: date) -> dict[date, float]:
    """MFP's daily totals for one nutrient from `since` through MFP's today,
    whatever the Diary Settings show. The report always ends on MFP's today
    (0 days is an HTTP 400), so older days cost no extra requests, only a
    longer reply. One spare day keeps `since` in the window when MFP's today
    is a day ahead of ours. Days are dated by position back from the last
    result, and a result whose label disagrees is skipped."""
    days = min(max((today - since).days + 2, 1), MAX_LOOKBACK_DAYS)
    body = mfp_web.get_json(client, report_url(client, nutrient, days)) or {}
    results = body.get("outcome", {}).get("results") or []
    last_day = mfp_today(results[-1].get("date"), today) if results else None
    if last_day is None:
        return {}
    totals = {}
    for back, result in enumerate(reversed(results)):
        day = last_day - timedelta(days=back)
        if day < since:
            break
        total = result.get("total")
        if month_day(result.get("date")) == (day.month, day.day) and isinstance(
            total, (int, float)
        ):
            totals[day] = float(total)
    return totals
