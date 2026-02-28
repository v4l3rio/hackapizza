from __future__ import annotations

from datapizza.tools.mcp_client import MCPClient

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from utils.logger import log
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class MarketAgent:
    """
    Deterministic market agent (no LLM).

    - Selling is BLOCKED: we never list our ingredients on the market.
    - Buying is programmatic: scan market for ingredients we need at price
      below MARKET_MAX_BUY_FLAT. Buy only from other teams.
    """

    async def execute_waiting(self, state: GameState) -> None:
        """Waiting phase: selling is BLOCKED. Do nothing."""
        log("waiting", state.turn_id, "market", "MarketAgent: selling BLOCKED — skipping")

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
