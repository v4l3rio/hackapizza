from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from config import DASHBOARD


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

    async def dump_data(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, DASHBOARD.run_dump, self.current_turn_id)

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

