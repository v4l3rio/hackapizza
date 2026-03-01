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
        "Il tuo obiettivo è acquisire gli ingredienti necessari spendendo il MINIMO possibile.\n\n"
        "STRATEGIA DI PRICING:\n"
        "- Ti verrà fornito lo storico dei bid di TUTTI i ristoranti per ogni ingrediente.\n"
        "- Il 'clearing price' è il prezzo minimo che ha ottenuto status COMPLETED in un turno.\n"
        "- Chi offre meno del clearing price viene CANCELLED (non ottiene l'ingrediente).\n"
        "- Il tuo obiettivo è offrire appena sopra il clearing price atteso per vincere spendendo poco.\n"
        "- Analizza i trend: se il clearing price sta salendo, offri un po' di più; se è stabile o in calo, puoi offrire meno.\n"
        "- Per ingredienti con clearing price costantemente basso (1-2), non serve offrire di più.\n"
        "- Per ingredienti con alta competizione o clearing price in salita, offri di più per assicurarteli.\n"
        "- La spesa totale (somma di priceForEach × quantity per ogni bid) NON deve superare il budget.\n"
        "- Chiama closed_bid esattamente una volta con l'array completo di offerte."
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

            # Build historical bid context for LLM autonomous pricing
            loop = asyncio.get_event_loop()
            bid_context = await loop.run_in_executor(
                None,
                lambda: DASHBOARD.get_bid_context_for_llm(
                    target_ingredients=list(needed.keys()),
                    limit=10,
                ),
            )

            log("closed_bid", state.turn_id, "debug-agent", f"Bid context length: {len(bid_context)} chars")

            task = (
                f"Saldo attuale: {state.balance:.2f}\n"
                f"Limite di budget ({int(MAX_BID_BALANCE_FRACTION * 100)}% del saldo): {budget:.2f}\n"
                f"Ingredienti da offrire (quantità per ciascuno: {DEFAULT_BID_QUANTITY}):\n"
                f"{json.dumps(list(needed.keys()))}\n\n"
                f"Prezzo di fallback se non hai dati storici: {DEFAULT_BID_FLAT}\n\n"
                f"{bid_context}\n\n"
                f"Sulla base dello storico sopra, decidi il priceForEach ottimale per OGNI ingrediente.\n"
                f"Obiettivo: vincere i bid spendendo il meno possibile. La spesa totale "
                f"(somma di priceForEach × {DEFAULT_BID_QUANTITY} per ogni ingrediente) "
                f"NON deve superare {budget:.2f}.\n"
                f"Chiama closed_bid una volta con l'array completo di offerte."
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
