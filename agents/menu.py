from __future__ import annotations

import json
import logging

from datapizza.agents import Agent
from datapizza.tools import Tool

from config import (
    MENU_MARKUP_BUDGET,
    MENU_MARKUP_STANDARD,
    MENU_MARKUP_PRESTIGE,
    MENU_PRESTIGE_SCORE_HIGH,
    MENU_PRESTIGE_SCORE_LOW,
    DEFAULT_PRICE_SELL,
    DASHBOARD
)
from infrastructure.llm_factory import get_llm_client
from state.game_state import GameState
from state.game_state import ingredient_cost
from state.memory import StrategyMemory
from utils.ingredient_data import dish_prestige_score, dish_avg_prep_time_ms
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)
_log = logging.getLogger("menu_agent")


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
            _log.info("MenuAgent.execute: turn_id=%s balance=%.2f inventory=%s",
                      state.turn_id, state.balance, state.inventory)

            cookable = state.cookable_dishes()
            _log.info("Cookable dishes count=%d names=%s",
                      len(cookable), [r.get("name") for r in cookable])
            if not cookable:
                log("waiting", state.turn_id, "menu", "No cookable dishes — skipping menu update")
                _log.warning("No cookable dishes — MenuAgent will not set a menu this turn")
                return

            clearing = memory.clearing_prices if memory.clearing_prices else None
            _log.debug("clearing_prices=%s", clearing)

            # Reputation multiplier: rep=100 → 1.5, rep=50 → 0.75, rep=0 → 0.5
            # Formula: 0.5 + (rep/100)^2
            rep_multiplier = round(0.5 + (state.reputation / 100.0) ** 2, 3)
            log("waiting", state.turn_id, "menu", f"Reputation={state.reputation:.1f} → price multiplier={rep_multiplier}")

            # Pre-compute pricing profiles — deterministic, no LLM involvement
            dish_profiles: list[dict] = []

            # Fetch dish price history once for all cookable dishes
            history_prices: dict[str, float | None] = {}
            try:
                all_dish_history = DASHBOARD.history_dishes(limit=1)

                for recipe in cookable:
                    dish_name = recipe.get("name", "Unknown")
                    try:
                        obs = all_dish_history.get(dish_name, [])
                        if obs:
                            prices = [o["price"] for o in obs]
                            history_prices[dish_name] = round(sum(prices) / len(prices), 2)
                        else:
                            history_prices[dish_name] = None
                        _log.debug("History for '%s': avg_price=%s", dish_name, history_prices[dish_name])
                    except Exception as e:
                        _log.warning("Could not fetch history for '%s': %s", dish_name, e)
                        log("waiting", state.turn_id, "menu",
                            f"Could not fetch history for {dish_name}: {e}")
                        history_prices[dish_name] = None
            except Exception as exc:
                _log.exception("Failed to initialize HistoryClient: %s", exc)
                log_error("waiting", state.turn_id, "menu",
                         f"Failed to initialize HistoryClient: {exc}")
                for recipe in cookable:
                    dish_name = recipe.get("name", "Unknown")
                    history_prices[dish_name] = None
            _log.info("history_prices=%s", history_prices)

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

                if history_prices.get(name) is not None:
                    suggested_price = max(1.0, round(history_prices[name] - 5, 2))
                    _log.debug("'%s': price from history=%.2f → suggested=%.2f",
                               name, history_prices[name], suggested_price)
                else:
                    suggested_price = float(DEFAULT_PRICE_SELL)
                    _log.debug("'%s': no history → fallback suggested_price=%.2f", name, suggested_price)

                _log.info(
                    "Dish profile: name='%s' tier=%s prestige=%.1f cost=%.2f markup=%.2f "
                    "avg_prep_ms=%d suggested_price=%.2f",
                    name, tier, prestige_score, cost, markup, avg_prep_ms, suggested_price,
                )
                dish_profiles.append({
                    "name": name,
                    "estimated_cost": round(cost, 2),
                    "prestige_score": round(prestige_score, 1),
                    "avg_prep_time_ms": round(avg_prep_ms),
                    "tier": tier,
                    "markup": markup,
                    "suggested_price": suggested_price * rep_multiplier,
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
                _log.debug("Calling a_run with %d dish profiles", len(dish_profiles))
                result = await self.a_run(task, tool_choice="required_first")
                _log.debug("a_run result: %s", result)
                if result:
                    tools_called = [tc.name for tc in result.tools_used]
                    _log.info("LLM tools called: %s", tools_called)
                    for tc in result.tools_used:
                        _log.debug("Tool call: name=%s args=%s", tc.name, tc.arguments)
                else:
                    _log.warning("a_run returned no result — save_menu may not have been called")
            except Exception as exc:
                _log.exception("MenuAgent.a_run failed: %s", exc)
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "agent", f"MenuAgent failed: {exc}")
