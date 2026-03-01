from __future__ import annotations

import json

from datapizza.agents import Agent
from datapizza.tools import Tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.llm_factory import get_llm_client
from infrastructure.history_client import HistoryClient
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
    DEFAULT_PRICE_SELL,
    WEB_APP_URL
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
        "Chiama save_menu esattamente una volta con tutti i piatti del menu."
    )

    def __init__(self, mcp_tools: list[Tool]) -> None:
        self._state: GameState | None = None
        super().__init__(client=get_llm_client(), tools=mcp_tools, max_steps=3)

    # ------------------------------------------------------------------ phase entry

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
    ) -> None:
        self._state = state

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

            # Fetch dish price history once for all cookable dishes
            history_prices: dict[str, float | None] = {}
            try:
                with HistoryClient(WEB_APP_URL) as c:
                    c.set_turn(state.turn_id)
                    for recipe in cookable:
                        dish_name = recipe.get("name", "Unknown")
                        try:
                            dh = c.dish_history(dish_name, limit=1)
                            history_prices[dish_name] = dh.summary.avg_price
                        except Exception as e:
                            log("waiting", state.turn_id, "menu",
                                f"Could not fetch history for {dish_name}: {e}")
                            history_prices[dish_name] = None
            except Exception as exc:
                log_error("waiting", state.turn_id, "menu",
                         f"Failed to initialize HistoryClient: {exc}")

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

                # Use dish history average price minus 5 if available, otherwise use calculated markup
                if history_prices.get(name) is not None:
                    suggested_price = max(0.0, round(history_prices[name] - 5, 2))
                else:
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
                f"Profili di prezzo (pre-calcolati — usa il suggested_price come base):\n"
                f"{json.dumps(dish_profiles, indent=2)}\n\n"
                f"Piatti cucinabili (dati ricetta completi): {json.dumps(cookable)}\n\n"
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
                "Per ogni piatto usa il suggested_price (±10% di aggiustamento consentito). "
                "Poi chiama save_menu una volta con l'array completo."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "agent", f"MenuAgent failed: {exc}")
