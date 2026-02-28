from __future__ import annotations

import json

from datapizza.agents import Agent
from datapizza.tools import tool

from config import DEFAULT_BID_FLAT, MAX_BID_BALANCE_FRACTION, DEFAULT_BID_QUANTITY, BID_CLEARING_MULTIPLIER, BID_SERVINGS_MULTIPLIER
from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
from infrastructure.llm_factory import get_llm_client
from utils.ingredient_data import get_ingredient_data
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


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
        "Sei l'agente di offerta del nostro ristorante in un gioco di cucina competitivo. "
        "Il tuo obiettivo è acquisire gli ingredienti necessari per cucinare quanti più piatti possibile. "
        "Analizza l'inventario attuale, gli ingredienti necessari e i prezzi di aggiudicazione. "
        "Calcola offerte competitive (clearing_price * moltiplicatore, o valore fisso predefinito se non c'è storico). "
        "Rispetta il limite di budget e invia tutte le offerte in una singola chiamata a submit_bids. "
        "Chiama submit_bids esattamente una volta — non saltarla se ci sono ingredienti necessari."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        super().__init__(client=get_llm_client(), max_steps=1)

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
                f"Saldo attuale: {state.balance:.2f}\n"
                f"Limite di budget ({int(MAX_BID_BALANCE_FRACTION * 100)}% del saldo): {budget:.2f}\n"
                f"Ingredienti necessari: {json.dumps(needed)}\n"
                f"Ultimi prezzi di aggiudicazione noti: {json.dumps(memory.clearing_prices)}\n"
            #    f"Regola di prezzo: clearing_price * {BID_CLEARING_MULTIPLIER} se esiste storico, "
                f"Regola di valore bid fisso predefinito = {DEFAULT_BID_FLAT}.\n"
            #    f"altrimenti valore fisso predefinito = {DEFAULT_BID_FLAT}.\n"
                f"La spesa totale delle offerte NON deve superare {budget:.2f}.\n\n"
            #    "Per ogni ingrediente necessario, offri clearing_price * moltiplicatore (o valore fisso predefinito). "
                "Rispetta il limite di budget, poi chiama submit_bids una volta con l'array JSON completo."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("closed_bid", state.turn_id, "agent", f"BiddingAgent failed: {exc}")

    # ------------------------------------------------------------------ helpers

    def _compute_needed(self, state: GameState) -> dict[str, int]:
        """Find shortfall per ingredient for BID_SERVINGS_MULTIPLIER servings of focus recipes.

        Only considers ingredients present in ingredient_frequencies.yaml to guard against
        typos or unknown ingredient names coming from the game server.
        """
        valid_ingredients = get_ingredient_data()

        # recipes = state.recipes
        # if focus_recipes:
        #     focus_set = set(focus_recipes)
        #     recipes = [r for r in recipes if r.get("name") in focus_set]

        needed: dict[str, int] = {}
        # for recipe in recipes:
        #     for ing, qty in recipe.get("ingredients", {}).items():
        for ing in valid_ingredients:
            if ing not in valid_ingredients:
                log("closed_bid", state.turn_id, "agent", f"Skipping unknown ingredient: {ing!r}")
                continue
            # have = state.inventory.get(ing, 0)
            shortfall = DEFAULT_BID_QUANTITY # max(0, qty * BID_SERVINGS_MULTIPLIER - have)
            if shortfall > 0:
                needed[ing] = max(needed.get(ing, 0), shortfall)
        return needed
