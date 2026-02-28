from __future__ import annotations

import json

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)
from config import DEFAULT_BID_FLAT, MAX_BID_BALANCE_FRACTION, BID_CLEARING_MULTIPLIER


class BiddingAgent(Agent):
    """
    Handles the 'closed_bid' phase using LLM reasoning.

    Strategy:
      - Identifies target recipes (highest-value dishes we can build toward)
      - Calculates required ingredients vs current inventory
      - Turn 1: flat bid per ingredient
      - Turn > 1: bid = clearing_price * multiplier (from memory)
      - Cap: max MAX_BID_BALANCE_FRACTION of balance in total bids
    """

    name = "bidding_agent"
    system_prompt = (
        "You are the bidding agent for our restaurant in a competitive cooking game. "
        "Your goal is to acquire the ingredients needed to cook as many dishes as possible. "
        "Analyze the current inventory, needed ingredients, and clearing prices. "
        "Compute competitive bids (clearing_price * multiplier, or flat default if no history). "
        "Respect the budget cap and submit all bids in a single call to submit_bids. "
        "Always call submit_bids exactly once — do not skip it if there are needed ingredients."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        super().__init__(client=get_llm_client(), max_steps=3)

    # ------------------------------------------------------------------ tools

    @tool(
        name="submit_bids",
        description=(
            "Submit all ingredient bids for this auction round. "
            "Accepts a JSON array of bid objects, each with keys: "
            "ingredient (str), quantity (int), bid (float). "
            'Example: [{"ingredient": "flour", "quantity": 5, "bid": 55.0}]'
        ),
    )
    async def submit_bids(self, bids_json: str) -> str:
        """Submit bids to the closed-bid auction."""
        try:
            bids = json.loads(bids_json)
            result = await self._mcp.closed_bid(bids)
            turn = self._state.turn_id if self._state else "?"
            log("closed_bid", turn, "tool", f"Submitted {len(bids)} bids: {result}")
            return f"Bids submitted successfully: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("closed_bid", turn, "tool", f"submit_bids failed: {exc}")
            return f"Error submitting bids: {exc}"

    # ------------------------------------------------------------------ phase entry

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        self._state = state
        self._strat = memory
        self._mcp = mcp

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
            needed = self._compute_needed(state)

            if not needed:
                log("closed_bid", state.turn_id, "agent", "No ingredients needed — skipping bid")
                return

            task = (
                f"Current balance: {state.balance:.2f}\n"
                f"Budget cap ({int(MAX_BID_BALANCE_FRACTION * 100)}% of balance): {budget:.2f}\n"
                f"Current inventory: {json.dumps(state.inventory)}\n"
                f"All recipes: {json.dumps(state.recipes)}\n"
                f"Pre-computed needed ingredients (max shortfall per ingredient): {json.dumps(needed)}\n"
                f"Last known clearing prices: {json.dumps(memory.clearing_prices)}\n"
                f"Bid pricing rule: clearing_price * {BID_CLEARING_MULTIPLIER} if available, "
                f"else flat default = {DEFAULT_BID_FLAT}.\n"
                f"Total bid spend MUST NOT exceed {budget:.2f}.\n\n"
                "Compute the best bids for each needed ingredient, respect the budget cap, "
                "then call submit_bids once with the complete JSON array."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("closed_bid", state.turn_id, "agent", f"BiddingAgent failed: {exc}")

    # ------------------------------------------------------------------ helpers

    def _compute_needed(self, state: GameState) -> dict[str, int]:
        """Find max shortfall per ingredient across all recipes."""
        needed: dict[str, int] = {}
        for recipe in state.recipes:
            for ing, qty in recipe.get("ingredients", {}).items():
                have = state.inventory.get(ing, 0)
                shortfall = max(0, qty - have)
                if shortfall > 0:
                    needed[ing] = max(needed.get(ing, 0), shortfall)
        return needed
