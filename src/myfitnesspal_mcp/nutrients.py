import re

# Every nutrient MFP tracks beyond calories and the three macros, named as its
# v2 food API names them. Vitamin A, vitamin C, calcium, and iron are percent
# of daily value (MFP's default goal for each is 100); cholesterol, sodium, and
# potassium are mg; the rest are g.
EXTRA_NUTRIENTS = (
    "saturated_fat",
    "polyunsaturated_fat",
    "monounsaturated_fat",
    "trans_fat",
    "cholesterol",
    "sodium",
    "potassium",
    "fiber",
    "sugar",
    "vitamin_a",
    "vitamin_c",
    "calcium",
    "iron",
)

# Drafts carry only these four of the extras: carrying all 13 made drafts 2-3x
# bigger, and these are the ones food entries fill in most often (checked on
# 2026-10-29 against search results for a protein shake and a fast-food entree).
DRAFT_MICROS = ("sodium", "potassium", "fiber", "sugar")

# Diary page column headers are MFP abbreviations, lowercased by
# python-myfitnesspal. Only "Sodium", "Sugar", "Fiber" and the library's own
# "Potass." have been seen live, so the others match by prefix ("Sat Fat" and
# "Saturated Fat" alike). Vitamins and sugar match exactly so a "Vit D" or
# "Sugar Alcohols" column isn't mistaken for them.
_HEADER_PREFIXES = (
    ("sat", "saturated_fat"),
    ("poly", "polyunsaturated_fat"),
    ("mono", "monounsaturated_fat"),
    ("trans", "trans_fat"),
    ("chol", "cholesterol"),
    ("sodium", "sodium"),
    ("potass", "potassium"),
    ("fib", "fiber"),
    ("dietaryfib", "fiber"),
    ("calc", "calcium"),
    ("iron", "iron"),
)
_HEADER_NAMES = {
    "sugar": "sugar",
    "sugars": "sugar",
    "vita": "vitamin_a",
    "vitamina": "vitamin_a",
    "vitc": "vitamin_c",
    "vitaminc": "vitamin_c",
}


def diary_column_nutrient(header: str) -> str | None:
    letters = re.sub(r"[^a-z]", "", header.lower())
    if letters in _HEADER_NAMES:
        return _HEADER_NAMES[letters]
    for prefix, nutrient in _HEADER_PREFIXES:
        if letters.startswith(prefix):
            return nutrient
    return None
