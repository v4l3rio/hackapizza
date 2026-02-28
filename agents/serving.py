from __future__ import annotations

import json
from typing import Any

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
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
      - preparation_complete: directly serves the ready dish to the waiting client.
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

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        self._http: HttpClient | None = None
        self._pending_orders: dict[str, list[str]] = {}  # dish_name -> [client_id, ...]
        super().__init__(client=get_llm_client(), max_steps=3)

    # ------------------------------------------------------------------ tools

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
    ) -> None:
        """Called when the serving phase starts — restaurant already opened in waiting phase."""
        self._state = state
        self._strat = memory
        self._mcp = mcp
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

            # Filter recipes to only those on the menu AND cookable (have all ingredients in stock)
            menu_names = {item.get("name") for item in self._state.menu_items}
            cookable = self._state.cookable_dishes()
            menu_recipes = [r for r in cookable if r.get("name") in menu_names]

            # Let LLM pick best matching dish and call prepare_dish
            task = (
                f"Il cliente '{client_id}' è arrivato.\n"
                f"Il suo ordine: \"{order_text}\"\n"
                f"Le sue intolleranze/allergie alimentari: {json.dumps(intolerances)}\n\n"
                "Identifica l'archetipo del cliente dal suo nome e dal testo dell'ordine:\n"
                "  - Galactic Explorer → prezzo più basso + meno ingredienti (preparazione veloce)\n"
                "  - Astrobaron → prezzo più alto + meno ingredienti (preparazione veloce)\n"
                "  - Space Sage → ingredienti più rari/prestigiosi\n"
                "  - Orbital Family → miglior rapporto prezzo-qualità\n\n"
                f"Menu attuale (nome, prezzo, descrizione): {json.dumps(self._state.menu_items)}\n"
                f"Ricette con ingredienti (per controllo intolleranze): {json.dumps(menu_recipes)}\n\n"
                "Seleziona il piatto del menu che corrisponde meglio usando i criteri dell'archetipo sopra. "
                "IMPORTANTE: escludi qualsiasi piatto contenente un ingrediente a cui il cliente è intollerante. "
                "Se c'è una buona corrispondenza, chiama prepare_dish con il nome esatto del piatto. "
                "Se nessun piatto sicuro è disponibile, non fare nulla."
            )

            try:
                result = await self.a_run(task)
                # Record which dish we're preparing for this client
                if result:
                    for tool_call in result.tools_used:
                        if tool_call.name == "prepare_dish":
                            dish_name = tool_call.arguments.get("dish_name", "")
                            if dish_name:
                                self._pending_orders.setdefault(dish_name, []).append(client_id)
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

        # _pending_orders maps dish_name -> [customer_name, ...] (customer.name, not id)
        customers = self._pending_orders.get(dish_name, [])

        target_customer_name: str = ""
        if customers:
            target_customer_name = customers.pop(0)
            if not customers:
                del self._pending_orders[dish_name]

            # Resolve customer name -> customerId via /meals
            try:
                target_customer_id = await self._lookup_customer_id_from_meals(target_customer_name)
            except Exception as exc:
                log_error(
                    "serving",
                    self._state.turn_id,
                    "meal_lookup",
                    f"Failed to lookup customerId for '{target_customer_name}': {exc}",
                )
                return
        else:
            # Dish name mismatch between prepare_dish call and preparation_complete event —
            # fall back to /meals to find the first unserved customer in this turn.
            log(
                "serving",
                self._state.turn_id,
                "kitchen",
                f"No pending order found for '{dish_name}' — falling back to /meals lookup",
            )
            try:
                target_customer_id = await self._lookup_any_unserved_customer()
            except Exception as exc:
                log_error(
                    "serving",
                    self._state.turn_id,
                    "meal_lookup",
                    f"Fallback lookup failed for '{dish_name}': {exc}",
                )
                return

        try:
            result = await self._mcp.serve_dish(dish_name, str(target_customer_id))
            log(
                "serving",
                self._state.turn_id,
                "serve",
                f"Served '{dish_name}' to customer {target_customer_id} ('{target_customer_name}'): {result}",
            )
        except Exception as exc:
            log_error(
                "serving",
                self._state.turn_id,
                "serve",
                f"serve_dish failed for customer {target_customer_id} ('{target_customer_name}'): {exc}",
            )

    async def _lookup_any_unserved_customer(self) -> int:
        if self._state is None:
            raise RuntimeError("Missing state")

        turn_id = self._state.turn_id
        restaurant_id = self._http.team_id

        meals = await self._http.get_meals(turn_id=turn_id, restaurant_id=restaurant_id)

        for m in meals:
            if (
                not m.get("executed", False)
                and (m.get("status") or "").lower() not in ("cancelled", "canceled")
                and m.get("servedDishId") is None
            ):
                cid = m.get("customerId")
                if cid is not None:
                    name = ((m.get("customer") or {}).get("name") or "")
                    log("serving", turn_id, "meal_lookup", f"Fallback: serving customer '{name}' (id={cid})")
                    return int(cid)

        raise LookupError(f"No unserved customer found in /meals (turn_id={turn_id})")

    async def _lookup_customer_id_from_meals(self, customer_name: str) -> int:
        if self._state is None:
            raise RuntimeError("Missing state")

        turn_id = self._state.turn_id
        restaurant_id = self._http.team_id

        meals = await self._http.get_meals(turn_id=turn_id, restaurant_id=restaurant_id)

        key = customer_name.strip().lower()

        # Prefer non-executed, non-cancelled meals
        preferred = [
            m for m in meals
            if not m.get("executed", False)
            and (m.get("status") or "").lower() not in ("cancelled", "canceled")
        ]
        haystack = preferred or meals

        for m in haystack:
            name = ((m.get("customer") or {}).get("name") or "").strip().lower()
            if name == key:
                cid = m.get("customerId")
                if cid is None:
                    raise RuntimeError(f"Found '{customer_name}' but customerId missing in meal id={m.get('id')}")
                return int(cid)

        raise LookupError(f"customerId not found for customer.name='{customer_name}' (turn_id={turn_id})")