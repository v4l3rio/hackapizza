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
            "Accept": "application/json, text/event-stream",
        }

    async def _call_tool(self, tool: str, params: dict[str, Any]) -> Any:
        url = self.mcp_url
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": params,
            },
            "id": 1,
        }
        log("MCP", "?", "call", f"→ {tool}({params})")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers) as resp:
                response = await resp.json(content_type=None)
                if not resp.ok:
                    log_error("MCP", "?", tool, f"HTTP {resp.status}: {response}")
                    resp.raise_for_status()
                tool_result = response.get("result", {})
                if tool_result.get("isError"):
                    error_text = (tool_result.get("content") or [{}])[0].get("text", "unknown error")
                    log_error("MCP", "?", tool, f"Tool error: {error_text}")
                    raise RuntimeError(f"MCP tool error ({tool}): {error_text}")
                log("MCP", "?", "result", f"← {tool}: {tool_result}")
                return tool_result

    # --- Auction ---

    async def closed_bid(self, bids: list[dict[str, Any]]) -> Any:
        """
        Submit closed bids for ingredients.
        bids: list of {"ingredient": str, "quantity": int, "bid": float}
        """
        return await self._call_tool("closed_bid", {"bids": bids})

    # --- Menu ---

    async def save_menu(self, items: list[dict[str, Any]]) -> Any:
        """
        Set the restaurant menu.
        items: list of {"name": str, "price": float, "description": str}
        """
        return await self._call_tool("save_menu", {"items": items})

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
            },
        )

    async def execute_transaction(self, entry_id: str) -> Any:
        """Buy a market entry by ID."""
        return await self._call_tool(
            "execute_transaction",
            {"entry_id": entry_id},
        )

    async def delete_market_entry(self, entry_id: str) -> Any:
        """Remove own market listing."""
        return await self._call_tool(
            "delete_market_entry",
            {"entry_id": entry_id},
        )

    # --- Kitchen ---

    async def prepare_dish(self, name: str) -> Any:
        """Start preparing a dish. Triggers 'preparation_complete' SSE when done."""
        return await self._call_tool(
            "prepare_dish",
            {"name": name},
        )

    async def serve_dish(self, name: str, client_id: str) -> Any:
        """Serve a prepared dish to a client."""
        return await self._call_tool(
            "serve_dish",
            {"name": name, "client_id": client_id},
        )

    # --- Restaurant ---

    async def update_restaurant_is_open(self, is_open: bool) -> Any:
        """Open or close the restaurant."""
        return await self._call_tool(
            "update_restaurant_is_open",
            {"is_open": is_open},
        )

    # --- Communication ---

    async def send_message(self, recipient_id: int, text: str) -> Any:
        """Send a message to another restaurant."""
        return await self._call_tool(
            "send_message",
            {"recipient_id": recipient_id, "text": text},
        )
