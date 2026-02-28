"""
Mock server completo — SSE + REST API + MCP (JSON-RPC 2.0)

Replica l'intero server di gioco in locale per test end-to-end.

Uso:
    python mock_sse_server.py [--port 8765] [--speed 1.0]
    python main.py --test [--test-speed 3.0]

Endpoint esposti:
    GET  /events/{team_id}          ← SSE stream
    GET  /recipes                   ← lista ricette
    GET  /restaurants               ← lista ristoranti
    GET  /restaurant/{id}           ← info ristorante (balance, inventory)
    GET  /restaurant/{id}/menu      ← menu corrente
    GET  /meals                     ← ordini attivi
    GET  /bid_history               ← storico aste
    GET  /market/entries            ← mercato
    POST /mcp                       ← MCP JSON-RPC (tutti i tool)
    POST /phase/{phase}             ← forza fase (test manuale)
    POST /message                   ← invia messaggio SSE
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
        "name": "Sinfonia Temporale di Fenice e Xenodonte su Pane degli Abissi con Colata di Plasma Vitale e Polvere di Crononite",
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
        "name": "Portale Cosmico: Sinfonia di Gnocchi del Crepuscolo con Essenza di Tachioni e Sfumature di Fenice",
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


# ---------------------------------------------------------------------------
# Stato mutabile condiviso
# ---------------------------------------------------------------------------

@dataclass
class MockGameState:
    balance: float = 1020.0
    inventory: dict[str, int] = field(default_factory=dict)
    menu: list[dict[str, Any]] = field(default_factory=list)
    is_open: bool = False
    market_entries: list[dict[str, Any]] = field(default_factory=list)
    active_meals: list[dict[str, Any]] = field(default_factory=list)
    bid_history: list[dict[str, Any]] = field(default_factory=list)
    last_bids: list[dict[str, Any]] = field(default_factory=list)
    # piatti in cottura: {dish_name: asyncio.Task}
    cooking: dict[str, Any] = field(default_factory=dict)


STATE = MockGameState()

# Set di code SSE: una per ogni client connesso
_client_queues: set[asyncio.Queue] = set()


# ---------------------------------------------------------------------------
# Helpers SSE
# ---------------------------------------------------------------------------

def _sse_line(event_type: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


async def _broadcast(event_type: str, data: dict[str, Any]) -> None:
    line = _sse_line(event_type, data)
    for q in list(_client_queues):
        await q.put(line)


def _mcp_ok(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "result": {"isError": False, "content": [{"type": "text", "text": text}]},
        "id": 1,
    }


def _mcp_err(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "result": {"isError": True, "content": [{"type": "text", "text": text}]},
        "id": 1,
    }


# ---------------------------------------------------------------------------
# Simulazione asta: dopo closed_bid → waiting assegna ingredienti
# ---------------------------------------------------------------------------

def _simulate_auction_results() -> None:
    """
    Distribuisce gli ingredienti delle ultime bid come se avessimo vinto
    l'asta. Scala il balance in modo realistico.
    """
    if not STATE.last_bids:
        return
    won: list[dict] = []
    for bid in STATE.last_bids:
        ing = bid.get("ingredient", "")
        qty = int(bid.get("quantity", 1))
        price = float(bid.get("bid", 50.0))
        total = price * qty
        if STATE.balance >= total:
            STATE.balance -= total
            STATE.inventory[ing] = STATE.inventory.get(ing, 0) + qty
            won.append({"ingredient": ing, "quantity": qty, "paid": total})
    # Aggiungi allo storico bid
    STATE.bid_history.append({
        "turn_id": 1,
        "bids": STATE.last_bids,
        "results": won,
    })


# ---------------------------------------------------------------------------
# SSE handler
# ---------------------------------------------------------------------------

async def sse_handler(request: web.Request) -> web.StreamResponse:
    team_id = request.match_info.get("team_id", "?")
    print(f"[mock] SSE client connesso (team_id={team_id})", flush=True)

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
        print(f"[mock] SSE client disconnesso (team_id={team_id})", flush=True)
    return response


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

async def get_recipes(request: web.Request) -> web.Response:
    return web.json_response(MOCK_RECIPES)


async def get_restaurants(request: web.Request) -> web.Response:
    return web.json_response(MOCK_RESTAURANTS)


async def get_restaurant_info(request: web.Request) -> web.Response:
    rid = request.match_info["id"]
    # Cerca tra i ristoranti mockati; per il nostro team restituisce lo stato live
    our_id = str(request.app["team_id"])
    if rid == our_id:
        return web.json_response({
            "id": int(rid),
            "name": "Accattoni",
            "balance": STATE.balance,
            "inventory": STATE.inventory,
            "is_open": STATE.is_open,
            "reputation": 100,
        })
    # Ristorante generico
    resto = next((r for r in MOCK_RESTAURANTS if str(r["id"]) == rid), None)
    if resto is None:
        return web.Response(status=404, text="Restaurant not found")
    return web.json_response({**resto, "balance": 500.0, "inventory": {}})


async def get_restaurant_menu(request: web.Request) -> web.Response:
    return web.json_response(STATE.menu)


async def get_meals(request: web.Request) -> web.Response:
    # Ogni meal contiene "client_id" — usarlo come argomento di serve_dish
    return web.json_response(STATE.active_meals)


async def get_bid_history(request: web.Request) -> web.Response:
    return web.json_response(STATE.bid_history)


async def get_market_entries(request: web.Request) -> web.Response:
    return web.json_response(STATE.market_entries)


# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

async def mcp_handler(request: web.Request) -> web.Response:
    body = await request.json()
    params = body.get("params", {})
    tool = params.get("name", "")
    args = params.get("arguments", {}) or {}
    print(f"[mock] MCP → {tool}({args})", flush=True)

    result = await _dispatch_tool(tool, args)
    print(f"[mock] MCP ← {tool}: {result.get('result', {}).get('content', '')}", flush=True)
    return web.json_response(result)


async def _dispatch_tool(tool: str, args: dict) -> dict:
    if tool == "closed_bid":
        bids = args.get("bids", [])
        STATE.last_bids = bids
        return _mcp_ok(f"Bid placed for {len(bids)} ingredient(s)")

    if tool == "save_menu":
        STATE.menu = args.get("items", [])
        return _mcp_ok("Menu saved")

    if tool == "create_market_entry":
        ing = args.get("ingredient", "")
        qty = int(args.get("quantity", 1))
        price = float(args.get("price", 0))
        if STATE.inventory.get(ing, 0) < qty:
            return _mcp_err(f"Insufficient inventory: {ing}")
        entry_id = str(uuid.uuid4())[:8]
        STATE.inventory[ing] = STATE.inventory.get(ing, 0) - qty
        STATE.market_entries.append({
            "id": entry_id,
            "ingredient": ing,
            "quantity": qty,
            "price": price,
            "unit_price": price,
            "restaurant_id": 5,
            "restaurant_name": "Accattoni",
            "status": "available",
        })
        await _broadcast("message", {
            "sender": "server",
            "payload": f"The restaurant: Accattoni has created a new market entry.",
        })
        return _mcp_ok(f"Market entry {entry_id} created")

    if tool == "execute_transaction":
        entry_id = args.get("entry_id", "")
        entry = next((e for e in STATE.market_entries if e["id"] == entry_id), None)
        if entry is None:
            return _mcp_err(f"Market entry {entry_id} not found")
        total = entry["price"] * entry["quantity"]
        if STATE.balance < total:
            return _mcp_err("Insufficient balance")
        STATE.balance -= total
        ing = entry["ingredient"]
        STATE.inventory[ing] = STATE.inventory.get(ing, 0) + entry["quantity"]
        STATE.market_entries = [e for e in STATE.market_entries if e["id"] != entry_id]
        return _mcp_ok(f"Bought {entry['quantity']} {ing} for {total}")

    if tool == "delete_market_entry":
        entry_id = args.get("entry_id", "")
        entry = next((e for e in STATE.market_entries if e["id"] == entry_id), None)
        if entry:
            STATE.inventory[entry["ingredient"]] = (
                STATE.inventory.get(entry["ingredient"], 0) + entry["quantity"]
            )
            STATE.market_entries = [e for e in STATE.market_entries if e["id"] != entry_id]
        return _mcp_ok(f"Market entry {entry_id} deleted")

    if tool == "prepare_dish":
        dish_name = args.get("dish_name", "")
        recipe = next((r for r in MOCK_RECIPES if r["name"] == dish_name), None)
        if recipe is None:
            return _mcp_err(f"Unknown dish: {dish_name}")
        # Verifica inventario
        for ing, qty in recipe["ingredients"].items():
            if STATE.inventory.get(ing, 0) < qty:
                return _mcp_err(f"Missing ingredient: {ing}")
        # Scala inventario
        for ing, qty in recipe["ingredients"].items():
            STATE.inventory[ing] -= qty
        prep_ms = recipe.get("preparationTimeMs", 3000)
        # Fire SSE preparation_complete dopo prep_ms
        asyncio.create_task(_fire_preparation_complete(dish_name, prep_ms / 1000))
        return _mcp_ok(f"Preparing {dish_name}")

    if tool == "serve_dish":
        dish_name = args.get("dish_name", "")
        client_id = args.get("client_id", "")
        # Trova il pasto attivo
        meal = next((m for m in STATE.active_meals if m.get("client_id") == client_id), None)
        revenue = 0.0
        if meal:
            revenue = float(meal.get("price", 0))
            STATE.balance += revenue
            STATE.active_meals = [m for m in STATE.active_meals if m.get("client_id") != client_id]
        return _mcp_ok(f"Served {dish_name} to client {client_id}, earned {revenue}")

    if tool == "update_restaurant_is_open":
        STATE.is_open = bool(args.get("is_open", False))
        status = "opened" if STATE.is_open else "closed"
        return _mcp_ok(f"Restaurant {status}")

    if tool == "send_message":
        recipient = args.get("recipient_id", "?")
        text = args.get("text", "")
        await _broadcast("message", {
            "sender": "server",
            "payload": f"Restaurant 5 → Restaurant {recipient}: {text}",
        })
        return _mcp_ok("Message sent")

    return _mcp_err(f"Unknown tool: {tool}")


async def _fire_preparation_complete(dish_name: str, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    # Il serving agent legge data.get("dish") or data.get("name")
    await _broadcast("preparation_complete", {"dish": dish_name, "name": dish_name})
    print(f"[mock] SSE → preparation_complete: {dish_name}", flush=True)


# ---------------------------------------------------------------------------
# Handler di controllo manuale (test)
# ---------------------------------------------------------------------------

async def post_message(request: web.Request) -> web.Response:
    body = await request.json()
    payload = body.get("payload", "test message")
    await _broadcast("message", {"sender": "server", "payload": payload})
    return web.json_response({"ok": True})


async def post_phase(request: web.Request) -> web.Response:
    phase = request.match_info["phase"]
    turn_id = int(request.rel_url.query.get("turn_id", "1"))
    if phase == "waiting":
        _simulate_auction_results()
    await _broadcast("game_phase_changed", {"phase": phase, "turn_id": turn_id})
    print(f"[mock] Phase forced → {phase} (turn={turn_id})", flush=True)
    return web.json_response({"ok": True, "phase": phase})


async def post_game_started(request: web.Request) -> web.Response:
    turn_id = int(request.rel_url.query.get("turn_id", "1"))
    STATE.balance = 1020.0
    STATE.inventory = {}
    STATE.last_bids = []
    await _broadcast("game_started", {"turn_id": turn_id})
    print(f"[mock] game_started (turn={turn_id})", flush=True)
    return web.json_response({"ok": True})


async def get_status(request: web.Request) -> web.Response:
    return web.json_response({
        "clients": len(_client_queues),
        "balance": STATE.balance,
        "inventory": STATE.inventory,
        "menu": [i.get("name") for i in STATE.menu],
        "is_open": STATE.is_open,
        "market_entries": len(STATE.market_entries),
        "active_meals": len(STATE.active_meals),
    })


# ---------------------------------------------------------------------------
# Task di background
# ---------------------------------------------------------------------------

async def heartbeat_task() -> None:
    while True:
        await asyncio.sleep(15.0)
        ts = int(time.time() * 1000)
        await _broadcast("heartbeat", {"ts": ts})
        print(f"[mock] ♥ heartbeat ts={ts}", flush=True)


# Scenario di default — replica la sequenza dei log
DEFAULT_SCENARIO: list[tuple[float, str, dict[str, Any]]] = [
    (0.5,  "game_started",       {"turn_id": 8}),
    (2.0,  "game_phase_changed", {"phase": "speaking",   "turn_id": 8}),
    (5.0,  "game_phase_changed", {"phase": "closed_bid", "turn_id": 8}),
    (4.0,  "message",            {"sender": "server",
                                   "payload": "The restaurant: Starrats has created a new market entry."}),
    # Prima del waiting: risolviamo l'asta
    (8.0,  "_auction",           {}),  # evento interno — non broadcastato
    (0.1,  "game_phase_changed", {"phase": "waiting",    "turn_id": 8}),
    (3.0,  "message",            {"sender": "server",
                                   "payload": "The restaurant: Accattoni has created a new market entry."}),
    (10.0, "game_phase_changed", {"phase": "serving",    "turn_id": 8}),
    # Clienti variegati — client_id leggibile via GET /meals
    (2.0,  "_spawn_order", {
        "client_id": "AstrobaronX-42",
        "dish": "Nebulosa Galattica",
        "price": 625.0,
        "orderText": "Voglio il piatto più esclusivo e veloce della galassia, presto!",
        "intolerances": [],
    }),
    (4.0,  "_spawn_order", {
        "client_id": "GalacticExplorer-7",
        "dish": "Eterea Sinfonia di Gravità con Infusione Temporale",
        "price": 750.0,
        "orderText": "Qualcosa di economico ma soddisfacente per un lungo viaggio.",
        "intolerances": ["Teste di Idra"],
    }),
    (7.0,  "_spawn_order", {
        "client_id": "SpaceSage-Omega",
        "dish": "Nebulosa Galattica",
        "price": 625.0,
        "orderText": "Cerco qualcosa di raro, con ingredienti del cosmo profondo.",
        "intolerances": [],
    }),
    (15.0, "game_phase_changed", {"phase": "stopped",    "turn_id": 8}),
    # Secondo turno
    (3.0,  "game_started",       {"turn_id": 9}),
    (2.0,  "game_phase_changed", {"phase": "speaking",   "turn_id": 9}),
    (5.0,  "game_phase_changed", {"phase": "closed_bid", "turn_id": 9}),
    (8.0,  "_auction",           {}),
    (0.1,  "game_phase_changed", {"phase": "waiting",    "turn_id": 9}),
    (10.0, "game_phase_changed", {"phase": "serving",    "turn_id": 9}),
    (3.0,  "_spawn_order", {
        "client_id": "OrbitalFamily-99",
        "dish": "Nebulosa Galattica",
        "price": 625.0,
        "orderText": "Siamo una famiglia, vogliamo qualcosa di buono e abbordabile.",
        "intolerances": [],
    }),
    (15.0, "game_phase_changed", {"phase": "stopped",    "turn_id": 9}),
]


async def scenario_task(scenario: list[tuple], speed: float) -> None:
    print(f"[mock] Scenario avviato ({len(scenario)} step, speed={speed}x)", flush=True)
    for delay, event_type, data in scenario:
        await asyncio.sleep(delay / speed)

        if event_type == "_auction":
            _simulate_auction_results()
            print(f"[mock] Asta simulata → inventory={STATE.inventory}", flush=True)
            continue

        if event_type == "_spawn_order":
            client_id = data["client_id"]
            # Aggiunge il pasto ad active_meals — client_id è il campo usato
            # da serve_dish e leggibile via GET /meals
            meal = {
                "client_id": client_id,
                "clientName": client_id,
                "dish": data["dish"],
                "price": data["price"],
                "orderText": data.get("orderText", "Un piatto galattico, veloce e saporito"),
                "intolerances": data.get("intolerances", []),
            }
            STATE.active_meals.append(meal)
            # Il serving agent ascolta "client_spawned", non "order_placed"
            # client_id è recuperabile anche da GET /meals
            await _broadcast("client_spawned", {
                "clientName": client_id,
                "client_id": client_id,
                "orderText": meal["orderText"],
                "intolerances": meal["intolerances"],
            })
            print(f"[mock] Cliente arrivato: {client_id} → GET /meals per client_id", flush=True)
            continue

        # reset state su game_started
        if event_type == "game_started":
            STATE.balance = 1020.0
            STATE.inventory = {}
            STATE.last_bids = []
            STATE.active_meals = []
            STATE.cooking = {}

        await _broadcast(event_type, data)
        print(f"[mock] → {event_type}: {data}", flush=True)

    print("[mock] Scenario completato.", flush=True)


# ---------------------------------------------------------------------------
# Costruzione app
# ---------------------------------------------------------------------------

def build_app(speed: float = 1.0, run_scenario: bool = True, team_id: int = 5) -> web.Application:
    app = web.Application()
    app["team_id"] = team_id

    # SSE
    app.router.add_get("/events/{team_id}", sse_handler)

    # REST
    app.router.add_get("/recipes", get_recipes)
    app.router.add_get("/restaurants", get_restaurants)
    app.router.add_get("/restaurant/{id}", get_restaurant_info)
    app.router.add_get("/restaurant/{id}/menu", get_restaurant_menu)
    app.router.add_get("/meals", get_meals)
    app.router.add_get("/bid_history", get_bid_history)
    app.router.add_get("/market/entries", get_market_entries)

    # MCP
    app.router.add_post("/mcp", mcp_handler)

    # Controllo manuale
    app.router.add_post("/message", post_message)
    app.router.add_post("/phase/{phase}", post_phase)
    app.router.add_post("/game_started", post_game_started)
    app.router.add_get("/status", get_status)

    async def on_startup(app: web.Application) -> None:
        asyncio.create_task(heartbeat_task())
        if run_scenario:
            asyncio.create_task(scenario_task(DEFAULT_SCENARIO, speed))

    app.on_startup.append(on_startup)
    return app


# ---------------------------------------------------------------------------
# Entry point (uso standalone)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mock server completo per Hackapizza")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Velocità scenario (es. 3.0 = 3x più veloce)")
    parser.add_argument("--team-id", type=int, default=5)
    parser.add_argument("--no-scenario", action="store_true",
                        help="Solo heartbeat, nessuno scenario automatico")
    args = parser.parse_args()

    app = build_app(speed=args.speed, run_scenario=not args.no_scenario, team_id=args.team_id)

    print(f"[mock] Server su http://localhost:{args.port}")
    print(f"[mock]   SSE:        GET  /events/{{team_id}}")
    print(f"[mock]   REST:       GET  /recipes | /restaurants | /restaurant/{{id}} | /meals | /market/entries")
    print(f"[mock]   MCP:        POST /mcp  (JSON-RPC 2.0)")
    print(f"[mock]   Ctrl fase:  POST /phase/{{speaking|closed_bid|waiting|serving|stopped}}?turn_id=1")
    print(f"[mock]   Stato:      GET  /status")
    print()

    web.run_app(app, host="0.0.0.0", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
