from datetime import date

from .mfp_web import api_url, get_json
from .nutrients import EXTRA_NUTRIENTS

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

MACRO_GOALS = {"protein": "protein", "carbs": "carbohydrates", "fat": "fat"}


def _goal_values(goal: dict) -> dict[str, float | None]:
    values = {name: goal.get(field) for name, field in MACRO_GOALS.items()}
    for nutrient in EXTRA_NUTRIENTS:
        value = goal.get(nutrient)
        # MFP stores 0 for a nutrient the user never set a goal for: the poly,
        # mono, and trans fat goals came back 0.0 on an account that never
        # touched them, so a 0 goal means none.
        values[nutrient] = value if value else None
    return values


def goal_period(client, day: date) -> dict:
    """The goal period covering day, from the v2 API the web app's goals page
    reads, with one set of goals per weekday, Monday first. Calories stay with
    the diary page, which already gives them in kcal whatever energy unit the
    account uses."""
    item = get_json(
        client, api_url(client, "v2/nutrient-goals", params={"date": day.isoformat()})
    )["items"][0]
    default = _goal_values(item["default_goal"])
    by_weekday = {
        goal["day_of_week"]: _goal_values(goal)
        for goal in item.get("daily_goals") or []
    }
    return {
        "valid_from": item["valid_from"],
        "valid_to": item["valid_to"],
        "goals": [by_weekday.get(weekday, default) for weekday in WEEKDAYS],
    }
