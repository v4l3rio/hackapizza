from __future__ import annotations

import asyncio
import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from infrastructure.mcp_client import MCPClient
from infrastructure.sse_listener import SSEListener
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class ServingAgent(Agent):
    """
    Handles the 'serving' phase using LLM-driven dish matching.

    Registers SSE handlers for:
      - client_spawned: LLM picks the best matching dish (respecting intolerances) and prepares it.
      - preparation_complete: directly serves the ready dish to the waiting client.
    """

    name = "serving_agent"
    system_prompt = (
        "You are the serving agent for our restaurant. "
        "When a client arrives, identify the best dish from our menu that matches their order "
        "and avoids any ingredients they are intolerant to. "
        "Use the client's archetype to guide your choice:\n"
        "  - Galactic Explorer: low budget, short time → pick the CHEAPEST and FASTEST dish.\n"
        "  - Astrobaron: high budget, short time → pick the most PREMIUM and FASTEST dish.\n"
        "  - Space Sage: unlimited budget, long time → pick the most PRESTIGIOUS or RARE dish.\n"
        "  - Orbital Family: balanced → pick the best QUALITY-TO-PRICE ratio dish.\n"
        "If no archetype is explicit, infer it from the order text and client name. "
        "Call prepare_dish with the best matching dish. "
        "If no suitable dish is available, call prepare_dish with the closest available option. "
        "Always call prepare_dish exactly once — do not call open_restaurant or any other tool."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        self._http: HttpClient | None = None
        self._pending_orders: dict[str, list[str]] = {}  # dish_name -> [client_id, ...]
        super().__init__(client=get_llm_client(), max_steps=1)

    # ------------------------------------------------------------------ tools

    @tool(
        name="prepare_dish",
        description=(
            "Start preparing a dish in the kitchen for a specific client. "
            "dish_name must exactly match one of the items on our current menu. "
            "client_id is the meal ID provided in the task (a server-issued identifier). "
            "This triggers a 'preparation_complete' event when the dish is ready."
        ),
    )
    async def prepare_dish(self, dish_name: str, client_id: str) -> str:
        """Begin kitchen preparation, register the client, and update local inventory."""
        turn = self._state.turn_id if self._state else "?"
        try:
            result = await self._mcp.prepare_dish(dish_name)

            # Register the client->dish mapping atomically so preparation_complete can serve it.
            self._pending_orders.setdefault(dish_name, []).append(client_id)

            # Update local inventory immediately to prevent double-booking when clients
            # arrive concurrently and state hasn't been refreshed yet.
            if self._state:
                for recipe in self._state.recipes:
                    if recipe.get("name") == dish_name:
                        for ing, qty in recipe.get("ingredients", {}).items():
                            current = self._state.inventory.get(ing, 0)
                            self._state.inventory[ing] = max(0, current - qty)
                        break

            log("serving", turn, "tool", f"Preparing '{dish_name}' for {client_id}: {result}")
            return f"Preparation started for '{dish_name}' (client: {client_id}): {result}"
        except Exception as exc:
            log_error("serving", turn, "tool", f"prepare_dish failed: {exc}")
            return f"Error preparing '{dish_name}': {exc}"

    # ------------------------------------------------------------------ phase entry

    def register(
        self,
        sse: SSEListener,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
        http: HttpClient,
    ) -> None:
        """Register SSE handlers. Call once at startup."""
        self._state = state
        self._strat = memory
        self._mcp = mcp
        self._http = http
        sse.on("client_spawned", self._on_client_spawned)
        sse.on("preparation_complete", self._on_preparation_complete)

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
        http: HttpClient,
    ) -> None:
        """Called when the serving phase starts — restaurant already opened in waiting phase."""
        self._state = state
        self._strat = memory
        self._mcp = mcp
        self._http = http
        self._pending_orders.clear()

        with tracer.start_as_current_span("serving_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)
            log("serving", state.turn_id, "agent", "ServingAgent started — restaurant already open")
            log(
                "serving",
                state.turn_id,
                "state",
                f"Balance={state.balance:.2f} | Inventory={state.inventory}",
            )

    # ------------------------------------------------------------------ helpers

    async def _lookup_meal_id(self, client_name: str) -> str | None:
        """
        Fetch /meals for the current turn and return the meal ID for this client.

        The server's serve_dish tool requires the internal meal ID (not the display name).
        We retry briefly in case the meal record hasn't been written yet when the SSE fires.
        Returns None if not found after retries (caller falls back to clientName).
        """
        if self._http is None or self._state is None:
            return None
        turn_id = self._state.turn_id
        for attempt in range(4):
            try:
                meals = await self._http.get_meals(turn_id)
                # Log raw response on first attempt to aid debugging
                if attempt == 0:
                    log("serving", turn_id, "meal_lookup",
                        f"GET /meals returned {len(meals)} entries: {meals[:3]}")
                for meal in meals:
                    # Try every plausible name field
                    name = (
                        meal.get("clientName")
                        or meal.get("client_name")
                        or meal.get("name")
                        or meal.get("customer")
                    )
                    if name == client_name and not meal.get("executed", False):
                        # The field serve_dish needs is "client_id" — try that first,
                        # then fall back to generic id fields.
                        resolved = (
                            meal.get("client_id")
                            or meal.get("clientId")
                            or meal.get("id")
                            or meal.get("mealId")
                            or meal.get("meal_id")
                            or meal.get("_id")
                        )
                        if resolved is not None:
                            log(
                                "serving", turn_id, "meal_lookup",
                                f"Resolved '{client_name}' → client_id={resolved}",
                            )
                            return str(resolved)
                        # ID field not found — log all keys so we can fix the alias
                        log_error(
                            "serving", turn_id, "meal_lookup",
                            f"Meal matched but no id field found. Keys: {list(meal.keys())} Values: {meal}",
                        )
            except Exception as exc:
                log_error("serving", turn_id, "meal_lookup", f"Attempt {attempt+1} failed: {exc}")
            if attempt < 3:
                await asyncio.sleep(0.8)
        log_error(
            "serving", turn_id, "meal_lookup",
            f"Could not resolve client_id for '{client_name}' after 4 attempts — falling back to clientName",
        )
        return None

    # ------------------------------------------------------------------ SSE handlers

    async def _on_client_spawned(self, data: dict[str, Any]) -> None:
        """Fire-and-forget: each client is handled concurrently so the SSE loop never blocks."""
        if self._state is None or self._mcp is None:
            return
        asyncio.create_task(self._handle_client_spawned(data))

    async def _handle_client_spawned(self, data: dict[str, Any]) -> None:
        """Full LLM handling for a single client_spawned event (runs as a background task)."""
        if self._state is None or self._mcp is None:
            return

        client_name = str(data.get("clientName") or data.get("client_id") or data.get("id", "unknown"))
        order_text = str(data.get("orderText") or data.get("order") or data.get("text", ""))
        intolerances = data.get("intolerances") or data.get("allergies") or []

        log("serving", self._state.turn_id, "client", f"Client {client_name} wants: '{order_text}'")

        # Resolve the actual meal ID that serve_dish needs — fall back to clientName if not found.
        meal_id = await self._lookup_meal_id(client_name)
        serve_client_id = meal_id if meal_id else client_name

        with tracer.start_as_current_span("serving_agent.client_spawned") as span:
            span.set_attribute("client_id", serve_client_id)
            span.set_attribute("turn_id", self._state.turn_id)

            # Filter recipes to only those on the menu AND cookable (have all ingredients in stock)
            menu_names = {item.get("name") for item in self._state.menu_items}
            cookable = self._state.cookable_dishes()
            menu_recipes = [r for r in cookable if r.get("name") in menu_names]

            if not menu_recipes:
                log("serving", self._state.turn_id, "client", f"No cookable menu items — skipping {client_name}")
                return

            # Let LLM pick best matching dish and call prepare_dish.
            # NOTE: prepare_dish tool takes both dish_name AND client_id (meal ID) so it can
            # register the pending order atomically — no reliance on result.tools_used.
            task = (
                f"Client '{client_name}' has arrived (meal_id={serve_client_id}).\n"
                f"Their order: \"{order_text}\"\n"
                f"Their dietary intolerances/allergies: {json.dumps(intolerances)}\n\n"
                "Identify the client's archetype from their name and order text:\n"
                "  - Galactic Explorer → cheapest + fewest ingredients (fast prep)\n"
                "  - Astrobaron → highest price + fewest ingredients (fast prep)\n"
                "  - Space Sage → rarest/most prestigious ingredients\n"
                "  - Orbital Family → best price-to-quality ratio\n\n"
                f"Available menu items we can cook right now (name, price, description):\n{json.dumps(self._state.menu_items)}\n"
                f"Cookable recipes with ingredients (for intolerance check):\n{json.dumps(menu_recipes)}\n\n"
                "Pick the best dish using archetype criteria. "
                "IMPORTANT: exclude any dish containing an ingredient the client is intolerant to. "
                f"Call prepare_dish with dish_name=<exact menu name> and client_id='{serve_client_id}'."
            )

            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("serving", self._state.turn_id, "client", f"_handle_client_spawned failed: {exc}")

    async def _on_preparation_complete(self, data: dict[str, Any]) -> None:
        if self._state is None or self._mcp is None:
            return

        dish_name = data.get("dish") or data.get("name", "")
        log("serving", self._state.turn_id, "kitchen", f"Preparation complete: '{dish_name}'")

        # Find the first client waiting for this dish
        clients = self._pending_orders.get(dish_name, [])
        if not clients:
            log(
                "serving",
                self._state.turn_id,
                "kitchen",
                f"No pending client for '{dish_name}' — ignoring",
            )
            return

        target_client_id = clients.pop(0)
        if not clients:
            del self._pending_orders[dish_name]

        # Serve directly — this is deterministic, no LLM needed
        try:
            result = await self._mcp.serve_dish(dish_name, target_client_id)
            log(
                "serving",
                self._state.turn_id,
                "serve",
                f"Served '{dish_name}' to client {target_client_id}: {result}",
            )
        except Exception as exc:
            log_error(
                "serving",
                self._state.turn_id,
                "serve",
                f"serve_dish failed for client {target_client_id}: {exc}",
            )
