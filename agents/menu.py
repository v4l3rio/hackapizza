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
from state.game_state import ingredient_cost
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
        "Sei l'agente del menu per il nostro ristorante in un multiverso gastronomico sci-fi. "
        "Il tuo compito è creare un menu attraente dai piatti che possiamo attualmente cucinare. "
        "Ogni piatto ha un livello pre-calcolato (BUDGET / STANDARD / PRESTIGE) e un prezzo suggerito. "
        "Usa il prezzo suggerito — puoi aggiustarlo fino al 10% se ha senso strategico. "
        "Scrivi una breve descrizione che si adatti al livello:\n"
        "  - BUDGET: 'veloce, conveniente, soddisfacente' — ideale per il Galactic Explorer.\n"
        "  - STANDARD: 'qualità ed equilibrio' — ideale per Orbital Family e Astrobaron.\n"
        "  - PRESTIGE: 'raro, squisito, straordinario' — ideale per lo Space Sage (budget illimitato).\n"
        "Chiama set_menu esattamente una volta con tutti i piatti del menu come array JSON."
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
            for item in items:
                item["price"] = 50.0
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
                cost = ingredient_cost(recipe, clearing)
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
                f"Piatti cucinabili (dati ricetta completi): {json.dumps(cookable)}\n\n"
                f"Profili di prezzo (pre-calcolati — usa il suggested_price come base):\n"
                f"{json.dumps(dish_profiles, indent=2)}\n\n"
                f"Regole per livello:\n"
                f"  BUDGET   (prestige_score < {MENU_PRESTIGE_SCORE_LOW}) "
                f"→ markup {MENU_MARKUP_BUDGET}x — ingredienti comuni, economici e veloci, "
                f"ideale per Galactic Explorer.\n"
                f"  STANDARD ({MENU_PRESTIGE_SCORE_LOW}–{MENU_PRESTIGE_SCORE_HIGH}) "
                f"→ markup {MENU_MARKUP_STANDARD}x — qualità equilibrata, "
                f"ideale per Orbital Family e Astrobaron.\n"
                f"  PRESTIGE (prestige_score >= {MENU_PRESTIGE_SCORE_HIGH}) "
                f"→ markup {MENU_MARKUP_PRESTIGE}x — ingredienti rari/esotici, "
                f"ideale per lo Space Sage (budget illimitato, cerca l'eccezionale).\n\n"
                "Per ogni piatto: usa il suggested_price (±10% di aggiustamento consentito), "
                "scrivi una breve descrizione che si adatti al livello e al profilo degli ingredienti del piatto. "
                "Poi chiama set_menu una volta con l'array JSON completo."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "agent", f"MenuAgent failed: {exc}")
