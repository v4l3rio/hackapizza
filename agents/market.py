from __future__ import annotations

from datapizza.tools.mcp_client import MCPClient

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from utils.logger import log, log_error
from utils.tracing import get_tracer
import config
from config import MARKET_MAX_BUY_MULTIPLIER, MARKET_MAX_BUY_FLAT

tracer = get_tracer(__name__)


class MarketAgent:
    """
    Deterministic market agent (no LLM).

    - Selling is BLOCKED: we never list our ingredients on the market.
    - Buying is programmatic: scan market for ingredients we need at price
      below clearing_price * MARKET_MAX_BUY_MULTIPLIER (or MARKET_MAX_BUY_FLAT
      if no clearing price is known). Buy only from other teams.
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
                # The server may return ingredient as a nested dict — flatten to string
                if isinstance(ingredient, dict):
                    ingredient = ingredient.get("name") or ingredient.get("ingredient_name") or ""
                ingredient = str(ingredient) if ingredient else ""

                price_raw = entry.get("price") or entry.get("unit_price") or entry.get("unitPrice", 9999)
                price = float(price_raw)
                seller_id = entry.get("seller_id") or entry.get("restaurant_id") or entry.get("createdByRestaurantId")
                side = entry.get("side", "SELL")
                status = entry.get("status", "open")

                # Only buy SELL entries that are open
                if side not in ("SELL", "sell") or status not in ("open", "active", "OPEN", "ACTIVE", "available"):
                    continue

                # Don't buy our own listings
                if seller_id == config.TEAM_ID:
                    continue

                # Only buy ingredients we actually need
                if ingredient not in needed:
                    continue

                # Price check: must be below our threshold
                max_price = self._max_price(ingredient, memory)
                if price > max_price:
                    log(
                        "serving", state.turn_id, "market",
                        f"SKIP {ingredient} @ {price:.2f} (max={max_price:.2f})"
                    )
                    continue

                # Buy it
                try:
                    result = await mcp.call_tool("execute_transaction", {"entry_id": str(entry_id)})
                    qty = entry.get("quantity", "?")
                    log(
                        "serving", state.turn_id, "market",
                        f"BOUGHT {qty}x {ingredient} @ {price:.2f} from team {seller_id}: {result}"
                    )
                    bought += 1
                    # Update needed (reduce or remove)
                    entry_qty = int(entry.get("quantity", 0))
                    if ingredient in needed:
                        needed[ingredient] = max(0, needed[ingredient] - entry_qty)
                        if needed[ingredient] == 0:
                            del needed[ingredient]
                except Exception as exc:
                    log_error(
                        "serving", state.turn_id, "market",
                        f"Failed to buy entry {entry_id}: {exc}"
                    )

            log("serving", state.turn_id, "market", f"Market scan done: bought {bought} entries")

    def _max_price(self, ingredient: str, memory: StrategyMemory) -> float:
        """Max price we're willing to pay for an ingredient."""
        clearing = memory.clearing_prices.get(ingredient)
        if clearing and clearing > 0:
            return clearing * MARKET_MAX_BUY_MULTIPLIER
        return float(MARKET_MAX_BUY_FLAT)

    def _compute_needed(self, state: GameState, focus_recipes: list[str] | None = None) -> dict[str, int]:
        """Ingredients still missing to cook all focus recipes once."""
        total_needed: dict[str, int] = {}
        recipes = state.recipes
        if focus_recipes:
            focus_set = set(focus_recipes)
            recipes = [r for r in recipes if r.get("name") in focus_set]
        for recipe in recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                total_needed[ing] = total_needed.get(ing, 0) + qty

        needed: dict[str, int] = {}
        for ing, total in total_needed.items():
            have = state.inventory.get(ing, 0)
            shortfall = max(0, total - have)
            if shortfall > 0:
                needed[ing] = shortfall
        return needed
