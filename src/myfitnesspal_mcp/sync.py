import logging
import threading
from contextlib import contextmanager
from datetime import date, timedelta

from . import config, measurements, notes, nutrient_goals, nutrient_reports
from .mfp_client import is_auth_error
from .nutrients import EXTRA_NUTRIENTS, diary_column_nutrient
from .store import Store

logger = logging.getLogger(__name__)

_poll_lock = threading.Lock()


@contextmanager
def tolerating_failures(description: str):
    """Auth failures propagate so the caller can refresh the session and retry;
    everything else is logged and skipped."""
    try:
        yield
    except Exception as exc:
        if is_auth_error(exc):
            raise
        logger.warning("%s failed: %s", description, exc)


def as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_number(values: dict, *keys) -> float | None:
    for key in keys:
        number = as_float(values.get(key))
        if number is not None:
            return number
    return None


def days_to_fetch(cached: set[str], lookback: int, today: date) -> list[date]:
    """Today is always refetched because the diary is live; past days only when
    the cache has not synced their diary yet."""
    past = (today - timedelta(days=offset) for offset in range(1, lookback))
    return [today] + [day for day in past if day.isoformat() not in cached]


def diary_nutrition(totals: dict) -> dict:
    """Totals keyed by the diary page's column headers. Nutrients whose column
    is hidden in MFP's Diary Settings come back None."""
    extras = dict.fromkeys(EXTRA_NUTRIENTS)
    for header, value in totals.items():
        nutrient = diary_column_nutrient(header)
        if nutrient is not None and extras[nutrient] is None:
            extras[nutrient] = as_float(value)
    return {
        "calories": first_number(totals, "calories"),
        "protein": first_number(totals, "protein"),
        "carbs": first_number(totals, "carbohydrates", "carbs"),
        "fat": first_number(totals, "fat"),
        **extras,
    }


def refresh_day(store: Store, client, day: date) -> None:
    mfp_day = client.get_date(day)
    key = day.isoformat()

    store.upsert_nutrition(
        key,
        **diary_nutrition(mfp_day.totals),
        water_ml=as_float(mfp_day.water),
        goal_calories=first_number(mfp_day.goals or {}, "calories"),
        goal_protein=first_number(mfp_day.goals or {}, "protein"),
        goal_carbs=first_number(mfp_day.goals or {}, "carbohydrates", "carbs"),
        goal_fat=first_number(mfp_day.goals or {}, "fat"),
    )

    store.replace_diary(
        key,
        [
            {
                "meal": str(meal.name).title(),
                "name": entry.name,
                **diary_nutrition(entry.totals),
            }
            for meal in mfp_day.meals
            for entry in meal.entries
        ],
    )

    with tolerating_failures(f"note fetch for {day}"):
        store.set_note(key, notes.get_note(client, day))

    with tolerating_failures(f"nutrient goals fetch for {day}"):
        refresh_goal_period(store, client, day, mfp_day.goals or {})

    store.mark_diary_synced(key)


def _goal_period_current(
    period: dict, day: date, diary_goals: dict, today: date
) -> bool:
    """An open-ended period stays open only until the user changes a goal, so
    it is trusted for the day it was fetched. Either way, macro goals that no
    longer match the diary page mean the goals changed since."""
    if period["valid_to"] >= today.isoformat() > period["fetched_on"]:
        return False
    cached = period["goals"][day.weekday()]
    for name, fields in (
        ("protein", ("protein",)),
        ("carbs", ("carbohydrates", "carbs")),
        ("fat", ("fat",)),
    ):
        shown = first_number(diary_goals, *fields)
        if shown is not None and abs((cached.get(name) or 0.0) - shown) >= 0.5:
            return False
    return True


def refresh_goal_period(
    store: Store, client, day: date, diary_goals: dict, *, today: date | None = None
) -> None:
    today = today or date.today()
    period = store.goal_period(day.isoformat())
    if period is not None and _goal_period_current(period, day, diary_goals, today):
        return
    store.save_goal_period(
        nutrient_goals.goal_period(client, day), fetched_on=today.isoformat()
    )


def fill_hidden_nutrients(
    store: Store,
    client,
    start: date,
    end: date,
    *,
    nutrients: tuple[str, ...] = EXTRA_NUTRIENTS,
    today: date | None = None,
) -> list[str]:
    """Fills the day totals the diary page didn't show from MFP's nutrient
    reports: one request per nutrient still missing in [start, end], and none
    for a nutrient the Diary Settings show. Returns the nutrients whose report
    failed. Those are logged rather than raised, even when they look like auth
    errors: the caller asked for extra data, and a report MFP won't serve
    shouldn't cost the whole call a session refresh and a reconnect hint."""
    today = today or date.today()
    # Two short of the cap leaves room for the spare day daily_totals adds.
    oldest = today - timedelta(days=nutrient_reports.MAX_LOOKBACK_DAYS - 2)
    start = max(start, oldest)
    failed: list[str] = []
    if start > end:
        return failed
    for nutrient in nutrients:
        missing = store.days_missing_nutrient(
            nutrient, start.isoformat(), end.isoformat()
        )
        if not missing:
            continue
        try:
            totals = nutrient_reports.daily_totals(
                client, nutrient, date.fromisoformat(min(missing)), today
            )
        except Exception as exc:
            logger.warning("%s report failed: %s", nutrient, exc)
            failed.append(nutrient)
            continue
        store.fill_nutrient(
            nutrient,
            {
                day.isoformat(): total
                for day, total in totals.items()
                if day.isoformat() in missing
            },
        )
    return failed


def poll(
    store: Store,
    client,
    days: int | None = None,
    force: bool = False,
    today: date | None = None,
) -> None:
    today = today or date.today()
    if not force and store.last_synced_on() == today.isoformat():
        return
    with _poll_lock:
        if not force and store.last_synced_on() == today.isoformat():
            return
        _poll_window(store, client, days, today)


def _poll_window(store: Store, client, days: int | None, today: date) -> None:
    lookback = days or config.sync_days()
    window_start = today - timedelta(days=lookback - 1)
    cached = store.days_with_synced_diary(
        window_start.isoformat(), (today - timedelta(days=1)).isoformat()
    )

    for day in days_to_fetch(cached, lookback, today):
        with tolerating_failures(f"sync for {day}"):
            refresh_day(store, client, day)

    with tolerating_failures("weight measurements"):
        weights = measurements.get_weights(client, window_start, today)
        for day, value in weights.items():
            if day >= window_start:
                store.upsert_nutrition(day.isoformat(), weight=float(value))

    store.mark_synced(today)
