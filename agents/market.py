from __future__ import annotations

from datapizza.tools.mcp_client import MCPClient

from config import DEFAULT_PRICE_SELL_MARKET
from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class MarketAgent:
    """
    Deterministic market agent (no LLM).

    - Selling: list surplus ingredients (beyond what focus recipes need) on the
      market at a fixed price DEFAULT_PRICE_SELL during the waiting phase.
    - Buying is programmatic: scan market for ingredients we need at price
      below MARKET_MAX_BUY_FLAT. Buy only from other teams.
    """

    async def execute_waiting(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        """Waiting phase: list surplus ingredients as SELL entries at DEFAULT_PRICE_SELL."""
        log("waiting", state.turn_id, "market", "MarketAgent: selling DISABLED — skipping")
        # surplus = self._compute_surplus(state, memory.focus_recipes)
        #
        # if not surplus:
        #     log("waiting", state.turn_id, "market", "MarketAgent: no surplus to sell — skipping")
        #     return
        #
        # log(
        #     "waiting",
        #     state.turn_id,
        #     "market",
        #     f"MarketAgent: listing {len(surplus)} surplus ingredient(s) at price {DEFAULT_PRICE_SELL}: {surplus}",
        # )
        #
        # for ingredient, quantity in surplus.items():
        #     try:
        #         await mcp.call_tool(
        #             "create_market_entry",
        #             {
        #                 "side": "SELL",
        #                 "ingredient_name": ingredient,
        #                 "quantity": quantity,
        #                 "price": DEFAULT_PRICE_SELL,
        #             },
        #         )
        #         log(
        #             "waiting",
        #             state.turn_id,
        #             "market",
        #             f"SELL listed: {quantity}x {ingredient} @ {DEFAULT_PRICE_SELL}",
        #         )
        #     except Exception as exc:
        #         log_error(
        #             "waiting",
        #             state.turn_id,
        #             "market",
        #             f"Failed to list {ingredient}: {exc}",
        #         )

    async def execute_serving(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
        http: HttpClient,
    ) -> None:
        """Serving phase: buying DISABLED."""
        log("serving", state.turn_id, "market", "MarketAgent: buying DISABLED — skipping")

    def _compute_needed(self, state: GameState, focus_recipes: list[str] | None = None) -> dict[str, int]:
        """Ingredients still missing to cook all focus recipes once."""
        recipes = state.recipes
        if focus_recipes:
            focus_set = set(focus_recipes)
            recipes = [r for r in recipes if r.get("name") in focus_set]

        total_needed: dict[str, int] = {}
        for recipe in recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                total_needed[ing] = total_needed.get(ing, 0) + qty

        return {
            ing: max(0, total - state.inventory.get(ing, 0))
            for ing, total in total_needed.items()
            if total > state.inventory.get(ing, 0)
        }

    def _compute_surplus(self, state: GameState, focus_recipes: list[str] | None = None) -> dict[str, int]:
        """Ingredients we hold beyond what focus recipes require — safe to sell."""
        recipes = state.recipes
        if focus_recipes:
            focus_set = set(focus_recipes)
            recipes = [r for r in recipes if r.get("name") in focus_set]

        # Total units needed across all (focus) recipes
        total_needed: dict[str, int] = {}
        for recipe in recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                total_needed[ing] = total_needed.get(ing, 0) + qty

        surplus: dict[str, int] = {}
        for ing, have in state.inventory.items():
            need = total_needed.get(ing, 0)
            extra = have - need
            if extra > 0:
                surplus[ing] = extra
        return surplus
