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

import asyncio
import json
import os
import sys
from typing import Any

# When run directly (`python agents/recipe_strategy.py`), Python adds the
# `agents/` subdirectory to sys.path instead of the project root, so sibling
# packages like `infrastructure`, `state`, and `config` are not found.
# Inserting the project root fixes this without affecting normal imports.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
You are the strategy agent for our restaurant in a competitive cooking hackathon
set in a gastronomic multiverse.

Context
-------
* Each game turn has phases: speaking → closed_bid → waiting → serving → stopped.
* During 'closed_bid' we compete in a blind auction to acquire ingredients.
  Ingredients expire at the end of the turn — unused stock is wasted.
* During 'serving' clients arrive and we earn money by serving them dishes.
* Client archetypes and what they value:
    - Galactic Explorer : low budget, little time  → cheap & fast dishes
    - Astrobaron        : big budget, little time  → premium & fast dishes
    - Space Sage        : unlimited budget, ample time → prestigious/rare dishes
    - Orbital Family    : balanced budget & time   → quality/price ratio

Goal
----
Pick 2-4 recipes to focus on across the entire competition.
Good focus recipes share traits like:
  * Few, commonly-available ingredients (easier to win at auction)
  * Short preparation time (serve more clients per turn)
  * High revenue potential relative to ingredient cost
  * Broad appeal to multiple client archetypes

Instructions
------------
1. Call fetch_recipes to retrieve all available recipes.
2. Analyse each recipe: ingredient count, preparation time, estimated margin.
3. Call set_strategy ONCE with your final prioritised list and clear reasoning.
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
            "Accepts a JSON object with keys: "
            "'recipes' (list of recipe names to focus on, in priority order) "
            "and 'reasoning' (short explanation of the choices). "
            'Example: {"recipes": ["Pasta Nebulare", "Zuppa Cosmica"], '
            '"reasoning": "Few ingredients, fast prep, wide client appeal."}'
        ),
    )
    async def set_strategy(self, strategy_json: str) -> str:
        """Persist the LLM's recipe prioritisation decision."""
        try:
            data = json.loads(strategy_json)
            chosen_names: list[str] = data.get("recipes", [])
            self.reasoning = data.get("reasoning", "")

            # Cross-reference names with full recipe objects (if already fetched)
            # We store names for now; callers can enrich later.
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

    async def execute(self) -> list[dict[str, Any]]:
        """
        Run the full strategy-selection loop.

        Returns the chosen recipe list (also stored in self.strategy).
        """
        log("strategy", 0, "agent", "RecipeStrategyAgent started")

        task = (
            "Please analyse our recipe options and decide which 2-4 recipes "
            "we should focus on throughout the competition.\n\n"
            "Step 1: call fetch_recipes to see what is available.\n"
            "Step 2: reason about ingredient cost, prep time, and client fit.\n"
            "Step 3: call set_strategy with your prioritised list and reasoning."
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
    asyncio.run(_main())
