from __future__ import annotations

from datapizza.tools.mcp_client import MCPClient

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from utils.logger import log, log_error
from utils.tracing import get_tracer
import config
from config import MARKET_MAX_BUY_FLAT

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
        """Serving phase: programmatic buying of cheap ingredients we need."""
        with tracer.start_as_current_span("market_agent.execute_serving") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("serving", state.turn_id, "market", "MarketAgent: scanning market for cheap buys")

            needed = self._compute_needed(state, memory.focus_recipes)
            if not needed:
                log("serving", state.turn_id, "market", "No ingredients needed from market")
                return

            log("serving", state.turn_id, "market", f"Need: {needed}")

            try:
                entries = await http.get_market_entries()
            except Exception as exc:
                log_error("serving", state.turn_id, "market", f"Failed to fetch market: {exc}")
                return

            bought = 0
            for entry in entries:
                entry_id = entry.get("id") or entry.get("market_entry_id")
                ingredient = entry.get("ingredient_name") or entry.get("ingredient", "")
                if isinstance(ingredient, dict):
                    ingredient = ingredient.get("name") or ingredient.get("ingredient_name") or ""
                ingredient = str(ingredient) if ingredient else ""

                price = float(entry.get("price") or entry.get("unit_price") or entry.get("unitPrice", 9999))
                seller_id = entry.get("seller_id") or entry.get("restaurant_id") or entry.get("createdByRestaurantId")
                side = entry.get("side", "SELL")
                status = entry.get("status", "open")

                if side.upper() != "SELL" or status.lower() not in ("open", "active", "available"):
                    continue
                if seller_id == config.TEAM_ID:
                    continue
                if ingredient not in needed:
                    continue
                if price > MARKET_MAX_BUY_FLAT:
                    log("serving", state.turn_id, "market", f"SKIP {ingredient} @ {price:.2f} (max={MARKET_MAX_BUY_FLAT})")
                    continue

                try:
                    await mcp.call_tool("execute_transaction", {"entry_id": str(entry_id)})
                    qty = entry.get("quantity", "?")
                    log("serving", state.turn_id, "market", f"BOUGHT {qty}x {ingredient} @ {price:.2f} from team {seller_id}")
                    bought += 1
                    entry_qty = int(entry.get("quantity", 0))
                    needed[ingredient] = max(0, needed[ingredient] - entry_qty)
                    if needed[ingredient] == 0:
                        del needed[ingredient]
                except Exception as exc:
                    log_error("serving", state.turn_id, "market", f"Failed to buy entry {entry_id}: {exc}")

            log("serving", state.turn_id, "market", f"Market scan done: bought {bought} entries")

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
