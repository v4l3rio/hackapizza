from __future__ import annotations

from typing import Any

import aiohttp

from utils.logger import log, log_error


class HttpClient:
    def __init__(self, base_url: str, team_id: int, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.team_id = team_id
        self._headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    # --- Endpoints ---

    async def get_recipes(self) -> list[dict[str, Any]]:
        data = await self._get("/recipes")
        return data if isinstance(data, list) else data.get("recipes", [])

    async def get_restaurants(self) -> list[dict[str, Any]]:
        data = await self._get("/restaurants")
        return data if isinstance(data, list) else data.get("restaurants", [])

    async def get_meals(self, turn_id: int | None = None) -> list[dict[str, Any]]:
        """
        Active meals / current orders.
        Requires turn_id and restaurant_id as query params per server spec.
        """
        params: dict[str, Any] = {"restaurant_id": self.team_id}
        if turn_id is not None:
            params["turn_id"] = turn_id
        data = await self._get("/meals", params=params)
        return data if isinstance(data, list) else data.get("meals", [])

    async def get_bid_history(self, turn_id: int | None = None) -> list[dict[str, Any]]:
        """
        Bid history for a given turn.
        Requires turn_id query param per server spec.
        """
        params: dict[str, Any] = {}
        if turn_id is not None:
            params["turn_id"] = turn_id
        data = await self._get("/bid_history", params=params)
        return data if isinstance(data, list) else data.get("bid_history", [])

    async def get_market_entries(self) -> list[dict[str, Any]]:
        data = await self._get("/market/entries")
        return data if isinstance(data, list) else data.get("entries", [])

    async def get_restaurant_info(self) -> dict[str, Any]:
        """
        Fetch own restaurant info including balance and inventory.
        Endpoint: GET /restaurant/:id  (singular, not /restaurants/:id)
        """
        return await self._get(f"/restaurant/{self.team_id}")

    async def get_all(self, turn_id: int = 0) -> dict[str, Any]:
        """Fetch all relevant state in parallel."""
        import asyncio
        results = await asyncio.gather(
            self.get_restaurant_info(),
            self.get_recipes(),
            self.get_meals(turn_id=turn_id),
            self.get_restaurants(),
            return_exceptions=True,
        )

        info, recipes, meals, restaurants = results

        out: dict[str, Any] = {}

        if isinstance(info, dict):
            out["balance"] = info.get("balance", 0.0)
            out["inventory"] = info.get("inventory", {})
        else:
            log_error("HTTP", "?", "get_all", f"get_restaurant_info failed: {info}")

        out["recipes"] = recipes if isinstance(recipes, list) else []
        out["active_meals"] = meals if isinstance(meals, list) else []
        out["restaurants"] = restaurants if isinstance(restaurants, list) else []

        return out
