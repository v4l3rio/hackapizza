"""
RecipeStrategyAgent
===================
Fetches the available recipes from the game server and uses an LLM to decide
which ones to focus on during the competition.

Run standalone:
    python -m agents.recipe_strategy          # with .env loaded
    python agents/recipe_strategy.py          # direct

The agent exposes two tools:
  - fetch_recipes   : calls GET /recipes and returns the raw list as JSON
  - set_strategy    : called by the LLM to record its final prioritisation

After `execute()` the chosen recipes are stored in `self.strategy` so callers
(e.g. BiddingAgent, MenuAgent) can read it directly.
"""

from __future__ import annotations

import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import tool

from infrastructure.http_client import HttpClient
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
import config


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Sei l'agente di strategia per il nostro ristorante in un hackathon gastronomico competitivo
ambientato in un multiverso gastronomico.

Contesto
--------
* Ogni turno di gioco ha fasi: speaking → closed_bid → waiting → serving → stopped.
* Durante 'closed_bid' comptiamo in un'asta cieca per acquisire ingredienti.
  Gli ingredienti scadono alla fine del turno — le scorte inutilizzate vengono sprecate.
* Durante 'serving' arrivano i clienti e guadagniamo denaro servendo loro i piatti.
* Archetipi di clienti e cosa apprezzano:
    - Galactic Explorer : budget basso, poco tempo  → piatti economici e veloci
    - Astrobaron        : budget alto, poco tempo   → piatti premium e veloci
    - Space Sage        : budget illimitato, molto tempo → piatti prestigiosi/rari
    - Orbital Family    : budget e tempo equilibrati → rapporto qualità/prezzo

Obiettivo
---------
Scegli 2-4 ricette su cui concentrarti per tutta la competizione.
Le buone ricette focus condividono caratteristiche come:
  * Pochi ingredienti comuni (più facile vincere all'asta)
  * Breve tempo di preparazione (servire più clienti per turno)
  * Alto potenziale di ricavi rispetto al costo degli ingredienti
  * Ampio appeal per più archetipi di clienti

Istruzioni
----------
1. Chiama fetch_recipes per recuperare tutte le ricette disponibili.
2. Analizza ogni ricetta: numero di ingredienti, tempo di preparazione, margine stimato.
3. Chiama set_strategy UNA VOLTA con la tua lista finale prioritizzata e una spiegazione chiara.
"""


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class RecipeStrategyAgent(Agent):
    """
    Decides which recipes to target for the whole competition.

    Usage:
        agent = RecipeStrategyAgent(http_client)
        await agent.execute()
        print(agent.strategy)    # list of chosen recipe dicts
    """

    name = "recipe_strategy_agent"
    system_prompt = _SYSTEM_PROMPT

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self.strategy: list[dict[str, Any]] = []
        self.reasoning: str = ""
        super().__init__(client=get_llm_client(), max_steps=4)

    # ------------------------------------------------------------------ tools

    @tool(
        name="fetch_recipes",
        description=(
            "Fetch all available recipes from the game server. "
            "Returns a JSON array of recipe objects. Each recipe has: "
            "name (str), ingredients (dict of ingredient->quantity), "
            "preparation_time (int, seconds or turns), and optionally prestige/value."
        ),
    )
    async def fetch_recipes(self) -> str:
        """Call GET /recipes and return the result as a JSON string."""
        try:
            recipes = await self._http.get_recipes()
            log("strategy", 0, "tool", f"Fetched {len(recipes)} recipes")
            return json.dumps(recipes, ensure_ascii=False, indent=2)
        except Exception as exc:
            log_error("strategy", 0, "tool", f"fetch_recipes failed: {exc}")
            return json.dumps({"error": str(exc)})

    @tool(
        name="set_strategy",
        description=(
            "Record the final recipe strategy after analysis. "
            "Parameters: "
            "'recipes' (list of recipe name strings to focus on, in priority order) "
            "and 'reasoning' (short explanation of the choices). "
            'Example: recipes=["Pasta Nebulare", "Zuppa Cosmica"], '
            'reasoning="Few ingredients, fast prep, wide client appeal."'
        ),
    )
    async def set_strategy(self, recipes: list, reasoning: str = "") -> str:
        """Persist the LLM's recipe prioritisation decision."""
        try:
            chosen_names: list[str] = [str(n) for n in recipes]
            self.reasoning = reasoning
            self.strategy = [{"name": n} for n in chosen_names]

            log(
                "strategy",
                0,
                "decision",
                f"Strategy set: {chosen_names} | Reasoning: {self.reasoning}",
            )
            return (
                f"Strategy recorded. Focused on {len(chosen_names)} recipe(s): "
                f"{', '.join(chosen_names)}."
            )
        except Exception as exc:
            log_error("strategy", 0, "tool", f"set_strategy failed: {exc}")
            return f"Error recording strategy: {exc}"

    # ------------------------------------------------------------------ entry point

    async def execute(self, clearing_prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """
        Run the full strategy-selection loop.

        Returns the chosen recipe list (also stored in self.strategy).
        """
        log("strategy", 0, "agent", "RecipeStrategyAgent started")

        prices_ctx = (
            f"Prezzi di aggiudicazione noti dai turni precedenti: {json.dumps(clearing_prices)}\n"
            "Usali per stimare il costo d'asta per ricetta (somma di prezzo * quantità per ogni ingrediente).\n"
            if clearing_prices
            else "Nessuno storico di prezzi di aggiudicazione (primo turno) — assume ~50 per unità di ingrediente.\n"
        )

        task = (
            "Analizza le nostre opzioni di ricette e decidi su quali 2-4 ricette "
            "concentrarci per tutta la competizione.\n\n"
            + prices_ctx
            + "Passo 1: chiama fetch_recipes per vedere cosa è disponibile.\n"
            "Passo 2: per ogni ricetta stima il costo d'asta (clearing_price * quantità per ingrediente), "
            "il tempo di preparazione e l'adattabilità a più archetipi di clienti.\n"
            "Passo 3: chiama set_strategy con la tua lista prioritizzata e la motivazione."
        )

        try:
            await self.a_run(task, tool_choice="required_first")
        except Exception as exc:
            log_error("strategy", 0, "agent", f"RecipeStrategyAgent failed: {exc}")

        log("strategy", 0, "agent", f"Done. Strategy: {self.strategy}")
        return self.strategy


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def _main() -> None:
    """
    Run the recipe strategy agent as a one-shot script.
    Loads credentials from .env / environment variables.
    """
    http = HttpClient(
        base_url=config.BASE_URL,
        team_id=config.TEAM_ID,
        api_key=config.TEAM_API_KEY,
    )

    agent = RecipeStrategyAgent(http)
    strategy = await agent.execute()

    print("\n" + "=" * 60)
    print("FINAL STRATEGY")
    print("=" * 60)
    for item in strategy:
        print(f"  - {item['name']}")
    if agent.reasoning:
        print(f"\nReasoning: {agent.reasoning}")
    print("=" * 60)


if __name__ == "__main__":
    import asyncio
    import os
    import sys
    # Fix sys.path when run directly from the agents/ subdirectory
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    asyncio.run(_main())
