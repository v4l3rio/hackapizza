from __future__ import annotations

import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
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
        "Then prepare the dish using the prepare_dish tool. "
        "When asked to open the restaurant, call open_restaurant. "
        "Act decisively — always call a tool to take action."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        self._pending_orders: dict[str, dict[str, Any]] = {}  # client_id -> {dish, client_id}
        super().__init__(client=get_llm_client(), max_steps=3)

    # ------------------------------------------------------------------ tools

    @tool(
        name="open_restaurant",
        description="Open the restaurant so clients can be served.",
    )
    async def open_restaurant(self) -> str:
        """Mark the restaurant as open for business."""
        try:
            result = await self._mcp.update_restaurant_is_open(True)
            turn = self._state.turn_id if self._state else "?"
            log("serving", turn, "tool", f"Restaurant opened: {result}")
            return f"Restaurant is now open: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("serving", turn, "tool", f"open_restaurant failed: {exc}")
            return f"Error opening restaurant: {exc}"

    @tool(
        name="prepare_dish",
        description=(
            "Start preparing a dish in the kitchen. "
            "The dish name must exactly match one of the items on our current menu. "
            "This triggers a 'preparation_complete' event when the dish is ready."
        ),
    )
    async def prepare_dish(self, dish_name: str) -> str:
        """Begin kitchen preparation for a named dish."""
        try:
            result = await self._mcp.prepare_dish(dish_name)
            turn = self._state.turn_id if self._state else "?"
            log("serving", turn, "tool", f"Preparing '{dish_name}': {result}")
            return f"Preparation started for '{dish_name}': {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("serving", turn, "tool", f"prepare_dish failed: {exc}")
            return f"Error preparing '{dish_name}': {exc}"

    # ------------------------------------------------------------------ phase entry

    def register(
        self,
        sse: SSEListener,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        """Register SSE handlers. Call once at startup."""
        self._state = state
        self._strat = memory
        self._mcp = mcp
        sse.on("client_spawned", self._on_client_spawned)
        sse.on("preparation_complete", self._on_preparation_complete)

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        """Called when the serving phase starts — opens the restaurant."""
        self._state = state
        self._strat = memory
        self._mcp = mcp

        with tracer.start_as_current_span("serving_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("serving", state.turn_id, "agent", "ServingAgent started")
            log(
                "serving",
                state.turn_id,
                "state",
                f"Balance={state.balance:.2f} | Inventory={state.inventory}",
            )

            task = (
                "The serving phase has begun. Open the restaurant so clients can arrive. "
                "Call open_restaurant now."
            )
            try:
                await self.a_run(task, tool_choice="required_first")
            except Exception as exc:
                span.record_exception(exc)
                log_error("serving", state.turn_id, "agent", f"ServingAgent open failed: {exc}")

    # ------------------------------------------------------------------ SSE handlers

    async def _on_client_spawned(self, data: dict[str, Any]) -> None:
        if self._state is None or self._mcp is None:
            return

        client_id = str(data.get("clientName") or data.get("client_id") or data.get("id", "unknown"))
        order_text = str(data.get("orderText") or data.get("order") or data.get("text", ""))
        intolerances = data.get("intolerances") or data.get("allergies") or []

        log("serving", self._state.turn_id, "client", f"Client {client_id} wants: '{order_text}'")

        with tracer.start_as_current_span("serving_agent.client_spawned") as span:
            span.set_attribute("client_id", client_id)
            span.set_attribute("turn_id", self._state.turn_id)

            # Let LLM pick best matching dish and call prepare_dish
            task = (
                f"Client '{client_id}' has arrived.\n"
                f"Their order: \"{order_text}\"\n"
                f"Their dietary intolerances/allergies: {json.dumps(intolerances)}\n\n"
                f"Current menu: {json.dumps(self._state.menu_items)}\n"
                f"Recipes with ingredients: {json.dumps(self._state.recipes)}\n\n"
                "Select the best matching menu item for this client. "
                "IMPORTANT: exclude any dish that contains an ingredient the client is intolerant to. "
                "If there is a good match, call prepare_dish with the exact dish name. "
                "If no safe dish is available, do nothing."
            )

            try:
                result = await self.a_run(task, tool_choice="required_first")
                # Record which dish we're preparing for this client
                if result:
                    for tool_call in result.tools_used:
                        if tool_call.name == "prepare_dish":
                            dish_name = tool_call.arguments.get("dish_name", "")
                            self._pending_orders[client_id] = {
                                "dish": dish_name,
                                "client_id": client_id,
                            }
                            log(
                                "serving",
                                self._state.turn_id,
                                "kitchen",
                                f"Preparing '{dish_name}' for client {client_id}",
                            )
            except Exception as exc:
                span.record_exception(exc)
                log_error("serving", self._state.turn_id, "client", f"_on_client_spawned failed: {exc}")

    async def _on_preparation_complete(self, data: dict[str, Any]) -> None:
        if self._state is None or self._mcp is None:
            return

        dish_name = data.get("dish") or data.get("name", "")
        log("serving", self._state.turn_id, "kitchen", f"Preparation complete: '{dish_name}'")

        # Find the client waiting for this dish
        target_client_id = None
        for cid, order in list(self._pending_orders.items()):
            if order["dish"] == dish_name:
                target_client_id = cid
                break

        if target_client_id is None:
            log(
                "serving",
                self._state.turn_id,
                "kitchen",
                f"No pending client for '{dish_name}' — ignoring",
            )
            return

        self._pending_orders.pop(target_client_id, None)

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
