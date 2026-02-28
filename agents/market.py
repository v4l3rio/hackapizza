from __future__ import annotations

import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
from infrastructure.http_client import HttpClient
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class MarketAgent(Agent):
    """
    Handles market operations using LLM reasoning.

    - 'waiting' phase: sell surplus ingredients at a slight premium over clearing price.
    - 'serving' phase: opportunistically buy missing ingredients at fair prices.
    """

    name = "market_agent"
    system_prompt = (
        "You are the market agent for our restaurant. "
        "You manage ingredient trading: selling surplus and buying what we need. "
        "Use the available tools to list ingredients for sale or buy from the market. "
        "Base prices on the last known clearing prices. "
        "Sell surplus at clearing_price * 1.05; buy only when price <= clearing_price."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        self._http: HttpClient | None = None
        super().__init__(client=get_llm_client(), max_steps=8)

    # ------------------------------------------------------------------ tools

    @tool(
        name="list_ingredient_for_sale",
        description=(
            "List an ingredient on the open market for sale. "
            "Provide the ingredient name, quantity to sell, and asking price per unit."
        ),
    )
    async def list_ingredient_for_sale(
        self, ingredient: str, quantity: int, price: float
    ) -> str:
        """Create a market listing to sell an ingredient."""
        try:
            result = await self._mcp.create_market_entry(ingredient, quantity, price)
            turn = self._state.turn_id if self._state else "?"
            log("waiting", turn, "tool", f"Listed {quantity}x {ingredient} @ {price}: {result}")
            return f"Listed {quantity}x {ingredient} @ {price:.2f}: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("waiting", turn, "tool", f"list_ingredient_for_sale failed: {exc}")
            return f"Error listing {ingredient}: {exc}"

    @tool(
        name="get_market_listings",
        description=(
            "Fetch all current market listings. "
            "Returns a JSON array of available entries with id, ingredient, quantity, price, seller_id."
        ),
    )
    async def get_market_listings(self) -> str:
        """Retrieve open market entries from the game server."""
        try:
            entries = await self._http.get_market_entries()
            turn = self._state.turn_id if self._state else "?"
            log("serving", turn, "tool", f"Fetched {len(entries)} market entries")
            return json.dumps(entries)
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("serving", turn, "tool", f"get_market_listings failed: {exc}")
            return f"Error fetching market: {exc}"

    @tool(
        name="buy_market_entry",
        description=(
            "Buy an ingredient listing from the market by its entry ID. "
            "Only buy if the price is at or below the clearing price."
        ),
    )
    async def buy_market_entry(self, entry_id: str) -> str:
        """Execute a market transaction to purchase an ingredient listing."""
        try:
            result = await self._mcp.execute_transaction(entry_id)
            turn = self._state.turn_id if self._state else "?"
            log("serving", turn, "tool", f"Bought market entry {entry_id}: {result}")
            return f"Purchase successful for entry {entry_id}: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("serving", turn, "tool", f"buy_market_entry failed: {exc}")
            return f"Error buying entry {entry_id}: {exc}"

    # ------------------------------------------------------------------ phase entries

    async def execute_waiting(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        """Called during the 'waiting' phase: sell surplus ingredients."""
        self._state = state
        self._strat = memory
        self._mcp = mcp

        with tracer.start_as_current_span("market_agent.execute_waiting") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("waiting", state.turn_id, "market", "MarketAgent: checking surplus to sell")

            surplus = self._compute_surplus(state)
            if not surplus:
                log("waiting", state.turn_id, "market", "No surplus to sell")
                return

            task = (
                f"Current inventory: {json.dumps(state.inventory)}\n"
                f"Recipes and their ingredient requirements: {json.dumps(state.recipes)}\n"
                f"Surplus ingredients (beyond what we need): {json.dumps(surplus)}\n"
                f"Last clearing prices: {json.dumps(memory.clearing_prices)}\n\n"
                "List each surplus ingredient for sale at clearing_price * 1.05 "
                "(or 10.0 if no clearing price). "
                "Call list_ingredient_for_sale once per surplus ingredient."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "market", f"MarketAgent waiting failed: {exc}")

    async def execute_serving(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
        http: HttpClient,
    ) -> None:
        """Called during the 'serving' phase: buy missing ingredients at fair prices."""
        self._state = state
        self._strat = memory
        self._mcp = mcp
        self._http = http

        with tracer.start_as_current_span("market_agent.execute_serving") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("serving", state.turn_id, "market", "MarketAgent: scanning market for good buys")

            needed = self._compute_needed(state)
            if not needed:
                log("serving", state.turn_id, "market", "No ingredients needed from market")
                return

            task = (
                f"Current inventory: {json.dumps(state.inventory)}\n"
                f"Needed ingredients (shortfall): {json.dumps(needed)}\n"
                f"Last clearing prices: {json.dumps(memory.clearing_prices)}\n"
                f"Our team_id (do not buy our own listings): {state.__class__.__name__}\n\n"
                "First call get_market_listings to see what is available. "
                "Then buy any listing where the ingredient is needed AND price <= clearing_price. "
                "Skip listings for ingredients we don't need or that are too expensive. "
                "Call buy_market_entry for each good purchase."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("serving", state.turn_id, "market", f"MarketAgent serving failed: {exc}")

    # ------------------------------------------------------------------ helpers

    def _compute_surplus(self, state: GameState) -> dict[str, int]:
        """Ingredients we have beyond what all recipes need."""
        needed: dict[str, int] = {}
        for recipe in state.recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                needed[ing] = max(needed.get(ing, 0), qty)

        surplus: dict[str, int] = {}
        for ing, have in state.inventory.items():
            need = needed.get(ing, 0)
            if have > need:
                surplus[ing] = have - need
        return surplus

    def _compute_needed(self, state: GameState) -> dict[str, int]:
        """Ingredients still missing to cook recipes we can't cook yet."""
        needed: dict[str, int] = {}
        for recipe in state.recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                have = state.inventory.get(ing, 0)
                shortfall = max(0, qty - have)
                if shortfall > 0:
                    needed[ing] = max(needed.get(ing, 0), shortfall)
        return needed
