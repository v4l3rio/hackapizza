"""
Ristorante Dashboard — Python web service
==========================================
Serves:
  GET /                        → static dashboard HTML
  GET /api/status              → full current snapshot + deltas vs previous dump
  GET /api/market              → all market entries grouped by ingredient
  GET /api/market/<ingredient> → market entries for a specific ingredient (URL-encoded)
  GET /api/restaurant          → my restaurant info + delta
  GET /api/restaurants         → all other restaurants + deltas
  GET /api/recipes             → all recipes
  GET /api/menu                → my restaurant menu
  GET /api/dump/history        → list of all past dump timestamps
  GET /api/dump/latest         → raw latest dump
  GET /api/dump/previous       → raw previous dump
  POST /api/config             → update base_url and api_key at runtime
  GET /api/config              → get current config (key redacted)

Scheduler runs a full dump every REFRESH_INTERVAL_SECONDS (default 30).
Delta is computed field-by-field between the two most recent dumps.
"""

import json
import os
import sys
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from typing import Any

# ── CONFIG ────────────────────────────────────────────────────────────────────

CONFIG = {
    "base_url": os.environ.get("BASE_URL", "").rstrip("/"),
    "api_key":  os.environ.get("API_KEY",  ""),
    "my_restaurant_id": os.environ.get("MY_RESTAURANT_ID", "5"),
    "refresh_interval": int(os.environ.get("REFRESH_INTERVAL", "30")),
    "port": int(os.environ.get("PORT", "8765")),
}

# ── STATE ─────────────────────────────────────────────────────────────────────

# Each dump is a dict: { "ts": ISO, "data": { ... } }
dumps: list[dict] = []
dumps_lock = threading.Lock()


def latest_dump() -> dict | None:
    with dumps_lock:
        return dumps[-1] if dumps else None

def previous_dump() -> dict | None:
    with dumps_lock:
        return dumps[-2] if len(dumps) >= 2 else None


# ── UPSTREAM FETCH ────────────────────────────────────────────────────────────

def upstream_get(path: str) -> Any:
    base = CONFIG["base_url"]
    if not base:
        raise ValueError("base_url not configured")
    url = base + path
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if CONFIG["api_key"]:
        req.add_header("x-api-key", CONFIG["api_key"])
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def fetch_all() -> dict:
    """Fetch all endpoints and return a combined snapshot."""
    result = {}
    endpoints = {
        "restaurant":  f"/restaurant/{CONFIG['my_restaurant_id']}",
        "menu":        f"/restaurant/{CONFIG['my_restaurant_id']}/menu",
        "restaurants": "/restaurants",
        "market":      "/market/entries",
        "recipes":     "/recipes",
    }
    errors = {}
    for key, path in endpoints.items():
        try:
            result[key] = upstream_get(path)
        except Exception as e:
            errors[key] = str(e)
            result[key] = None
    result["_errors"] = errors
    return result


# ── DELTA ENGINE ──────────────────────────────────────────────────────────────

def scalar_delta(old, new) -> dict | None:
    """Return a delta dict for a scalar value, or None if unchanged."""
    if old is None or new is None:
        return None
    if type(old) != type(new):
        return {"prev": old, "curr": new, "changed": True}
    if isinstance(old, (int, float)):
        diff = new - old
        pct = round(diff / old * 100, 2) if old != 0 else None
        return {"prev": old, "curr": new, "diff": diff, "pct": pct, "changed": diff != 0}
    if old != new:
        return {"prev": old, "curr": new, "changed": True}
    return {"prev": old, "curr": new, "changed": False}


def restaurant_delta(old_r: dict | None, new_r: dict) -> dict:
    """Compute delta for a restaurant object."""
    if not old_r:
        return {"_new": True}
    delta = {}
    for field in ("balance", "reputation", "isOpen"):
        d = scalar_delta(old_r.get(field), new_r.get(field))
        if d and d.get("changed"):
            delta[field] = d
    # inventory changes
    old_inv = old_r.get("inventory", {})
    new_inv = new_r.get("inventory", {})
    inv_delta = {}
    for k in set(list(old_inv.keys()) + list(new_inv.keys())):
        old_v = old_inv.get(k)
        new_v = new_inv.get(k)
        if old_v != new_v:
            inv_delta[k] = {"prev": old_v, "curr": new_v}
    if inv_delta:
        delta["inventory"] = inv_delta
    # menu changes
    old_items = {i["name"]: i["price"] for i in (old_r.get("menu", {}) or {}).get("items", [])}
    new_items = {i["name"]: i["price"] for i in (new_r.get("menu", {}) or {}).get("items", [])}
    menu_delta = {}
    for name in set(list(old_items.keys()) + list(new_items.keys())):
        if old_items.get(name) != new_items.get(name):
            menu_delta[name] = {"prev": old_items.get(name), "curr": new_items.get(name)}
    if menu_delta:
        delta["menu"] = menu_delta
    return delta


