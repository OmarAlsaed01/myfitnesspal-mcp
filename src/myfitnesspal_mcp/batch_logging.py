"""Logs several foods to one day in one call. Each add is still checked by
reading the diary back, and that read is reused as the starting page of the
next add, so the caller refreshes the day once at the end instead of once per
food."""

import json
import logging
from datetime import date

from . import config, custom_items, diary, food_logging

logger = logging.getLogger(__name__)

MACRO_UNITS = ("protein", "carbs", "fat")
MICRO_UNITS = ("sodium", "potassium", "fiber", "sugar")

# Public MyFitnessPal foods holding one gram (one mg for sodium and potassium)
# of a single nutrient and nothing else, so logging N servings adds N of it.
# Ids checked through the legacy search on 2026-10-29; MFP_UNIT_FOODS overrides
# any of them with {"sodium": {"food_id": ..., "weight_id": ...}, ...}.
DEFAULT_UNIT_FOODS = {
    "protein": {"food_id": "62185581", "weight_id": "87444186", "name": "Protein 1g"},
    "carbs": {"food_id": "152094942", "weight_id": "211042314", "name": "Carb 1g"},
    "fat": {"food_id": "152094897", "weight_id": "211042259", "name": "Fat 1g"},
    "sodium": {"food_id": "176150011", "weight_id": "245278278", "name": "Sodium 1mg"},
    "potassium": {
        "food_id": "4976036734",
        "weight_id": "5579919214",
        "name": "Potassium 1mg",
    },
    "fiber": {"food_id": "168657689", "weight_id": "235260391", "name": "Fiber 1g"},
    "sugar": {"food_id": "5066066174", "weight_id": "5685000374", "name": "Sugar 1g"},
}

ITEM_KINDS = ("draft_id", "food_id", "query", "custom")


def unit_foods() -> dict[str, dict]:
    raw = config.unit_foods_env()
    if not raw:
        return DEFAULT_UNIT_FOODS
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MFP_UNIT_FOODS is not valid JSON: {exc}") from exc
    unknown = set(overrides) - set(DEFAULT_UNIT_FOODS)
    if unknown:
        raise ValueError(
            f"MFP_UNIT_FOODS has unknown nutrients {sorted(unknown)}; use "
            f"{', '.join(DEFAULT_UNIT_FOODS)}"
        )
    foods = dict(DEFAULT_UNIT_FOODS)
    for nutrient, ids in overrides.items():
        if not ids.get("food_id") or not ids.get("weight_id"):
            raise ValueError(f"MFP_UNIT_FOODS.{nutrient} needs food_id and weight_id")
        foods[nutrient] = {
            "food_id": str(ids["food_id"]),
            "weight_id": str(ids["weight_id"]),
            "name": ids.get("name") or f"{nutrient} unit food",
        }
    return foods


def _unit_entries(amounts: dict | None, nutrients: tuple, label: str) -> list[dict]:
    amounts = amounts or {}
    unknown = set(amounts) - set(nutrients)
    if unknown:
        raise ValueError(
            f"unknown {label} {sorted(unknown)}; use {', '.join(nutrients)}"
        )
    foods = unit_foods()
    entries = []
    for nutrient in nutrients:
        amount = amounts.get(nutrient)
        if not amount:
            continue
        if amount < 0:
            raise ValueError(f"{label}.{nutrient} must be zero or more, not {amount}")
        entries.append(
            {
                "label": f"{label}.{nutrient}",
                "source": "unit",
                "food": foods[nutrient],
                "quantity": float(amount),
            }
        )
    return entries


def _checked_item(index: int, item: dict) -> dict:
    given = [kind for kind in ITEM_KINDS if item.get(kind) is not None]
    if len(given) != 1:
        raise ValueError(
            f"item {index}: pass exactly one of {', '.join(ITEM_KINDS)}, "
            f"not {given or 'none'}"
        )
    kind = given[0]
    if kind == "draft_id" and item.get("option") is None:
        raise ValueError(f"item {index}: option is required with draft_id")
    if kind == "food_id" and item.get("weight_id") is None:
        raise ValueError(f"item {index}: weight_id is required with food_id")
    quantity = item.get("quantity")
    if quantity is not None and not quantity > 0:
        raise ValueError(f"item {index}: quantity must be above 0, not {quantity}")
    return {**item, "source": kind, "label": f"items[{index}]"}


