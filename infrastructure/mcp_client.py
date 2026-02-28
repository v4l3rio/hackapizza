from __future__ import annotations

from typing import Any

import aiohttp

from utils.logger import log, log_error


class MCPClient:
    """
    Wraps all game MCP tool calls.
    Each method corresponds to one MCP tool exposed by the game server.
    """

    def __init__(self, mcp_url: str, team_id: int, api_key: str) -> None:
        self.mcp_url = mcp_url.rstrip("/")
        self.team_id = team_id
        self._headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

    async def _call_tool(self, tool: str, params: dict[str, Any]) -> Any:
        url = f"{self.mcp_url}/tools/{tool}"
        log("MCP", "?", "call", f"→ {tool}({params})")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=params, headers=self._headers) as resp:
                body = await resp.json()
                if not resp.ok:
                    log_error("MCP", "?", tool, f"HTTP {resp.status}: {body}")
                    resp.raise_for_status()
                log("MCP", "?", "result", f"← {tool}: {body}")
                return body

    # --- Auction ---

    async def closed_bid(self, bids: list[dict[str, Any]]) -> Any:
        """
        Submit closed bids for ingredients.
        bids: list of {"ingredient": str, "quantity": int, "price": float}
        """
        return await self._call_tool("closed_bid", {"bids": bids, "team_id": self.team_id})

    # --- Menu ---

    async def save_menu(self, items: list[dict[str, Any]]) -> Any:
        """
        Set the restaurant menu.
        items: list of {"name": str, "price": float, "description": str}
        """
        return await self._call_tool("save_menu", {"items": items, "team_id": self.team_id})

    # --- Market ---

    async def create_market_entry(
        self,
        ingredient: str,
        quantity: int,
        price: float,
    ) -> Any:
        """List an ingredient for sale on the market."""
        return await self._call_tool(
            "create_market_entry",
            {
                "ingredient": ingredient,
                "quantity": quantity,
                "price": price,
                "team_id": self.team_id,
            },
        )

    async def execute_transaction(self, entry_id: str) -> Any:
        """Buy a market entry by ID."""
        return await self._call_tool(
            "execute_transaction",
            {"entry_id": entry_id, "team_id": self.team_id},
        )

    async def delete_market_entry(self, entry_id: str) -> Any:
        """Remove own market listing."""
        return await self._call_tool(
            "delete_market_entry",
            {"entry_id": entry_id, "team_id": self.team_id},
        )

    # --- Kitchen ---

    async def prepare_dish(self, name: str) -> Any:
        """Start preparing a dish. Triggers 'preparation_complete' SSE when done."""
        return await self._call_tool(
            "prepare_dish",
            {"name": name, "team_id": self.team_id},
        )

    async def serve_dish(self, name: str, client_id: str) -> Any:
        """Serve a prepared dish to a client."""
        return await self._call_tool(
            "serve_dish",
            {"name": name, "client_id": client_id, "team_id": self.team_id},
        )

    # --- Restaurant ---

    async def update_restaurant_is_open(self, is_open: bool) -> Any:
        """Open or close the restaurant."""
        return await self._call_tool(
            "update_restaurant_is_open",
            {"is_open": is_open, "team_id": self.team_id},
        )

    # --- Communication ---

    async def send_message(self, recipient_id: int, text: str) -> Any:
        """Send a message to another restaurant."""
        return await self._call_tool(
            "send_message",
            {"recipient_id": recipient_id, "text": text, "team_id": self.team_id},
        )
