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
from config import MENU_MARKUP

tracer = get_tracer(__name__)


class MenuAgent(Agent):
    """
    Handles menu construction during the 'waiting' phase using LLM reasoning.

    Strategy:
      - Identifies cookable dishes (all ingredients available)
      - Sets price = ingredient cost * MENU_MARKUP
      - Writes appealing dish descriptions
      - Calls save_menu
    """

    name = "menu_agent"
    system_prompt = (
        "You are the menu agent for our restaurant. "
        "Your job is to craft an appealing menu from the dishes we can currently cook. "
        "Set prices at roughly the specified markup over ingredient cost. "
        "Write short, enticing descriptions for each dish. "
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

            # Pre-compute costs to guide LLM pricing
            costs = {
                recipe.get("name", "Unknown"): state.ingredient_cost(recipe)
                for recipe in cookable
            }

            task = (
                f"Cookable dishes (we have all ingredients): {json.dumps(cookable)}\n"
                f"Estimated ingredient costs per dish: {json.dumps(costs)}\n"
                f"Target price markup: {MENU_MARKUP}x ingredient cost.\n\n"
                "Create the menu: for each cookable dish set a price (~markup * cost) "
                "and write a short, enticing description. "
                "Then call set_menu once with the complete JSON array."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("waiting", state.turn_id, "agent", f"MenuAgent failed: {exc}")
