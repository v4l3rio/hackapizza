from __future__ import annotations

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
from utils.logger import log, log_error
from utils.tracing import get_tracer
from config import DEFAULT_BID_FLAT, MAX_BID_BALANCE_FRACTION, BID_SERVINGS_MULTIPLIER

tracer = get_tracer(__name__)


class BiddingAgent:
    """
    Handles the 'closed_bid' phase with deterministic, weighted bidding (no LLM).

    Quantity logic:
      - SUM of each ingredient across ALL focus recipes × BID_SERVINGS_MULTIPLIER
        (e.g., Recipe A needs 2 flour + Recipe B needs 3 flour → bid for 10 flour total)
      - Minus what we already have in inventory.

    Bid price logic:
      - Turn 1 (no clearing history): distribute budget evenly across all needed units,
        capped at DEFAULT_BID_FLAT per unit.
      - Turn 2+: clearing_price × BID_CLEARING_MULTIPLIER from memory.
      - Importance boost: +0–25% for ingredients shared across multiple focus recipes.
      - Final safety: proportional scale-down if total spend still exceeds budget.
    """

    def __init__(self) -> None:
        pass

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        with tracer.start_as_current_span("bidding_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)
            span.set_attribute("balance", state.balance)

            log("closed_bid", state.turn_id, "agent", "BiddingAgent started")
            log(
                "closed_bid",
                state.turn_id,
                "state",
                f"Balance={state.balance:.2f} | Inventory={state.inventory}",
            )

            budget = state.balance * MAX_BID_BALANCE_FRACTION
            needed = self._compute_needed(state, memory.focus_recipes)

            if not needed:
                log("closed_bid", state.turn_id, "agent", "No ingredients needed — skipping bid")
                return

            # Count how many focus recipes each needed ingredient appears in
            focus_recipe_objs = state.recipes
            if memory.focus_recipes:
                focus_set = set(memory.focus_recipes)
                focus_recipe_objs = [r for r in state.recipes if r.get("name") in focus_set]

            ingredient_freq: dict[str, int] = {}
            for recipe in focus_recipe_objs:
                for ing in recipe.get("ingredients", {}).keys():
                    if ing in needed:
                        ingredient_freq[ing] = ingredient_freq.get(ing, 0) + 1

            num_focus = len(focus_recipe_objs)

            # Turn 1 (no clearing prices): distribute budget proportionally across units
            # so we never blindly over-commit with the flat default.
            if not memory.clearing_prices:
                total_units = sum(needed.values())
                effective_flat = max(1, int(min(
                    DEFAULT_BID_FLAT,
                    budget / total_units if total_units > 0 else DEFAULT_BID_FLAT,
                )))
                log(
                    "closed_bid",
                    state.turn_id,
                    "bids",
                    f"Turn-1 flat bid: {effective_flat:.2f}/unit "
                    f"(budget={budget:.2f}, units={total_units})",
                )
            else:
                effective_flat = DEFAULT_BID_FLAT  # overridden by clearing price in bid_for()

            # Build bids: base price from memory, boosted by ingredient importance
            bids = []
            for ing, qty in needed.items():
                base_bid = memory.bid_for(ing, effective_flat)
                freq = ingredient_freq.get(ing, 1)
                # Boost bid up to +25% for ingredients shared across all focus recipes
                if num_focus > 1:
                    importance = (freq - 1) / (num_focus - 1)  # 0.0..1.0
                    boost = 1.0 + 0.25 * importance
                else:
                    boost = 1.0
                bid_price = max(1, int(round(base_bid * boost)))
                bids.append({"ingredient": ing, "quantity": qty, "bid": bid_price})

            # Safety: proportionally scale down if total still exceeds budget
            total_spend = sum(b["bid"] * b["quantity"] for b in bids)
            if total_spend > budget and total_spend > 0:
                scale = budget / total_spend
                bids = [
                    {
                        "ingredient": b["ingredient"],
                        "quantity": b["quantity"],
                        "bid": max(1, int(round(b["bid"] * scale))),
                    }
                    for b in bids
                ]
                total_spend = sum(b["bid"] * b["quantity"] for b in bids)

            log(
                "closed_bid",
                state.turn_id,
                "bids",
                f"Submitting {len(bids)} bids, total~{total_spend:.2f}: {bids}",
            )

            try:
                result = await mcp.closed_bid(bids)
                log("closed_bid", state.turn_id, "tool", f"Bids submitted: {result}")
            except Exception as exc:
                span.record_exception(exc)
                log_error("closed_bid", state.turn_id, "agent", f"closed_bid failed: {exc}")

    # ------------------------------------------------------------------ helpers

    def _compute_needed(self, state: GameState, focus_recipes: list[str]) -> dict[str, int]:
        """
        Total ingredient shortfall = SUM of each ingredient across ALL focus recipes
        × BID_SERVINGS_MULTIPLIER, minus current inventory.

        Using SUM (not max) because during a serving phase we may need to cook
        multiple different focus recipes simultaneously.
        """
        recipes = state.recipes
        if focus_recipes:
            focus_set = set(focus_recipes)
            recipes = [r for r in recipes if r.get("name") in focus_set]

        total_needed: dict[str, int] = {}
        for recipe in recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                total_needed[ing] = total_needed.get(ing, 0) + qty * BID_SERVINGS_MULTIPLIER

        needed: dict[str, int] = {}
        for ing, total in total_needed.items():
            have = state.inventory.get(ing, 0)
            shortfall = max(0, total - have)
            if shortfall > 0:
                needed[ing] = shortfall
        return needed