def market_delta(old_entries: list | None, new_entries: list) -> dict:
    """Compute market delta: new entries, cancelled entries, price changes."""
    if not old_entries:
        return {"_new": True}
    old_by_id = {e["id"]: e for e in old_entries}
    new_by_id = {e["id"]: e for e in new_entries}
    added   = [e for eid, e in new_by_id.items() if eid not in old_by_id]
    removed = [e for eid, e in old_by_id.items() if eid not in new_by_id]
    changed = []
    for eid, new_e in new_by_id.items():
        if eid in old_by_id:
            old_e = old_by_id[eid]
            if old_e.get("status") != new_e.get("status"):
                changed.append({
                    "id": eid,
                    "field": "status",
                    "prev": old_e.get("status"),
                    "curr": new_e.get("status"),
                })
    return {
        "added":   added,
        "removed": removed,
        "changed": changed,
        "_summary": {
            "added_count":   len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
        }
    }


def compute_deltas(old_data: dict | None, new_data: dict) -> dict:
    """Top-level delta between two snapshots."""
    if not old_data:
        return {}
    deltas = {}

    # My restaurant
    if new_data.get("restaurant") and old_data.get("restaurant"):
        d = restaurant_delta(old_data["restaurant"], new_data["restaurant"])
        if d:
            deltas["restaurant"] = d

    # Market
    if new_data.get("market") is not None and old_data.get("market") is not None:
        deltas["market"] = market_delta(old_data["market"], new_data["market"])

    # Other restaurants (by id)
    old_rs = {r["id"]: r for r in (old_data.get("restaurants") or [])}
    new_rs = {r["id"]: r for r in (new_data.get("restaurants") or [])}
    rs_deltas = {}
    for rid, new_r in new_rs.items():
        if rid == CONFIG["my_restaurant_id"]:
            continue
        d = restaurant_delta(old_rs.get(rid), new_r)
        if d:
            rs_deltas[rid] = d
    if rs_deltas:
        deltas["restaurants"] = rs_deltas

    return deltas


# ── MARKET HELPERS ────────────────────────────────────────────────────────────

def group_market_by_ingredient(entries: list) -> dict:
    """Group market entries by ingredient name, sorted by unit price asc."""
    groups: dict[str, list] = {}
    for e in entries:
        name = (e.get("ingredient") or {}).get("name") or f"Ingrediente #{e.get('ingredientId')}"
        groups.setdefault(name, []).append(e)
    # Sort each group by unit price
    for name in groups:
        groups[name].sort(key=lambda e: e["totalPrice"] / max(e["quantity"], 1))
    # Sort group names alphabetically
    return dict(sorted(groups.items()))


def enrich_market_entries(entries: list, restaurants: list) -> list:
    """Add restaurant_name field to each market entry."""
    r_map = {r["id"]: r["name"] for r in (restaurants or [])}
    for e in entries:
        rid = str(e.get("createdByRestaurantId", ""))
        e["restaurant_name"] = r_map.get(rid, f"Ristorante #{rid}")
        e["unit_price"] = round(e["totalPrice"] / max(e["quantity"], 1), 4)
        e["is_mine"] = rid == str(CONFIG["my_restaurant_id"])
    return entries


# ── SCHEDULER ─────────────────────────────────────────────────────────────────

def do_dump():
    """Fetch everything, compute delta, store dump."""
    if not CONFIG["base_url"]:
        print("[scheduler] base_url not set, skipping dump")
        return
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[scheduler] dump at {ts}")
    try:
        data = fetch_all()
        # Enrich market entries with restaurant names
        if data.get("market") and data.get("restaurants"):
            data["market"] = enrich_market_entries(data["market"], data["restaurants"])
        prev = previous_dump()
        delta = compute_deltas(prev["data"] if prev else None, data)
        with dumps_lock:
            dumps.append({"ts": ts, "data": data, "delta": delta})
            # Keep only last 100 dumps to avoid unbounded memory growth
            if len(dumps) > 100:
                dumps.pop(0)
        print(f"[scheduler] dump ok — market:{len(data.get('market') or [])} entries, delta keys:{list(delta.keys())}")
    except Exception as e:
        print(f"[scheduler] dump error: {e}")


def scheduler_loop():
    while True:
        do_dump()
        time.sleep(CONFIG["refresh_interval"])


