"""
Ingredient prestige and rarity data loader.

Reads resources/ingredients/ingredient_frequencies.yaml once and exposes:
  - get_ingredient_data()   → raw dict keyed by ingredient name
  - dish_prestige_score()   → combined prestige + rarity score for a recipe

Score formula
-------------
  score = weighted_avg_prestige(ingredients) + rarity_bonus(ingredients)

  rarity_bonus per ingredient:
    frequency=1  (ultra-rare) → +20 points
    frequency=65 (most common) → +0 points
    Linear interpolation between those extremes.

  This rewards dishes that use rare, high-prestige ingredients,
  which correlates with Space Sage client preferences.

Score interpretation (approximate, based on observed data):
  >= 75  → PRESTIGE dish  (rare ingredients, Space Sage territory)
  62–75  → STANDARD dish  (typical game balance)
  <  62  → BUDGET dish    (common/cheap ingredients, Explorer territory)
"""
from __future__ import annotations

import os
from typing import Any

import yaml

_YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "resources", "ingredients", "ingredient_frequencies.yaml",
)

# Global defaults derived from the YAML
GLOBAL_AVG_PRESTIGE: float = 62.77
_MAX_FREQUENCY: int = 65   # Carne di Balena spaziale — most common
_RARITY_BONUS_MAX: float = 20.0  # points added for frequency=1 ingredient

_cache: dict[str, dict[str, Any]] | None = None


def get_ingredient_data() -> dict[str, dict[str, Any]]:
    """Load and cache ingredient data from YAML. Returns dict keyed by name."""
    global _cache
    if _cache is None:
        try:
            with open(_YAML_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            _cache = raw.get("ingredients", {})
        except Exception:
            _cache = {}
    return _cache


def dish_prestige_score(recipe: dict[str, Any]) -> float:
    """
    Compute a prestige score for a recipe.

    Returns a float; higher = rarer and more prestigious ingredients.
    Unknow ingredients fall back to global averages.
    """
    data = get_ingredient_data()
    ingredients: dict[str, int] = recipe.get("ingredients", {})
    if not ingredients:
        return GLOBAL_AVG_PRESTIGE

    total_qty = sum(ingredients.values()) or 1

    weighted_prestige = 0.0
    rarity_bonus = 0.0

    for ing, qty in ingredients.items():
        ing_data = data.get(ing, {})
        prestige = float(ing_data.get("avg_prestige", GLOBAL_AVG_PRESTIGE))
        frequency = int(ing_data.get("frequency", _MAX_FREQUENCY))

        # Clamp frequency so rarity stays in [0, 1]
        frequency = max(1, min(frequency, _MAX_FREQUENCY))
        rarity = (_MAX_FREQUENCY - frequency) / _MAX_FREQUENCY  # 0..1

        weight = qty / total_qty
        weighted_prestige += prestige * weight
        rarity_bonus += rarity * _RARITY_BONUS_MAX * weight

    return weighted_prestige + rarity_bonus


def dish_avg_prep_time_ms(recipe: dict[str, Any]) -> float:
    """
    Estimate average preparation time for a recipe based on its ingredients.
    Useful for identifying fast dishes (Galactic Explorer / Astrobaron) vs
    slow prestigious ones (Space Sage).
    Falls back to the global average (9000ms) for unknown ingredients.
    """
    data = get_ingredient_data()
    ingredients: dict[str, int] = recipe.get("ingredients", {})
    if not ingredients:
        return 9000.0

    total_qty = sum(ingredients.values()) or 1
    weighted_time = 0.0

    for ing, qty in ingredients.items():
        ing_data = data.get(ing, {})
        prep_ms = float(ing_data.get("avg_preparation_time_ms", 9000.0))
        weighted_time += prep_ms * (qty / total_qty)

    return weighted_time
