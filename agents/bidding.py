from __future__ import annotations

import asyncio
import json

from datapizza.agents import Agent
from datapizza.tools import Tool

from config import DEFAULT_BID_FLAT, DEFAULT_BID_QUANTITY, MAX_BID_BALANCE_FRACTION, MAX_RECIPES, DASHBOARD
from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.llm_factory import get_llm_client
from utils.ingredient_data import get_ingredient_data
from utils.logger import log, log_error
from utils.tracing import get_tracer
from infrastructure.http_client import HttpClient

tracer = get_tracer(__name__)


class BiddingAgent(Agent):
    """
    Handles the 'closed_bid' phase using LLM reasoning.

    Bids DEFAULT_BID_QUANTITY units of every known ingredient, capped at
    MAX_BID_BALANCE_FRACTION of the current balance.
    """

    name = "bidding_agent"
    system_prompt = (
        "Sei l'agente di offerta del nostro ristorante in un gioco di cucina competitivo. "
        "Il tuo obiettivo è acquisire gli ingredienti necessari per cucinare quanti più piatti possibile. "
        "Analizza l'inventario attuale, gli ingredienti necessari e i prezzi di aggiudicazione. "
        "Rispetta il limite di budget e invia tutte le offerte in una singola chiamata a closed_bid. "
        "Chiama closed_bid esattamente una volta — non saltarla se ci sono ingredienti necessari."
    )

    def __init__(self, mcp_tools: list[Tool]) -> None:
        self._state: GameState | None = None
        super().__init__(client=get_llm_client(), tools=mcp_tools, max_steps=1)
        self.max_recipes = MAX_RECIPES

    async def execute(self, state: GameState, memory: StrategyMemory, http: HttpClient) -> None:
        self._state = state

        with tracer.start_as_current_span("bidding_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)
            span.set_attribute("balance", state.balance)

            log("closed_bid", state.turn_id, "agent", "BiddingAgent started")
            log("closed_bid", state.turn_id, "state", f"Balance={state.balance:.2f} | Inventory={state.inventory}")

            budget = state.balance * MAX_BID_BALANCE_FRACTION
            needed = await self._compute_needed(state, http)

            if not needed:
                log("closed_bid", state.turn_id, "agent", "No ingredients needed — skipping bid")
                return

            task = (
                f"Saldo attuale: {state.balance:.2f}\n"
                f"Limite di budget ({int(MAX_BID_BALANCE_FRACTION * 100)}% del saldo): {budget:.2f}\n"
                f"Ingredienti da offrire (quantità per ciascuno: {DEFAULT_BID_QUANTITY}): {json.dumps(list(needed))}\n"
                f"Ultimi prezzi di aggiudicazione noti: {json.dumps(memory.clearing_prices)}\n"
                f"Prezzo fisso di default per bid (non moltiplicare questo valore per la quantità, inseriscilo come parametro come fornito): {DEFAULT_BID_FLAT}.\n"
                f"La spesa totale NON deve superare {budget:.2f}.\n\n"
                "Chiama closed_bid una volta con l'array completo di offerte."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("closed_bid", state.turn_id, "agent", f"BiddingAgent failed: {exc}")

    async def _compute_needed(self, state, http: HttpClient) -> dict[str, int]:
        """Returns DEFAULT_BID_QUANTITY for every known ingredient."""

        if MAX_RECIPES:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None, DASHBOARD.get_optimal_recipe_set, self.max_recipes
            )
            ingredients = list(data['shared_ingredients'].keys())
            log("closed_bid", state.turn_id, "debug-agent", f"Obtained {len(ingredients)} Ingredients from {MAX_RECIPES} recipes")
        else:
            ingredients = get_ingredient_data()

        return {ing: DEFAULT_BID_QUANTITY for ing in ingredients} # get_ingredient_data()
