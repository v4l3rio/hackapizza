from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from infrastructure.http_client import HttpClient


def ingredient_cost(recipe: dict[str, Any], clearing_prices: dict[str, float] | None = None) -> float:
    """Estimate ingredient cost using clearing prices if available, else 50/unit default."""
    ingredients = recipe.get("ingredients", {})
    if clearing_prices:
        return sum(clearing_prices.get(ing, 50.0) * qty for ing, qty in ingredients.items())
    return float(len(ingredients)) * 50.0


@dataclass
class GameState:
    turn_id: int = 0
    phase: str = "unknown"
    balance: float = 0.0
    reputation: int = 100
    inventory: dict[str, int] = field(default_factory=dict)
    recipes: list[dict[str, Any]] = field(default_factory=list)
    menu_items: list[dict[str, Any]] = field(default_factory=list)
    active_meals: list[dict[str, Any]] = field(default_factory=list)
    restaurants: list[dict[str, Any]] = field(default_factory=list)

    async def refresh_info(self, http: HttpClient) -> None:
        """Refresh balance, reputation, and inventory from the server."""
        info = await http.get_restaurant_info()
        if isinstance(info, dict):
            self.balance = info.get("balance", self.balance)
            self.reputation = int(info.get("reputation", self.reputation))
            self.inventory = info.get("inventory", self.inventory)

    async def refresh_recipes(self, http: HttpClient) -> None:
        """Refresh available recipes from the server."""
        recipes = await http.get_recipes()
        if isinstance(recipes, list):
            self.recipes = recipes

    async def refresh_menu(self, http: HttpClient) -> None:
        """Refresh own restaurant menu from the server."""
        menu = await http.get_restaurant_menu()
        if isinstance(menu, list):
            self.menu_items = menu

    async def refresh_restaurants(self, http: HttpClient) -> None:
        """Refresh the list of all restaurants from the server."""
        restaurants = await http.get_restaurants()
        if isinstance(restaurants, list):
            self.restaurants = restaurants

    async def refresh_meals(self, http: HttpClient) -> None:
        """Refresh active meals from the server."""
        meals = await http.get_meals(self.turn_id)
        if isinstance(meals, list):
            self.active_meals = meals

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