# ── HTTP HANDLER ──────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg: str, status: int = 500):
        self.send_json({"error": msg}, status)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        query  = urllib.parse.parse_qs(parsed.query)

        # ── Static files ──
        if path == "/" or path == "/index.html":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return

        # ── API routes ──
        if not path.startswith("/api"):
            self.send_error_json("Not found", 404)
            return

        if path == "/api/config":
            self.send_json({
                "base_url": CONFIG["base_url"],
                "api_key":  "***" if CONFIG["api_key"] else "",
                "my_restaurant_id": CONFIG["my_restaurant_id"],
                "refresh_interval": CONFIG["refresh_interval"],
            })

        elif path == "/api/status":
            dump = latest_dump()
            if not dump:
                self.send_json({"status": "no_data", "message": "No dump yet. Configure base_url and wait for first refresh."})
                return
            prev = previous_dump()
            self.send_json({
                "ts":       dump["ts"],
                "prev_ts":  prev["ts"] if prev else None,
                "data":     dump["data"],
                "delta":    dump["delta"],
            })

        elif path == "/api/restaurant":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            self.send_json({
                "ts":   dump["ts"],
                "data": dump["data"].get("restaurant"),
                "menu": dump["data"].get("menu"),
                "delta": dump["delta"].get("restaurant"),
            })

        elif path == "/api/restaurants":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            rs = dump["data"].get("restaurants") or []
            rs_deltas = dump["delta"].get("restaurants", {})
            enriched = []
            for r in rs:
                enriched.append({**r, "_delta": rs_deltas.get(r["id"])})
            self.send_json({"ts": dump["ts"], "data": enriched})

        elif path == "/api/recipes":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            self.send_json({"ts": dump["ts"], "data": dump["data"].get("recipes")})

        elif path == "/api/menu":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            self.send_json({"ts": dump["ts"], "data": dump["data"].get("menu")})

        elif path == "/api/market":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            entries = dump["data"].get("market") or []
            grouped = group_market_by_ingredient(entries)
            market_delta_data = dump["delta"].get("market", {})
            # Optionally filter by side
            side = (query.get("side", [None])[0] or "").upper()
            if side in ("BUY", "SELL"):
                grouped = {k: [e for e in v if e.get("side") == side] for k, v in grouped.items()}
                grouped = {k: v for k, v in grouped.items() if v}
            self.send_json({
                "ts":      dump["ts"],
                "grouped": grouped,
                "delta":   market_delta_data,
                "summary": {
                    "total_entries":     len(entries),
                    "unique_ingredients": len(grouped),
                    "my_entries":        sum(1 for e in entries if e.get("is_mine")),
                }
            })

        elif path.startswith("/api/market/"):
            ingredient = urllib.parse.unquote(path[len("/api/market/"):])
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            entries = dump["data"].get("market") or []
            grouped = group_market_by_ingredient(entries)
            matches = grouped.get(ingredient)
            if matches is None:
                # fuzzy: case-insensitive partial match
                ingredient_lower = ingredient.lower()
                for key, val in grouped.items():
                    if ingredient_lower in key.lower():
                        matches = val
                        break
            if matches is None:
                self.send_error_json(f"Ingredient '{ingredient}' not found", 404); return
            self.send_json({
                "ts":         dump["ts"],
                "ingredient": ingredient,
                "entries":    matches,
                "best_unit_price": min(e["unit_price"] for e in matches),
                "total_quantity":  sum(e["quantity"] for e in matches),
            })

        elif path == "/api/dump/history":
            with dumps_lock:
                history = [{"ts": d["ts"], "delta_summary": d["delta"].get("market", {}).get("_summary")} for d in dumps]
            self.send_json({"count": len(history), "dumps": history})

        elif path == "/api/dump/latest":
            dump = latest_dump()
            if not dump:
                self.send_error_json("No data yet", 503); return
            self.send_json(dump)

        elif path == "/api/dump/previous":
            dump = previous_dump()
            if not dump:
                self.send_error_json("No previous dump yet", 404); return
            self.send_json(dump)

        else:
            self.send_error_json("Unknown route", 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_error_json("Invalid JSON", 400); return
            changed = False
            for key in ("base_url", "api_key", "my_restaurant_id", "refresh_interval"):
                if key in payload:
                    if key == "base_url":
                        CONFIG[key] = payload[key].rstrip("/")
                    elif key == "refresh_interval":
                        CONFIG[key] = int(payload[key])
                    else:
                        CONFIG[key] = payload[key]
                    changed = True
            if changed:
                print(f"[config] updated: base_url={CONFIG['base_url']}, restaurant_id={CONFIG['my_restaurant_id']}")
                # Trigger immediate dump in background
                threading.Thread(target=do_dump, daemon=True).start()
            self.send_json({"ok": True, "config": {
                "base_url": CONFIG["base_url"],
                "my_restaurant_id": CONFIG["my_restaurant_id"],
                "refresh_interval": CONFIG["refresh_interval"],
            }})

        elif path == "/api/refresh":
            threading.Thread(target=do_dump, daemon=True).start()
            self.send_json({"ok": True, "message": "Refresh triggered"})

        else:
            self.send_error_json("Unknown route", 404)

    def _serve_static(self, filename: str, content_type: str):
        fpath = os.path.join(STATIC_DIR, filename)
        if not os.path.exists(fpath):
            self.send_error_json(f"File not found: {filename}", 404)
            return
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── ENTRYPOINT ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════╗
║       Ristorante Dashboard — Server          ║
╠══════════════════════════════════════════════╣
║  http://localhost:{CONFIG['port']}                        ║
║  Refresh interval: {CONFIG['refresh_interval']}s                        ║
║  My restaurant ID: {CONFIG['my_restaurant_id']}                         ║
╚══════════════════════════════════════════════╝
""")

    # Start scheduler thread
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    # If base_url already set via env, do an immediate first dump
    if CONFIG["base_url"]:
        threading.Thread(target=do_dump, daemon=True).start()

    server = HTTPServer(("0.0.0.0", CONFIG["port"]), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
        server.shutdown()
