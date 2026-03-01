from __future__ import annotations

import asyncio
import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import Tool
from datapizza.tools.mcp_client import MCPClient

from config import MIN_DISH_TO_FULFILL_OR_CLOSE_FRACTION
from state.game_state import GameState
from infrastructure.sse_listener import SSEListener
from infrastructure.http_client import HttpClient
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class ServingAgent(Agent):
    """
    Handles the 'serving' phase using LLM-driven dish matching.

    Registers SSE handlers for:
      - client_spawned: LLM picks the best matching dish (respecting intolerances) and prepares it.
      - preparation_complete: serves the ready dish to the waiting client.
    """

    name = "serving_agent"
    system_prompt = (
        "Sei l'agente di servizio per il nostro ristorante. "
        "Quando arriva un cliente, identifica il piatto migliore dal nostro menu che corrisponda al suo ordine "
        "ed eviti qualsiasi ingrediente a cui è intollerante. "
        "Usa l'archetipo del cliente per guidare la tua scelta:\n"
        "  - Galactic Explorer: budget basso, poco tempo → scegli il piatto più ECONOMICO e VELOCE.\n"
        "  - Astrobaron: budget alto, poco tempo → scegli il piatto più PREMIUM e VELOCE.\n"
        "  - Space Sage: budget illimitato, molto tempo → scegli il piatto più PRESTIGIOSO o RARO.\n"
        "  - Orbital Family: equilibrato → scegli il miglior rapporto QUALITÀ-PREZZO.\n"
        "Se l'archetipo non è esplicito, deducilo dal testo dell'ordine e dal nome del cliente. "
        "Chiama prepare_dish con il nome esatto del piatto dal nostro menu. "
        "Agisci con decisione — chiama sempre uno strumento per agire."
    )

    def __init__(self, mcp: MCPClient, mcp_tools: list[Tool]) -> None:
        self._state: GameState | None = None
        self._mcp = mcp
        self._http: HttpClient | None = None
        self._pending_orders: dict[str, list[str]] = {}  # dish_name -> [client_name, ...]
        super().__init__(client=get_llm_client(), tools=mcp_tools, max_steps=3)

    def register(self, sse: SSEListener, state: GameState, mcp: MCPClient, http: HttpClient) -> None:
        """Register SSE handlers. Call once at startup."""
        self._state = state
        self._mcp = mcp
        self._http = http
        sse.on("client_spawned", self._on_client_spawned)
        sse.on("preparation_complete", self._on_preparation_complete)

    async def execute(self, state: GameState) -> None:
        """Called when the serving phase starts."""
        self._state = state
        self._pending_orders.clear()
        with tracer.start_as_current_span("serving_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)
            log("serving", state.turn_id, "agent", "ServingAgent started — restaurant already open")
            log("serving", state.turn_id, "state", f"Balance={state.balance:.2f} | Inventory={state.inventory}")

    # ------------------------------------------------------------------ SSE handlers

    async def _on_client_spawned(self, data: dict[str, Any]) -> None:
        asyncio.create_task(self._process_client_spawned(data))

    async def _process_client_spawned(self, data: dict[str, Any]) -> None:
        if self._state is None or self._state.phase != "serving":
            return

        client_name = str(data.get("clientName") or data.get("client_id") or data.get("id", "unknown"))
        order_text = str(data.get("orderText") or data.get("order") or data.get("text", ""))
        intolerances = data.get("intolerances") or data.get("allergies") or []

        log("serving", self._state.turn_id, "client", f"Client {client_name} wants: '{order_text}'")

        menu_names = {item.get("name") for item in self._state.menu_items}
        cookable = self._state.cookable_dishes()
        menu_recipes = [r for r in cookable if r.get("name") in menu_names]

        task = (
            f"Il cliente '{client_name}' è arrivato.\n"
            f"Il suo ordine: \"{order_text}\"\n"
            f"Le sue intolleranze/allergie alimentari: {json.dumps(intolerances)}\n\n"
            "Identifica l'archetipo del cliente dal suo nome e dal testo dell'ordine:\n"
            "  - Galactic Explorer → prezzo più basso + meno ingredienti (preparazione veloce)\n"
            "  - Astrobaron → prezzo più alto + meno ingredienti (preparazione veloce)\n"
            "  - Space Sage → ingredienti più rari/prestigiosi\n"
            "  - Orbital Family → miglior rapporto prezzo-qualità\n\n"
            f"Menu attuale (nome, prezzo): {json.dumps(self._state.menu_items)}\n"
            f"Ricette con ingredienti (per controllo intolleranze): {json.dumps(menu_recipes)}\n\n"
            "Seleziona il piatto che corrisponde meglio all'archetipo evitando ingredienti a cui il cliente è intollerante. "
            "Se disponibile, chiama prepare_dish con il nome esatto. Se nessun piatto sicuro è disponibile, non fare nulla."
        )

        with tracer.start_as_current_span("serving_agent.client_spawned") as span:
            span.set_attribute("client_name", client_name)
            span.set_attribute("turn_id", self._state.turn_id)
            try:
                result = await self.a_run(task)
                if result:
                    for tc in result.tools_used:
                        if tc.name == "prepare_dish":
                            dish = tc.arguments.get("dish_name", "")
                            if dish:
                                self._pending_orders.setdefault(dish, []).append(client_name)
                                log("serving", self._state.turn_id, "kitchen", f"Preparing '{dish}' for {client_name}")
            except Exception as exc:
                span.record_exception(exc)
                log_error("serving", self._state.turn_id, "client", f"_on_client_spawned failed: {exc}")

    async def _on_preparation_complete(self, data: dict[str, Any]) -> None:
        if self._state is None or self._state.phase != "serving":
            return

        dish_name = data.get("dish")
        log("serving", self._state.turn_id, "kitchen", f"Preparation complete: '{dish_name}'")

        pending = self._pending_orders.get(dish_name, [])
        client_name: str | None = pending.pop(0) if pending else None
        if not pending and dish_name in self._pending_orders:
            del self._pending_orders[dish_name]

        try:
            customer_id = await self._resolve_customer_id(client_name)
            await self._mcp.call_tool("serve_dish", {"dish_name": dish_name, "client_id": str(customer_id)})
            log("serving", self._state.turn_id, "serve", f"Served '{dish_name}' to customer {customer_id} ('{client_name}')")
        except Exception as exc:
            log_error("serving", self._state.turn_id, "serve", f"serve_dish failed for '{dish_name}': {exc}")
            # return

        # After each successful serve, refresh inventory and close if nothing left to cook.
        await self._close_if_no_cookable_dishes()

    async def _close_if_no_cookable_dishes(self) -> None:
        """Refresh inventory then close the restaurant if no menu dish can still be cooked."""
        if self._state is None or self._http is None:
            await self._mcp.call_tool("update_restaurant_is_open", {"is_open": False})
            log("serving", self._state.turn_id if self._state else 0, "close_check", "State or http error, restaurant close")
            return

        try:
            await self._state.refresh_all(self._http)
        except Exception as exc:
            log_error("serving", self._state.turn_id, "close_check", f"State refresh failed: {exc}")

        menu_names = {item.get("name") for item in self._state.menu_items}
        full_menu_count = len(menu_names)
        still_cookable = [r for r in self._state.cookable_dishes() if r.get("name") in menu_names]

        if len(still_cookable) >= MIN_DISH_TO_FULFILL_OR_CLOSE_FRACTION * full_menu_count:
            log(
                "serving",
                self._state.turn_id,
                "close_check",
                f"Still {len(still_cookable)} cook-able menu dishes ({len(still_cookable)/full_menu_count:.2%}) — staying open",
            )
            return

        log("serving", self._state.turn_id, "close_check", f"Only {len(still_cookable)} cook-able menu dishes left ({len(still_cookable)/full_menu_count:.2%}) — closing restaurant")
        try:
            await self._mcp.call_tool("update_restaurant_is_open", {"is_open": False})
            log("serving", self._state.turn_id, "close_check", "Restaurant closed")
        except Exception as exc:
            log_error("serving", self._state.turn_id, "close_check", f"Failed to close restaurant: {exc}")

    async def _resolve_customer_id(self, client_name: str | None) -> int:
        """Resolve client name to numeric customer ID via /meals.

        If name is provided, matches by name first. Falls back to first unserved customer.
        """
        if self._state is None or self._http is None:
            raise RuntimeError("ServingAgent not registered")

        turn_id = self._state.turn_id
        meals = await self._http.get_meals(turn_id=turn_id, restaurant_id=self._http.team_id)
        active = [
            m for m in meals
            if not m.get("executed")
            and (m.get("status") or "").lower() not in ("cancelled", "canceled")
            and m.get("servedDishId") is None
        ]

        if client_name:
            key = client_name.strip().lower()
            for m in active:
                if ((m.get("customer") or {}).get("name") or "").strip().lower() == key:
                    return m["customerId"]
            log("serving", turn_id, "meal_lookup", f"Name '{client_name}' not found — falling back to first unserved")

        if active:
            m = active[0]
            name = ((m.get("customer") or {}).get("name") or "")
            log("serving", turn_id, "meal_lookup", f"Serving first unserved customer '{name}' (id={m['customerId']})")
            return m["customerId"]

        raise LookupError(f"No unserved customer found in /meals (turn_id={turn_id})")
