from __future__ import annotations

from typing import Any

import aiohttp

from utils.logger import log, log_error


class MCPClient:
    """
    Wraps all game MCP tool calls using the JSON-RPC protocol at POST /mcp.
    Each method corresponds to one MCP tool exposed by the game server.

    Auth is handled via the x-api-key header — team_id is NOT included in params.
    """

    def __init__(self, mcp_url: str, team_id: int, api_key: str) -> None:
        self.mcp_url = mcp_url.rstrip("/")
        self.team_id = team_id
        self._headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }
        self._call_id = 0

    async def _call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """
        Execute an MCP tool via JSON-RPC at POST /mcp.

        Request format:
            {"jsonrpc": "2.0", "id": N, "method": "tools/call",
             "params": {"name": tool, "arguments": {...}}}

        Response format:
            {"jsonrpc": "2.0", "id": N,
             "result": {"content": [{"type": "text", "text": "..."}], "isError": false}}
        """
        self._call_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._call_id,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": arguments,
            },
        }
        log("MCP", "?", "call", f"→ {tool}({arguments})")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.mcp_url, json=payload, headers=self._headers
            ) as resp:
                body = await resp.json()
                if not resp.ok:
                    log_error("MCP", "?", tool, f"HTTP {resp.status}: {body}")
                    resp.raise_for_status()

                # Handle JSON-RPC error
                if "error" in body:
                    err = body["error"]
                    raise Exception(f"MCP error {err.get('code')}: {err.get('message')}")

                result = body.get("result", body)

                # Handle MCP-level error (isError flag)
                if result.get("isError"):
                    err_text = ""
                    for c in result.get("content", []):
                        if c.get("type") == "text":
                            err_text = c.get("text", "")
                    raise Exception(f"Tool '{tool}' returned error: {err_text}")

                log("MCP", "?", "result", f"← {tool}: {result}")
                return result

    # --- Auction ---

    async def closed_bid(self, bids: list[dict[str, Any]]) -> Any:
        """
        Submit closed bids for ingredients.
        bids: list of {"ingredient": str, "bid": float, "quantity": int}
        Note: field is 'bid', not 'price' (per server spec).
        """
        return await self._call_tool("closed_bid", {"bids": bids})

    # --- Menu ---

    async def save_menu(self, items: list[dict[str, Any]]) -> Any:
        """
        Set the restaurant menu.
        items: list of {"name": str, "price": float}
        """
        return await self._call_tool("save_menu", {"items": items})

    # --- Market ---

    async def create_market_entry(
        self,
        ingredient: str,
        quantity: int,
        price: float,
        side: str = "SELL",
    ) -> Any:
        """
        List an ingredient for sale (or buy order) on the market.
        side: 'BUY' or 'SELL' (default SELL)
        """
        return await self._call_tool(
            "create_market_entry",
            {
                "side": side,
                "ingredient_name": ingredient,
                "quantity": quantity,
                "price": price,
            },
        )

    async def execute_transaction(self, entry_id: str | int) -> Any:
        """Buy a market entry by ID."""
        return await self._call_tool(
            "execute_transaction",
            {"market_entry_id": int(entry_id)},
        )

    async def delete_market_entry(self, entry_id: str | int) -> Any:
        """Remove own market listing."""
        return await self._call_tool(
            "delete_market_entry",
            {"market_entry_id": int(entry_id)},
        )

    # --- Kitchen ---

    async def prepare_dish(self, name: str) -> Any:
        """Start preparing a dish. Triggers 'preparation_complete' SSE when done."""
        return await self._call_tool(
            "prepare_dish",
            {"dish_name": name},
        )

    async def serve_dish(self, name: str, client_id: str) -> Any:
        """Serve a prepared dish to a client."""
        return await self._call_tool(
            "serve_dish",
            {"dish_name": name, "client_id": client_id},
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
