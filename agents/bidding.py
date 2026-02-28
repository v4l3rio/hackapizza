from __future__ import annotations

import json

from datapizza.agents import Agent
from datapizza.tools import Tool

from config import DEFAULT_BID_FLAT, MAX_BID_BALANCE_FRACTION, BID_SERVINGS_MULTIPLIER
from state.game_state import GameState
from state.memory import StrategyMemory
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
        "Rispetta il limite di budget e invia tutte le offerte in una singola chiamata a closed_bid. "
        "Chiama closed_bid esattamente una volta — non saltarla se ci sono ingredienti necessari."
    )

    def __init__(self, mcp_tools: list[Tool]) -> None:
        self._state: GameState | None = None
        super().__init__(client=get_llm_client(), tools=mcp_tools, max_steps=1)

    # ------------------------------------------------------------------ phase entry

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
    ) -> None:
        self._state = state

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

            focus_label = memory.focus_recipes if memory.focus_recipes else "all recipes"
            task = (
                f"Saldo attuale: {state.balance:.2f}\n"
                f"Limite di budget ({int(MAX_BID_BALANCE_FRACTION * 100)}% del saldo): {budget:.2f}\n"
                f"Ricette focus: {json.dumps(focus_label)}\n"
                f"Ingredienti necessari per le ricette focus ({BID_SERVINGS_MULTIPLIER} porzioni ciascuna, deficit): {json.dumps(needed)}\n"
                f"Ultimi prezzi di aggiudicazione noti: {json.dumps(memory.clearing_prices)}\n"
                f"Regola di prezzo: valore fisso predefinito = {DEFAULT_BID_FLAT}.\n"
                f"La spesa totale delle offerte NON deve superare {budget:.2f}.\n\n"
                "Rispetta il limite di budget, poi chiama closed_bid una volta con l'array completo."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("closed_bid", state.turn_id, "agent", f"BiddingAgent failed: {exc}")

    # ------------------------------------------------------------------ helpers

    def _compute_needed(self, state: GameState) -> dict[str, int]:
        """Find shortfall per ingredient.

        Only considers ingredients present in ingredient_frequencies.yaml to guard against
        typos or unknown ingredient names coming from the game server.
        """
        valid_ingredients = get_ingredient_data()
        needed: dict[str, int] = {}
        for ing in valid_ingredients:
            have = state.inventory.get(ing, 0)
            shortfall = 5
            if shortfall > 0:
                needed[ing] = max(needed.get(ing, 0), shortfall)
        return needed
