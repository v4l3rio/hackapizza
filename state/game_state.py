from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from infrastructure.http_client import HttpClient


@dataclass
class GameState:
    turn_id: int = 0
    phase: str = "unknown"
    balance: float = 0.0
    inventory: dict[str, int] = field(default_factory=dict)
    recipes: list[dict[str, Any]] = field(default_factory=list)
    menu_items: list[dict[str, Any]] = field(default_factory=list)
    active_meals: list[dict[str, Any]] = field(default_factory=list)
    restaurants: list[dict[str, Any]] = field(default_factory=list)

    async def refresh_all(self, http: "HttpClient") -> None:
        """Refresh all state from the game server."""
        results = await http.get_all(turn_id=self.turn_id)
        self.balance = results.get("balance", self.balance)
        self.inventory = results.get("inventory", self.inventory)
        self.recipes = results.get("recipes", self.recipes)
        self.menu_items = results.get("menu_items", self.menu_items)
        self.active_meals = results.get("active_meals", self.active_meals)
        self.restaurants = results.get("restaurants", self.restaurants)

    def cookable_dishes(self) -> list[dict[str, Any]]:
        """Return recipes for which we have all required ingredients."""
        cookable = []
        for recipe in self.recipes:
            ingredients = recipe.get("ingredients", {})
            can_cook = all(
                self.inventory.get(ing, 0) >= qty
                for ing, qty in ingredients.items()
            )
            if can_cook:
                cookable.append(recipe)
        return cookable

    def ingredient_cost(self, recipe: dict[str, Any]) -> float:
        """Estimate ingredient cost for a recipe (placeholder: 1 unit/ingredient)."""
        return float(len(recipe.get("ingredients", {})))