class _Resolver:
    """Turns an item into the food to add. Custom item tabs are read once per
    batch however many custom items it has."""

    def __init__(self, client, store, *, pin: bool):
        self.client = client
        self.store = store
        self.pin = pin
        self._custom_rows: dict[str, list[dict]] = {}

    def food(self, item: dict) -> tuple[dict, float, dict | None]:
        quantity = item.get("quantity")
        if item["source"] == "draft_id":
            body, chosen = food_logging.draft_option(
                self.store, item["draft_id"], int(item["option"])
            )
            food = food_logging.option_food(chosen, item.get("serving"))
            draft_pin = (body, chosen) if self.pin else None
            return food, body["quantity"] if quantity is None else quantity, draft_pin
        if item["source"] == "food_id":
            food = {
                "food_id": str(item["food_id"]),
                "weight_id": str(item["weight_id"]),
                "name": item.get("name") or str(item["food_id"]),
            }
            return food, quantity or 1.0, None
        if item["source"] == "custom":
            return self._custom(item), quantity or 1.0, None
        food, _ = food_logging.unambiguous_food(self.client, self.store, item["query"])
        if food is None:
            raise food_logging.NothingLogged(
                f"{item['query']!r} matches no remembered food and no single exact "
                "name, so it was skipped; pick one with fitness_draft_food and log "
                "it with fitness_log_food"
            )
        return food, quantity or 1.0, None

    def _custom(self, item: dict) -> dict:
        kind = item.get("kind") or "all"
        if kind not in self._custom_rows:
            self._custom_rows[kind] = [
                row
                for plural in custom_items.kinds(kind)
                for row in custom_items.tab_rows(self.client, plural)
            ]
        chosen_item = custom_items.pick(self._custom_rows[kind], item["custom"])
        chosen = custom_items.chosen_serving(chosen_item, item.get("serving"))
        return {
            "food_id": chosen_item["food_id"],
            "weight_id": chosen["weight_id"],
            "name": chosen_item["name"],
            "serving": chosen["serving"],
        }


def log_batch(
    client,
    store,
    day: date,
    meal: str,
    items: list[dict] | None = None,
    *,
    macros: dict | None = None,
    micros: dict | None = None,
    pin: bool = True,
) -> dict:
    planned = [_checked_item(index, item) for index, item in enumerate(items or [])]
    planned += _unit_entries(macros, MACRO_UNITS, "macros")
    planned += _unit_entries(micros, MICRO_UNITS, "micros")
    if not planned:
        raise ValueError("nothing to log: pass items, macros, or micros")
    resolver = _Resolver(client, store, pin=pin)
    # Read before anything is posted, so an expired session fails here, where
    # the auth retry can safely re-run the whole batch.
    page = diary.diary_page(client, day)
    results = []
    for entry in planned:
        result, page = _log_entry(client, resolver, day, meal, entry, page)
        results.append(result)
    logged = sum(1 for result in results if result["ok"])
    return {
        "date": day.isoformat(),
        "meal": meal,
        "logged_count": logged,
        "failed_count": len(results) - logged,
        "results": results,
    }


def _log_entry(
    client, resolver: _Resolver, day: date, meal: str, entry: dict, page: tuple | None
) -> tuple[dict, tuple | None]:
    """Never raises: once anything is posted, an exception reaching the auth
    retry would re-run the batch and add every earlier food again."""
    entry_meal = entry.get("meal") or meal
    try:
        if entry["source"] == "unit":
            food, quantity, draft_pin = entry["food"], entry["quantity"], None
        else:
            food, quantity, draft_pin = resolver.food(entry)
        result, page = food_logging.log_exact_reading_back(
            client, food, day, entry_meal, quantity=float(quantity), page=page
        )
    except Exception as exc:
        logger.warning("batch entry %s failed: %s", entry["label"], exc)
        # After a failure the next add reads the diary afresh.
        return {"entry": entry["label"], "ok": False, "error": str(exc)}, None
    if draft_pin:
        food_logging.pin_draft_choice(resolver.store, *draft_pin, result)
    return {"entry": entry["label"], "ok": True, **result}, page
