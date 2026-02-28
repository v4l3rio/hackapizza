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
from utils.ingredient_data import dish_prestige_score, dish_avg_prep_time_ms
from config import (
    MENU_MARKUP_BUDGET,
    MENU_MARKUP_STANDARD,
    MENU_MARKUP_PRESTIGE,
    MENU_PRESTIGE_SCORE_HIGH,
    MENU_PRESTIGE_SCORE_LOW,
)

tracer = get_tracer(__name__)


class MenuAgent(Agent):
    """
    Handles menu construction during the 'waiting' phase using LLM reasoning.

    Pricing policy (tiered by dish prestige score):
      - PRESTIGE: score >= MENU_PRESTIGE_SCORE_HIGH → MENU_MARKUP_PRESTIGE × cost
        Rare/high-prestige ingredients → targets Space Sage (unlimited budget).
      - STANDARD: MENU_PRESTIGE_SCORE_LOW <= score < HIGH → MENU_MARKUP_STANDARD × cost
        Typical ingredients → targets Orbital Family / Astrobaron.
      - BUDGET: score < MENU_PRESTIGE_SCORE_LOW → MENU_MARKUP_BUDGET × cost
        Common/cheap ingredients → targets Galactic Explorer (price-sensitive).

    Prestige score = weighted_avg_prestige(ingredients) + rarity_bonus,
    computed from ingredient_frequencies.yaml (see utils/ingredient_data.py).
    """

    name = "menu_agent"
    system_prompt = (
        "You are the menu agent for our restaurant in a sci-fi gastronomic multiverse. "
        "Your job is to craft an appealing menu from the dishes we can currently cook. "
        "Each dish has a pre-computed tier (BUDGET / STANDARD / PRESTIGE) and a suggested_price. "
        "Use the suggested_price — you may adjust by up to 10% if it makes strategic sense. "
        "Write a short description that matches the tier:\n"
        "  - BUDGET: 'quick, affordable, satisfying' — appeals to Galactic Explorer.\n"
        "  - STANDARD: 'balanced quality and value' — appeals to Orbital Family and Astrobaron.\n"
        "  - PRESTIGE: 'rare, exquisite, extraordinary' — appeals to Space Sage (unlimited budget).\n"
        "Call set_menu exactly once with all menu items as a JSON array."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        super().__init__(client=get_llm_client(), max_steps=3)

    # ------------------------------------------------------------------ tools

    @tool(
        name="set_menu",
        description=(
            "Save the restaurant menu. "
            "Accepts a JSON array of menu items, each with keys: "
            "name (str), price (float), description (str). "
            'Example: [{"name": "Pasta", "price": 12.50, "description": "A delicious pasta"}]'
        ),
    )
    async def set_menu(self, items_json: str) -> str:
        """Publish the menu to the game server."""
        try:
            items = json.loads(items_json)
            result = await self._mcp.save_menu(items)
            turn = self._state.turn_id if self._state else "?"
            log("waiting", turn, "tool", f"Menu saved ({len(items)} items): {result}")
            return f"Menu saved successfully with {len(items)} items: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("waiting", turn, "tool", f"set_menu failed: {exc}")
            return f"Error saving menu: {exc}"

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

        with tracer.start_as_current_span("menu_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("waiting", state.turn_id, "agent", "MenuAgent started")
            log(
                "waiting",
                state.turn_id,
                "state",
                f"Balance={state.balance:.2f} | Inventory={state.inventory}",
            )

            cookable = state.cookable_dishes()
            if not cookable:
                log("waiting", state.turn_id, "menu", "No cookable dishes — skipping menu update")
                return

            clearing = memory.clearing_prices if memory.clearing_prices else None

            # Pre-compute pricing profiles — deterministic, no LLM involvement
            dish_profiles: list[dict] = []
            for recipe in cookable:
                name = recipe.get("name", "Unknown")
                cost = state.ingredient_cost(recipe, clearing)
                prestige_score = dish_prestige_score(recipe)
                avg_prep_ms = dish_avg_prep_time_ms(recipe)

                if prestige_score >= MENU_PRESTIGE_SCORE_HIGH:
                    tier = "PRESTIGE"
                    markup = MENU_MARKUP_PRESTIGE
                elif prestige_score < MENU_PRESTIGE_SCORE_LOW:
                    tier = "BUDGET"
                    markup = MENU_MARKUP_BUDGET
                else:
                    tier = "STANDARD"
                    markup = MENU_MARKUP_STANDARD

                suggested_price = round(cost * markup, 2)
                dish_profiles.append({
                    "name": name,
                    "estimated_cost": round(cost, 2),
                    "prestige_score": round(prestige_score, 1),
                    "avg_prep_time_ms": round(avg_prep_ms),
                    "tier": tier,
                    "markup": markup,
                    "suggested_price": suggested_price,
                })

            profile_summary = ", ".join(
                f"{p['name']} [{p['tier']}] {p['suggested_price']}" for p in dish_profiles
            )
            log("waiting", state.turn_id, "menu", f"Dish profiles: {profile_summary}")

            task = (
                f"Cookable dishes (full recipe data): {json.dumps(cookable)}\n\n"
                f"Pricing profiles (pre-computed — use suggested_price as base):\n"
                f"{json.dumps(dish_profiles, indent=2)}\n\n"
                f"Tier rules:\n"
                f"  BUDGET   (prestige_score < {MENU_PRESTIGE_SCORE_LOW}) "
                f"→ {MENU_MARKUP_BUDGET}x markup — common ingredients, cheap & fast, "
                f"appeals to Galactic Explorer.\n"
                f"  STANDARD ({MENU_PRESTIGE_SCORE_LOW}–{MENU_PRESTIGE_SCORE_HIGH}) "
                f"→ {MENU_MARKUP_STANDARD}x markup — balanced quality, "
                f"appeals to Orbital Family and Astrobaron.\n"
                f"  PRESTIGE (prestige_score >= {MENU_PRESTIGE_SCORE_HIGH}) "
                f"→ {MENU_MARKUP_PRESTIGE}x markup — rare/exotic ingredients, "
                f"appeals to Space Sage (unlimited budget, seeks the extraordinary).\n\n"
                "For each dish: use the suggested_price (±10% adjustment allowed), "
                "write a short description that matches the tier and the dish's ingredient profile. "
                "Then call set_menu once with the complete JSON array."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "agent", f"MenuAgent failed: {exc}")
