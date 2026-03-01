from __future__ import annotations

from typing import Any

import aiohttp

from config import WEB_APP_URL
from utils.logger import log, log_error


class HttpClient:
    def __init__(self, base_url: str, team_id: int, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.team_id = team_id
        self._headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers) as resp:
                resp.raise_for_status()
                return await resp.json()

    # --- Endpoints ---

    async def get_best_ingredients(self, n_recipes) -> list[str]:
        url = '/api/recipes/optimal-set'
        params = f'size={n_recipes}'

        data = await self._get(f'{WEB_APP_URL}?{params}')

        return list(data['shared_ingredients'].keys())

    async def get_recipes(self) -> list[dict[str, Any]]:
        data = await self._get(f"/recipes")
        return data if isinstance(data, list) else data.get("recipes", [])

    async def get_restaurants(self) -> list[dict[str, Any]]:
        data = await self._get(f"/restaurants")
        return data if isinstance(data, list) else data.get("restaurants", [])

    async def get_meals(self, turn_id: int = 0, restaurant_id:int = 0) -> list[dict[str, Any]]:
        """Active meals / current orders."""
        params = f"restaurant_id={restaurant_id}"
        params += f"&turn_id={turn_id}"
        data = await self._get(f"/meals?{params}")
        return data if isinstance(data, list) else data.get("meals", [])

    async def get_bid_history(self, turn_id: int = 0) -> list[dict[str, Any]]:
        path = f"/bid_history?turn_id={turn_id}" if turn_id > 0 else "/bid_history"
        data = await self._get(path)
        return data if isinstance(data, list) else data.get("bid_history", [])

    async def get_market_entries(self) -> list[dict[str, Any]]:
        data = await self._get(f"/market/entries")
        return data if isinstance(data, list) else data.get("entries", [])

    async def get_restaurant_info(self) -> dict[str, Any]:
        """Fetch own restaurant info including balance and inventory."""
        return await self._get(f"/restaurant/{self.team_id}")

    async def get_restaurant_menu(self) -> list[dict[str, Any]]:
        """Fetch current menu for own restaurant."""
        data = await self._get(f"/restaurant/{self.team_id}/menu")
        return data if isinstance(data, list) else data.get("menu", data.get("items", []))

    async def get_all(self, turn_id: int = 0) -> dict[str, Any]:
        """Fetch all relevant state in parallel."""
        import asyncio
        results = await asyncio.gather(
            self.get_restaurant_info(),
            self.get_recipes(),
            self.get_meals(turn_id),
            self.get_restaurants(),
            self.get_restaurant_menu(),
            return_exceptions=True,
        )

        info, recipes, meals, restaurants, menu = results

        out: dict[str, Any] = {}

        if isinstance(info, dict):
            out["balance"] = info.get("balance", 0.0)
            out["inventory"] = info.get("inventory", {})
        else:
            log_error("HTTP", "ERROR", "get_all", f"get_restaurant_info failed: {info}")

        out["recipes"] = recipes if isinstance(recipes, list) else []
        out["active_meals"] = meals if isinstance(meals, list) else []
        out["restaurants"] = restaurants if isinstance(restaurants, list) else []
        out["menu_items"] = menu if isinstance(menu, list) else []

        return out
