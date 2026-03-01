"""
Mock server completo — SSE + REST API + MCP (JSON-RPC 2.0)

Replica l'intero server di gioco in locale per test end-to-end.

Uso:
    python mock_sse_server.py [--port 8765] [--speed 1.0] [--team-id 5]
    python main.py --test [--test-speed 3.0]

Endpoint esposti:
    GET  /events/{team_id}          ← SSE stream
    GET  /recipes                   ← lista ricette
    GET  /restaurants               ← lista ristoranti
    GET  /restaurant/{id}           ← info ristorante (balance, inventory)
    GET  /restaurant/{id}/menu      ← menu corrente
    GET  /meals                     ← ordini attivi (query: turn_id, restaurant_id)
    GET  /bid_history               ← storico aste (query: turn_id)
    GET  /market/entries            ← mercato attivo
    POST /mcp                       ← MCP JSON-RPC (tutti i tool)
    POST /phase/{phase}             ← forza fase (test manuale)
    POST /spawn_client              ← spawna cliente manuale
    POST /message                   ← invia messaggio SSE broadcast
    POST /game_started              ← avvia nuovo turno
    GET  /status                    ← stato server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


# ---------------------------------------------------------------------------
# Dati di gioco statici (ricette, ristoranti)
# ---------------------------------------------------------------------------

MOCK_RECIPES: list[dict[str, Any]] = [
    {
        "name": "Nebulosa Galattica",
        "preparationTimeMs": 1000,
        "ingredients": {
            "Radici di Gravità": 1,
            "Alghe Bioluminescenti": 1,
            "Foglie di Nebulosa": 1,
            "Gnocchi del Crepuscolo": 1,
            "Essenza di Tachioni": 1,
        },
        "prestige": 45,
    },
    {
        "name": "Eterea Sinfonia di Gravità con Infusione Temporale",
        "preparationTimeMs": 5000,
        "ingredients": {
            "Colonia di Mycoflora": 1,
            "Radici di Gravità": 1,
            "Carne di Xenodonte": 1,
            "Teste di Idra": 1,
            "Funghi dell'Etere": 1,
            "Essenza di Tachioni": 1,
        },
        "prestige": 84,
    },
    {
        "name": "Sinfonia Temporale di Fenice",
        "preparationTimeMs": 8000,
        "ingredients": {
            "Uova di Fenice": 1,
            "Plasma Vitale": 1,
            "Carne di Xenodonte": 1,
            "Essenza di Tachioni": 1,
            "Gnocchi del Crepuscolo": 1,
            "Pane degli Abissi": 1,
            "Polvere di Crononite": 1,
        },
        "prestige": 95,
    },
    {
        "name": "Portale Cosmico",
        "preparationTimeMs": 6000,
        "ingredients": {
            "Gnocchi del Crepuscolo": 1,
            "Essenza di Tachioni": 1,
            "Uova di Fenice": 1,
            "Foglie di Nebulosa": 1,
            "Plasma Vitale": 1,
        },
        "prestige": 100,
    },
    {
        "name": "Sinfonia Galattica",
        "preparationTimeMs": 15000,
        "ingredients": {
            "Cristalli di Nebulite": 1,
            "Colonia di Mycoflora": 1,
            "Teste di Idra": 1,
            "Carne di Xenodonte": 1,
            "Carne di Drago": 1,
            "Pane degli Abissi": 1,
            "Funghi dell'Etere": 1,
        },
        "prestige": 52,
    },
]

MOCK_RESTAURANTS: list[dict[str, Any]] = [
    {"id": 5,  "name": "Accattoni",  "is_open": True,  "reputation": 100},
    {"id": 7,  "name": "Starrats",   "is_open": True,  "reputation": 90},
    {"id": 12, "name": "Bluers",     "is_open": True,  "reputation": 85},
    {"id": 18, "name": "MockResto1", "is_open": True,  "reputation": 80},
    {"id": 25, "name": "MockResto2", "is_open": False, "reputation": 75},
]

OUR_RESTAURANT_NAME = "Accattoni"

# Clearing prices used by the simulated auction (per unit).
# These populate /bid_history so StrategyMemory.consolidate() can learn them.
MOCK_CLEARING_PRICES: dict[str, float] = {
    "Radici di Gravità":      8.0,
    "Alghe Bioluminescenti":  6.0,
    "Foglie di Nebulosa":     7.0,
    "Gnocchi del Crepuscolo": 12.0,
    "Essenza di Tachioni":    15.0,
    "Colonia di Mycoflora":   10.0,
    "Carne di Xenodonte":     11.0,
    "Teste di Idra":           9.0,
    "Funghi dell'Etere":       8.0,
    "Uova di Fenice":         14.0,
    "Plasma Vitale":          13.0,
    "Pane degli Abissi":       7.0,
    "Polvere di Crononite":   18.0,
    "Cristalli di Nebulite":   6.0,
    "Carne di Drago":         10.0,
}


# ---------------------------------------------------------------------------
# Stato mutabile condiviso
# ---------------------------------------------------------------------------

@dataclass
class MockGameState:
    balance: float = 1020.0
    inventory: dict[str, int] = field(default_factory=dict)
    menu: list[dict[str, Any]] = field(default_factory=list)
    is_open: bool = False
    current_turn_id: int = 1

    # Market: entries survive within a turn; cleared at turn start
    market_entries: list[dict[str, Any]] = field(default_factory=list)

    # Meals: accumulated across the serving phase; cleared at turn start
    # Each entry has: customerId(int), customer{name}, orderText, intolerances,
    #                 dish, price, executed(bool), servedDishId(str|None),
    #                 status(str), turn_id(int)
    active_meals: list[dict[str, Any]] = field(default_factory=list)
    _next_customer_id: int = 1

    # Bid data
    last_bids: list[dict[str, Any]] = field(default_factory=list)
    # Flat list of {ingredient, clearing_price, quantity} — read by /bid_history
    bid_history: list[dict[str, Any]] = field(default_factory=list)

    def next_customer_id(self) -> int:
        cid = self._next_customer_id
        self._next_customer_id += 1
        return cid

    def reset_turn(self, turn_id: int) -> None:
        """Reset per-turn ephemeral state."""
        self.current_turn_id = turn_id
        self.market_entries = []
        self.active_meals = []
        self.last_bids = []
        self._next_customer_id = 1


STATE = MockGameState()

# SSE queues: one per connected SSE client
_client_queues: set[asyncio.Queue] = set()


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_line(event_type: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


async def _broadcast(event_type: str, data: dict[str, Any]) -> None:
    line = _sse_line(event_type, data)
    for q in list(_client_queues):
        await q.put(line)


def _mcp_ok(text: str, extra: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "result": {
            "isError": False,
            "content": [{"type": "text", "text": text}],
            **(extra or {}),
        },
        "id": 1,
    }


def _mcp_err(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "result": {"isError": True, "content": [{"type": "text", "text": text}]},
        "id": 1,
    }


# ---------------------------------------------------------------------------
# Auction simulation
# ---------------------------------------------------------------------------

def _simulate_auction_results() -> None:
    """
    Award ingredients to our restaurant based on submitted bids.
    Produces flat bid_history entries matching StrategyMemory.consolidate():
        {"ingredient": str, "clearing_price": float, "quantity": int, "turn_id": int}
    """
    if not STATE.last_bids:
        return

    new_history: list[dict] = []
    for bid in STATE.last_bids:
        ing = bid.get("ingredient", "")
        qty = int(bid.get("quantity", 1))
        bid_price = float(bid.get("bid", 0.0))

        # Use known clearing price; fall back to bid price if unknown
        clearing = MOCK_CLEARING_PRICES.get(ing, bid_price)

        # Win the bid only if we offered at or above clearing price
        if bid_price >= clearing and STATE.balance >= clearing * qty:
            STATE.balance -= clearing * qty
            STATE.inventory[ing] = STATE.inventory.get(ing, 0) + qty
            print(f"[mock] Auction WON: {qty}x {ing} @ {clearing:.2f}", flush=True)
        else:
            qty = 0  # lost
            print(f"[mock] Auction LOST: {ing} (bid={bid_price:.2f}, clearing={clearing:.2f})", flush=True)

        new_history.append({
            "ingredient": ing,
            "clearing_price": clearing,
            "quantity": qty,
            "turn_id": STATE.current_turn_id,
        })

    STATE.bid_history = new_history   # replace with current turn results


# ---------------------------------------------------------------------------
# Meal factory
# ---------------------------------------------------------------------------

def _make_meal(
    client_name: str,
    order_text: str,
    intolerances: list[str],
    dish: str,
    price: float,
) -> dict[str, Any]:
    """Build a meal record matching the shape _resolve_customer_id expects."""
    return {
        "customerId": STATE.next_customer_id(),
        "customer": {"name": client_name},
        "orderText": order_text,
        "intolerances": intolerances,
        "dish": dish,
        "price": price,
        "executed": False,
        "servedDishId": None,
        "status": "active",
        "turn_id": STATE.current_turn_id,
    }


# ---------------------------------------------------------------------------
# SSE handler
# ---------------------------------------------------------------------------

async def sse_handler(request: web.Request) -> web.StreamResponse:
    team_id = request.match_info.get("team_id", "?")
    print(f"[mock] SSE client connected (team_id={team_id})", flush=True)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)
    await response.write(b"data: connected\n\n")

    queue: asyncio.Queue = asyncio.Queue()
    _client_queues.add(queue)
    try:
        while True:
            msg: bytes = await queue.get()
            await response.write(msg)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _client_queues.discard(queue)
        print(f"[mock] SSE client disconnected (team_id={team_id})", flush=True)
    return response


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

async def get_recipes(request: web.Request) -> web.Response:
    return web.json_response(MOCK_RECIPES)


async def get_restaurants(request: web.Request) -> web.Response:
    our_id = request.app["team_id"]
    result = []
    for r in MOCK_RESTAURANTS:
        entry = dict(r)
        if r["id"] == our_id:
            entry["is_open"] = STATE.is_open
        result.append(entry)
    return web.json_response(result)


async def get_restaurant_info(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    our_id = str(request.app["team_id"])
    if rid == our_id:
        return web.json_response({
            "id": int(rid),
            "name": OUR_RESTAURANT_NAME,
            "balance": STATE.balance,
            "inventory": STATE.inventory,
            "is_open": STATE.is_open,
            "reputation": 100,
        })
    resto = next((r for r in MOCK_RESTAURANTS if str(r["id"]) == rid), None)
    if resto is None:
        return web.Response(status=404, text="Restaurant not found")
    return web.json_response({**resto, "balance": 500.0, "inventory": {}})


async def get_restaurant_menu(request: web.Request) -> web.Response:
    return web.json_response(STATE.menu)


async def get_meals(request: web.Request) -> web.Response:
    """
    Returns meals for the current turn, optionally filtered by turn_id /
    restaurant_id query params (ignored in mock — always returns our meals).
    """
    # Respect turn_id filter if provided
    turn_id_param = request.rel_url.query.get("turn_id")
    if turn_id_param is not None:
        try:
            tid = int(turn_id_param)
            meals = [m for m in STATE.active_meals if m.get("turn_id") == tid]
        except ValueError:
            meals = STATE.active_meals
    else:
        meals = STATE.active_meals
    return web.json_response(meals)


async def get_bid_history(request: web.Request) -> web.Response:
    """
    Returns flat list of bid results for the requested turn.
    Shape: [{ingredient, clearing_price, quantity, turn_id}, ...]
    Matches what StrategyMemory.consolidate() reads.
    """
    turn_id_param = request.rel_url.query.get("turn_id")
    if turn_id_param:
        try:
            tid = int(turn_id_param)
            history = [e for e in STATE.bid_history if e.get("turn_id") == tid]
        except ValueError:
            history = STATE.bid_history
    else:
        history = STATE.bid_history
    return web.json_response(history)


async def get_market_entries(request: web.Request) -> web.Response:
    return web.json_response(STATE.market_entries)


# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

async def mcp_handler(request: web.Request) -> web.Response:
    body = await request.json()
    rpc_id = body.get("id", 1)
    params = body.get("params", {})
    tool = params.get("name", "")
    args = params.get("arguments", {}) or {}
    print(f"[mock] MCP → {tool}({json.dumps(args, ensure_ascii=False)})", flush=True)

    result = await _dispatch_tool(tool, args, request.app["team_id"])

    # Patch the jsonrpc id to match the request
    result["id"] = rpc_id
    content_text = (result.get("result", {}).get("content") or [{}])[0].get("text", "")
    print(f"[mock] MCP ← {tool}: {content_text}", flush=True)
    return web.json_response(result)


async def _dispatch_tool(tool: str, args: dict, our_team_id: int) -> dict:

    # ------------------------------------------------------------------
    # closed_bid
    # ------------------------------------------------------------------
    if tool == "closed_bid":
        bids = args.get("bids", [])
        STATE.last_bids = bids
        return _mcp_ok(f"Bid placed for {len(bids)} ingredient(s)")

    # ------------------------------------------------------------------
    # save_menu
    # ------------------------------------------------------------------
    if tool == "save_menu":
        STATE.menu = args.get("items", [])
        return _mcp_ok(f"Menu saved ({len(STATE.menu)} item(s))")

    # ------------------------------------------------------------------
    # create_market_entry  — supports ingredient_name (spec) + ingredient (legacy)
    # ------------------------------------------------------------------
    if tool == "create_market_entry":
        ing = args.get("ingredient_name") or args.get("ingredient", "")
        qty = int(args.get("quantity", 1))
        price = float(args.get("price", 0.0))
        side = str(args.get("side", "SELL")).upper()

        if not ing:
            return _mcp_err("ingredient_name is required")
        if qty <= 0:
            return _mcp_err("quantity must be > 0")

        if side == "SELL":
            if STATE.inventory.get(ing, 0) < qty:
                return _mcp_err(f"Insufficient inventory for {ing}: have {STATE.inventory.get(ing, 0)}, need {qty}")
            STATE.inventory[ing] = STATE.inventory.get(ing, 0) - qty

        entry_id = int(uuid.uuid4().int % 1_000_000)
        STATE.market_entries.append({
            "id": entry_id,
            "ingredient_name": ing,
            "ingredient": ing,          # legacy alias
            "quantity": qty,
            "price": price,
            "unit_price": price,
            "side": side,
            "restaurant_id": our_team_id,
            "restaurant_name": OUR_RESTAURANT_NAME,
            "status": "available",
            "turn_id": STATE.current_turn_id,
        })
        await _broadcast("message", {
            "sender": "server",
            "payload": f"The restaurant: {OUR_RESTAURANT_NAME} has created a new market entry.",
        })
        return _mcp_ok(f"Market entry {entry_id} created ({side} {qty}x {ing} @ {price})")

    # ------------------------------------------------------------------
    # execute_transaction  — market_entry_id (spec) + entry_id (legacy)
    # ------------------------------------------------------------------
    if tool == "execute_transaction":
        entry_id = args.get("market_entry_id") or args.get("entry_id")
        if entry_id is None:
            return _mcp_err("market_entry_id is required")
        try:
            entry_id = int(entry_id)
        except (ValueError, TypeError):
            pass  # keep as string for lookup

        entry = next((e for e in STATE.market_entries if e["id"] == entry_id), None)
        if entry is None:
            return _mcp_err(f"Market entry {entry_id} not found")

        side = entry.get("side", "SELL")
        ing = entry["ingredient_name"]
        qty = entry["quantity"]
        total_cost = entry["price"] * qty

        if side == "SELL":
            # We are buying from another restaurant
            if STATE.balance < total_cost:
                return _mcp_err(f"Insufficient balance: have {STATE.balance:.2f}, need {total_cost:.2f}")
            STATE.balance -= total_cost
            STATE.inventory[ing] = STATE.inventory.get(ing, 0) + qty
        else:
            # BUY entry: the other restaurant buys from us; we receive money
            STATE.balance += total_cost

        STATE.market_entries = [e for e in STATE.market_entries if e["id"] != entry_id]
        return _mcp_ok(f"Transaction complete: {qty}x {ing} for {total_cost:.2f}")

    # ------------------------------------------------------------------
    # delete_market_entry  — market_entry_id (spec) + entry_id (legacy)
    # ------------------------------------------------------------------
    if tool == "delete_market_entry":
        entry_id = args.get("market_entry_id") or args.get("entry_id")
        if entry_id is None:
            return _mcp_err("market_entry_id is required")
        try:
            entry_id = int(entry_id)
        except (ValueError, TypeError):
            pass

        entry = next((e for e in STATE.market_entries if e["id"] == entry_id), None)
        if entry is None:
            return _mcp_err(f"Market entry {entry_id} not found")

        # Return ingredients to inventory only for SELL entries (we had reserved them)
        if entry.get("side", "SELL") == "SELL":
            ing = entry["ingredient_name"]
            STATE.inventory[ing] = STATE.inventory.get(ing, 0) + entry["quantity"]

        STATE.market_entries = [e for e in STATE.market_entries if e["id"] != entry_id]
        return _mcp_ok(f"Market entry {entry_id} deleted")

    # ------------------------------------------------------------------
    # prepare_dish
    # ------------------------------------------------------------------
    if tool == "prepare_dish":
        dish_name = args.get("dish_name", "")
        recipe = next((r for r in MOCK_RECIPES if r["name"] == dish_name), None)
        if recipe is None:
            return _mcp_err(f"Unknown dish: {dish_name}")
        for ing, qty in recipe["ingredients"].items():
            if STATE.inventory.get(ing, 0) < qty:
                return _mcp_err(f"Missing ingredient: {ing} (have {STATE.inventory.get(ing, 0)}, need {qty})")
        # Deduct inventory immediately
        for ing, qty in recipe["ingredients"].items():
            STATE.inventory[ing] -= qty
            if STATE.inventory[ing] == 0:
                del STATE.inventory[ing]
        prep_s = recipe.get("preparationTimeMs", 3000) / 1000.0
        asyncio.create_task(_fire_preparation_complete(dish_name, prep_s))
        return _mcp_ok(f"Preparing '{dish_name}' (ready in {prep_s:.1f}s)")

    # ------------------------------------------------------------------
    # serve_dish
    # ------------------------------------------------------------------
    if tool == "serve_dish":
        dish_name = args.get("dish_name", "")
        client_id = args.get("client_id", "")

        # Match by customerId (int comparison, robust to str/int mismatch)
        meal = next(
            (
                m for m in STATE.active_meals
                if str(m.get("customerId")) == str(client_id)
                and not m.get("executed")
            ),
            None,
        )
        if meal is None:
            # Fallback: match first unserved meal
            meal = next(
                (m for m in STATE.active_meals if not m.get("executed")),
                None,
            )
            if meal:
                print(f"[mock] serve_dish: client_id {client_id!r} not found — falling back to first unserved", flush=True)

        revenue = 0.0
        if meal:
            revenue = float(meal.get("price", 0.0))
            STATE.balance += revenue
            meal["executed"] = True
            meal["servedDishId"] = dish_name
            meal["status"] = "served"
        else:
            print(f"[mock] serve_dish: no unserved meal found for client_id={client_id!r}", flush=True)

        return _mcp_ok(f"Served '{dish_name}' to client {client_id}, earned {revenue:.2f}")

    # ------------------------------------------------------------------
    # update_restaurant_is_open
    # ------------------------------------------------------------------
    if tool == "update_restaurant_is_open":
        STATE.is_open = bool(args.get("is_open", False))
        status = "opened" if STATE.is_open else "closed"
        return _mcp_ok(f"Restaurant {status}")

    # ------------------------------------------------------------------
    # send_message
    # ------------------------------------------------------------------
    if tool == "send_message":
        recipient = args.get("recipient_id", "?")
        text = args.get("text", "")
        await _broadcast("message", {
            "sender": "server",
            "payload": f"Restaurant {our_team_id} → Restaurant {recipient}: {text}",
        })
        return _mcp_ok("Message sent")

    return _mcp_err(f"Unknown tool: {tool}")


# ---------------------------------------------------------------------------
# SSE background tasks
# ---------------------------------------------------------------------------

async def _fire_preparation_complete(dish_name: str, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    await _broadcast("preparation_complete", {"dish": dish_name, "name": dish_name})
    print(f"[mock] SSE → preparation_complete: {dish_name}", flush=True)


async def heartbeat_task() -> None:
    while True:
        await asyncio.sleep(15.0)
        ts = int(time.time() * 1000)
        await _broadcast("heartbeat", {"ts": ts})
        print(f"[mock] ♥ heartbeat ts={ts}", flush=True)


# ---------------------------------------------------------------------------
# Manual control endpoints
# ---------------------------------------------------------------------------

async def post_message(request: web.Request) -> web.Response:
    body = await request.json()
    payload = body.get("payload", "test message")
    await _broadcast("message", {"sender": "server", "payload": payload})
    return web.json_response({"ok": True})


async def post_phase(request: web.Request) -> web.Response:
    """Force a phase transition. If phase=waiting, resolve the auction first."""
    phase = request.match_info["phase"]
    turn_id = int(request.rel_url.query.get("turn_id", str(STATE.current_turn_id)))
    STATE.current_turn_id = turn_id

    if phase == "waiting":
        _simulate_auction_results()
        print(f"[mock] Auction simulated → inventory={STATE.inventory}", flush=True)

    await _broadcast("game_phase_changed", {"phase": phase, "turn_id": turn_id})
    print(f"[mock] Phase forced → {phase} (turn={turn_id})", flush=True)
    return web.json_response({"ok": True, "phase": phase, "turn_id": turn_id})


async def post_game_started(request: web.Request) -> web.Response:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    turn_id = int(request.rel_url.query.get("turn_id", body.get("turn_id", STATE.current_turn_id + 1)))
    STATE.balance = 1020.0
    STATE.inventory = {}
    STATE.reset_turn(turn_id)
    await _broadcast("game_started", {"turn_id": turn_id})
    print(f"[mock] game_started (turn={turn_id})", flush=True)
    return web.json_response({"ok": True, "turn_id": turn_id})


async def post_spawn_client(request: web.Request) -> web.Response:
    """
    Manually spawn a client for testing.
    Body JSON: {clientName, orderText, intolerances?, dish?, price?}
    """
    body = await request.json()
    client_name = body.get("clientName", f"TestClient-{STATE._next_customer_id}")
    order_text = body.get("orderText", "Qualcosa di galattico, per favore.")
    intolerances = body.get("intolerances", [])
    dish = body.get("dish", MOCK_RECIPES[0]["name"] if MOCK_RECIPES else "")
    price = float(body.get("price", 500.0))

    meal = _make_meal(client_name, order_text, intolerances, dish, price)
    STATE.active_meals.append(meal)

    await _broadcast("client_spawned", {
        "clientName": client_name,
        "orderText": order_text,
        "intolerances": intolerances,
    })
    print(f"[mock] Manual spawn: {client_name} (customerId={meal['customerId']})", flush=True)
    return web.json_response({"ok": True, "customerId": meal["customerId"], "clientName": client_name})


async def get_status(request: web.Request) -> web.Response:
    return web.json_response({
        "sse_clients": len(_client_queues),
        "turn_id": STATE.current_turn_id,
        "balance": STATE.balance,
        "inventory": STATE.inventory,
        "menu": [i.get("name") for i in STATE.menu],
        "is_open": STATE.is_open,
        "market_entries": len(STATE.market_entries),
        "active_meals": len(STATE.active_meals),
        "unserved_meals": sum(1 for m in STATE.active_meals if not m.get("executed")),
        "bid_history_entries": len(STATE.bid_history),
    })


# ---------------------------------------------------------------------------
# Scenario automatico
# ---------------------------------------------------------------------------

# Scenario events:
#   (delay_s, event_type, data)
#   Special pseudo-events:
#     "_auction"      → resolve auction (called just before waiting)
#     "_spawn_order"  → add a meal + broadcast client_spawned
#     "_reset_turn"   → reset per-turn state (balance preserved)

DEFAULT_SCENARIO: list[tuple[float, str, dict[str, Any]]] = [
    # ── Turn 8 ──────────────────────────────────────────────────────────────
    (0.5,  "game_started",       {"turn_id": 8}),
    (2.0,  "game_phase_changed", {"phase": "speaking",   "turn_id": 8}),
    (5.0,  "game_phase_changed", {"phase": "closed_bid", "turn_id": 8}),
    (4.0,  "message",            {"sender": "server",
                                   "payload": "The restaurant: Starrats has created a new market entry."}),
    # Resolve auction just before waiting phase
    (4.0,  "_auction",           {}),
    (0.1,  "game_phase_changed", {"phase": "waiting",    "turn_id": 8}),
    (8.0,  "game_phase_changed", {"phase": "serving",    "turn_id": 8}),
    # Diverse client archetypes
    (1.0,  "_spawn_order", {
        "clientName": "Astrobaron Xerxes",
        "orderText": "Voglio il piatto più esclusivo e veloce della galassia, presto!",
        "intolerances": [],
        "dish": "Nebulosa Galattica",
        "price": 625.0,
    }),
    (4.0,  "_spawn_order", {
        "clientName": "GalacticExplorer-7",
        "orderText": "Qualcosa di economico ma soddisfacente per un lungo viaggio.",
        "intolerances": ["Teste di Idra"],
        "dish": "Eterea Sinfonia di Gravità con Infusione Temporale",
        "price": 320.0,
    }),
    (5.0,  "_spawn_order", {
        "clientName": "Saggio Omega del Cosmo",
        "orderText": "Cerco qualcosa di raro, con ingredienti del cosmo profondo. Il prezzo non è un problema.",
        "intolerances": [],
        "dish": "Portale Cosmico",
        "price": 980.0,
    }),
    (12.0, "game_phase_changed", {"phase": "stopped",    "turn_id": 8}),

    # ── Turn 9 ──────────────────────────────────────────────────────────────
    (3.0,  "game_started",       {"turn_id": 9}),
    (2.0,  "game_phase_changed", {"phase": "speaking",   "turn_id": 9}),
    (5.0,  "game_phase_changed", {"phase": "closed_bid", "turn_id": 9}),
    (4.0,  "_auction",           {}),
    (0.1,  "game_phase_changed", {"phase": "waiting",    "turn_id": 9}),
    (8.0,  "game_phase_changed", {"phase": "serving",    "turn_id": 9}),
    (2.0,  "_spawn_order", {
        "clientName": "Famiglia Orbitale Rossi",
        "orderText": "Siamo una famiglia numerosa. Vogliamo qualcosa di buono e abbordabile.",
        "intolerances": [],
        "dish": "Nebulosa Galattica",
        "price": 450.0,
    }),
    (6.0,  "_spawn_order", {
        "clientName": "Astrobaron Krenn",
        "orderText": "Solo il meglio. Niente compromessi.",
        "intolerances": ["Alghe Bioluminescenti"],
        "dish": "Sinfonia Temporale di Fenice",
        "price": 1100.0,
    }),
    (15.0, "game_phase_changed", {"phase": "stopped",    "turn_id": 9}),
]


async def scenario_task(scenario: list[tuple], speed: float) -> None:
    print(f"[mock] Scenario started ({len(scenario)} steps, speed={speed}x)", flush=True)
    for delay, event_type, data in scenario:
        await asyncio.sleep(delay / speed)

        # ── Internal pseudo-events ─────────────────────────────────────────

        if event_type == "_auction":
            _simulate_auction_results()
            print(f"[mock] Auction resolved → inventory={STATE.inventory}", flush=True)
            continue

        if event_type == "_spawn_order":
            client_name = data.get("clientName", f"Client-{STATE._next_customer_id}")
            order_text = data.get("orderText", "Un piatto galattico.")
            intolerances = data.get("intolerances", [])
            dish = data.get("dish", "")
            price = float(data.get("price", 500.0))

            meal = _make_meal(client_name, order_text, intolerances, dish, price)
            STATE.active_meals.append(meal)

            await _broadcast("client_spawned", {
                "clientName": client_name,
                "orderText": order_text,
                "intolerances": intolerances,
            })
            print(f"[mock] Client spawned: {client_name} (customerId={meal['customerId']})", flush=True)
            continue

        # ── Real SSE events ────────────────────────────────────────────────

        if event_type == "game_started":
            turn_id = data.get("turn_id", STATE.current_turn_id + 1)
            STATE.reset_turn(turn_id)

        if event_type == "game_phase_changed" and data.get("phase") == "waiting":
            # Ensure auction is resolved before waiting (in case _auction step was skipped)
            if STATE.last_bids and not STATE.bid_history:
                _simulate_auction_results()

        await _broadcast(event_type, data)
        print(f"[mock] SSE → {event_type}: {data}", flush=True)

    print("[mock] Scenario complete.", flush=True)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def build_app(speed: float = 1.0, run_scenario: bool = True, team_id: int = 5) -> web.Application:
    app = web.Application()
    app["team_id"] = team_id

    # SSE
    app.router.add_get("/events/{team_id}", sse_handler)

    # REST
    app.router.add_get("/recipes",              get_recipes)
    app.router.add_get("/restaurants",          get_restaurants)
    app.router.add_get("/restaurant/{id}",      get_restaurant_info)
    app.router.add_get("/restaurant/{id}/menu", get_restaurant_menu)
    app.router.add_get("/meals",                get_meals)
    app.router.add_get("/bid_history",          get_bid_history)
    app.router.add_get("/market/entries",       get_market_entries)

    # MCP
    app.router.add_post("/mcp", mcp_handler)

    # Manual control
    app.router.add_post("/message",      post_message)
    app.router.add_post("/phase/{phase}", post_phase)
    app.router.add_post("/spawn_client", post_spawn_client)
    app.router.add_post("/game_started", post_game_started)
    app.router.add_get("/status",        get_status)

    async def on_startup(app: web.Application) -> None:
        asyncio.create_task(heartbeat_task())
        if run_scenario:
            asyncio.create_task(scenario_task(DEFAULT_SCENARIO, speed))

    app.on_startup.append(on_startup)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mock server for Hackapizza 2.0")
    parser.add_argument("--port",        type=int,   default=8765)
    parser.add_argument("--speed",       type=float, default=1.0,
                        help="Scenario playback speed multiplier (e.g. 3.0 = 3x faster)")
    parser.add_argument("--team-id",     type=int,   default=5)
    parser.add_argument("--no-scenario", action="store_true",
                        help="Start server without running the automatic scenario")
    args = parser.parse_args()

    app = build_app(speed=args.speed, run_scenario=not args.no_scenario, team_id=args.team_id)

    print(f"[mock] Server at http://localhost:{args.port}")
    print(f"[mock]   SSE:          GET  /events/{{team_id}}")
    print(f"[mock]   REST:         GET  /recipes | /restaurants | /restaurant/{{id}}")
    print(f"[mock]                 GET  /restaurant/{{id}}/menu | /meals | /market/entries | /bid_history")
    print(f"[mock]   MCP:          POST /mcp  (JSON-RPC 2.0)")
    print(f"[mock]   Phase ctrl:   POST /phase/{{speaking|closed_bid|waiting|serving|stopped}}?turn_id=N")
    print(f"[mock]   Spawn client: POST /spawn_client  {{clientName, orderText, intolerances, dish, price}}")
    print(f"[mock]   Game start:   POST /game_started?turn_id=N")
    print(f"[mock]   Status:       GET  /status")
    print()

    web.run_app(app, host="0.0.0.0", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
