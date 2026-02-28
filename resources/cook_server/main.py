"""
Ristorante Dashboard — Python web service
==========================================
Run:
    pip install fastapi uvicorn httpx python-dotenv
    uvicorn server:app --port 8765 --reload
"""

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
import httpx
import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any
from dotenv import load_dotenv

# ── LOAD .env ─────────────────────────────────────────────────────────────────

load_dotenv()  # reads .env from cwd or project root automatically

# ── CONFIG ────────────────────────────────────────────────────────────────────

CONFIG = {
    "base_url":           os.environ.get("BASE_URL", "").rstrip("/"),
    "api_key":            os.environ.get("API_KEY",  ""),
    "my_restaurant_id":   os.environ.get("MY_RESTAURANT_ID", "5"),
    "refresh_interval":   int(os.environ.get("REFRESH_INTERVAL", "30")),
    "port":               int(os.environ.get("PORT", "8765")),
    "dumps_dir":          os.environ.get("DUMPS_DIR", os.path.join(os.path.dirname(__file__), "dumps")),
}

# ── STATE ─────────────────────────────────────────────────────────────────────

dumps: list[dict] = []
dumps_lock = threading.Lock()

def latest_dump() -> dict | None:
    with dumps_lock:
        return dumps[-1] if dumps else None

def previous_dump() -> dict | None:
    with dumps_lock:
        return dumps[-2] if len(dumps) >= 2 else None

# ── PERSISTENCE ───────────────────────────────────────────────────────────────

def ensure_dumps_dir():
    os.makedirs(CONFIG["dumps_dir"], exist_ok=True)

def dump_filename(ts: str) -> str:
    safe = ts.replace(":", "-").replace(".", "-")
    return os.path.join(CONFIG["dumps_dir"], f"{safe}.json")

def persist_dump(dump: dict):
    try:
        ensure_dumps_dir()
        with open(dump_filename(dump["ts"]), "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[persist] write error: {e}")

def load_dumps_from_disk() -> list[dict]:
    ensure_dumps_dir()
    result = []
    for fname in sorted(os.listdir(CONFIG["dumps_dir"])):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONFIG["dumps_dir"], fname), "r", encoding="utf-8") as f:
                result.append(json.load(f))
        except Exception as e:
            print(f"[persist] read error {fname}: {e}")
    return result

def list_dump_files() -> list[str]:
    ensure_dumps_dir()
    return sorted(f for f in os.listdir(CONFIG["dumps_dir"]) if f.endswith(".json"))

# ── UPSTREAM FETCH ────────────────────────────────────────────────────────────

def upstream_get(path: str) -> Any:
    base = CONFIG["base_url"]
    if not base:
        raise ValueError("base_url not configured")
    headers = {"Accept": "application/json"}
    if CONFIG["api_key"]:
        headers["x-api-key"] = CONFIG["api_key"]
    with httpx.Client(timeout=10) as client:
        resp = client.get(base + path, headers=headers)
        resp.raise_for_status()
        return resp.json()

def fetch_all() -> dict:
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
    if not old_r:
        return {"_new": True}
    delta = {}
    for field in ("balance", "reputation", "isOpen"):
        d = scalar_delta(old_r.get(field), new_r.get(field))
        if d and d.get("changed"):
            delta[field] = d
    old_inv = old_r.get("inventory", {})
    new_inv = new_r.get("inventory", {})
    inv_delta = {}
    for k in set(list(old_inv.keys()) + list(new_inv.keys())):
        if old_inv.get(k) != new_inv.get(k):
            inv_delta[k] = {"prev": old_inv.get(k), "curr": new_inv.get(k)}
    if inv_delta:
        delta["inventory"] = inv_delta
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
    if not old_entries:
        return {"_new": True}
    old_by_id = {e["id"]: e for e in old_entries}
    new_by_id = {e["id"]: e for e in new_entries}
    added   = [e for eid, e in new_by_id.items() if eid not in old_by_id]
    removed = [e for eid, e in old_by_id.items() if eid not in new_by_id]
    changed = []
    for eid, new_e in new_by_id.items():
        if eid in old_by_id and old_by_id[eid].get("status") != new_e.get("status"):
            changed.append({"id": eid, "field": "status", "prev": old_by_id[eid].get("status"), "curr": new_e.get("status")})
    return {
        "added": added, "removed": removed, "changed": changed,
        "_summary": {"added_count": len(added), "removed_count": len(removed), "changed_count": len(changed)},
    }

def compute_deltas(old_data: dict | None, new_data: dict) -> dict:
    if not old_data:
        return {}
    deltas = {}
    if new_data.get("restaurant") and old_data.get("restaurant"):
        d = restaurant_delta(old_data["restaurant"], new_data["restaurant"])
        if d:
            deltas["restaurant"] = d
    if new_data.get("market") is not None and old_data.get("market") is not None:
        deltas["market"] = market_delta(old_data["market"], new_data["market"])
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
    groups: dict[str, list] = {}
    for e in entries:
        name = (e.get("ingredient") or {}).get("name") or f"Ingrediente #{e.get('ingredientId')}"
        groups.setdefault(name, []).append(e)
    for name in groups:
        groups[name].sort(key=lambda e: e["totalPrice"] / max(e["quantity"], 1))
    return dict(sorted(groups.items()))

def enrich_market_entries(entries: list, restaurants: list) -> list:
    r_map = {r["id"]: r["name"] for r in (restaurants or [])}
    for e in entries:
        rid = str(e.get("createdByRestaurantId", ""))
        e["restaurant_name"] = r_map.get(rid, f"Ristorante #{rid}")
        e["unit_price"] = round(e["totalPrice"] / max(e["quantity"], 1), 4)
        e["is_mine"] = rid == str(CONFIG["my_restaurant_id"])
    return entries

# ── SCHEDULER ─────────────────────────────────────────────────────────────────

def do_dump():
    if not CONFIG["base_url"]:
        print("[scheduler] base_url not set, skipping")
        return
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[scheduler] dump at {ts}")
    try:
        data = fetch_all()
        if data.get("market") and data.get("restaurants"):
            data["market"] = enrich_market_entries(data["market"], data["restaurants"])
        prev = previous_dump()
        delta = compute_deltas(prev["data"] if prev else None, data)
        dump = {"ts": ts, "data": data, "delta": delta}
        with dumps_lock:
            dumps.append(dump)
            if len(dumps) > 100:
                dumps.pop(0)
        persist_dump(dump)
        print(f"[scheduler] ok — {len(data.get('market') or [])} market entries")
    except Exception as e:
        print(f"[scheduler] error: {e}")

def scheduler_loop():
    while True:
        do_dump()
        time.sleep(CONFIG["refresh_interval"])

# ── FASTAPI APP ───────────────────────────────────────────────────────────────

app = FastAPI(title="Ristorante Dashboard API")

@app.on_event("startup")
def startup():
    # Load persisted dumps
    disk_dumps = load_dumps_from_disk()
    if disk_dumps:
        with dumps_lock:
            dumps.extend(disk_dumps[-100:])
        print(f"[startup] loaded {len(disk_dumps)} dumps from disk")
    # Start scheduler
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    if CONFIG["base_url"]:
        threading.Thread(target=do_dump, daemon=True).start()

# ── STATIC FILES ──────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

# ── CONFIG ROUTES ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    return {
        "base_url":           CONFIG["base_url"],
        "api_key":            "***" if CONFIG["api_key"] else "",
        "my_restaurant_id":   CONFIG["my_restaurant_id"],
        "refresh_interval":   CONFIG["refresh_interval"],
        "dumps_dir":          CONFIG["dumps_dir"],
    }

@app.post("/api/config")
def set_config(payload: dict):
    for key in ("base_url", "api_key", "my_restaurant_id", "refresh_interval"):
        if key in payload:
            CONFIG[key] = payload[key].rstrip("/") if key == "base_url" else int(payload[key]) if key == "refresh_interval" else payload[key]
    threading.Thread(target=do_dump, daemon=True).start()
    return {"ok": True, "config": get_config()}

@app.post("/api/refresh")
def manual_refresh():
    threading.Thread(target=do_dump, daemon=True).start()
    return {"ok": True, "message": "Refresh triggered"}

# ── STATUS ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    dump = latest_dump()
    if not dump:
        return {"status": "no_data", "message": "No dump yet. Configure base_url and wait."}
    prev = previous_dump()
    return {"ts": dump["ts"], "prev_ts": prev["ts"] if prev else None, "data": dump["data"], "delta": dump["delta"]}

# ── RESTAURANT ────────────────────────────────────────────────────────────────

@app.get("/api/restaurant")
def get_restaurant():
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    return {"ts": dump["ts"], "data": dump["data"].get("restaurant"), "menu": dump["data"].get("menu"), "delta": dump["delta"].get("restaurant")}

@app.get("/api/menu")
def get_menu():
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    return {"ts": dump["ts"], "data": dump["data"].get("menu")}

# ── RESTAURANTS ───────────────────────────────────────────────────────────────

@app.get("/api/restaurants")
def get_restaurants():
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    rs = dump["data"].get("restaurants") or []
    rs_deltas = dump["delta"].get("restaurants", {})
    return {"ts": dump["ts"], "data": [{**r, "_delta": rs_deltas.get(r["id"])} for r in rs]}

# ── RECIPES ───────────────────────────────────────────────────────────────────

@app.get("/api/recipes")
def get_recipes():
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    return {"ts": dump["ts"], "data": dump["data"].get("recipes")}

# ── MARKET ────────────────────────────────────────────────────────────────────

@app.get("/api/market")
def get_market(side: str = None):
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    entries = dump["data"].get("market") or []
    grouped = group_market_by_ingredient(entries)
    if side and side.upper() in ("BUY", "SELL"):
        s = side.upper()
        grouped = {k: [e for e in v if e.get("side") == s] for k, v in grouped.items()}
        grouped = {k: v for k, v in grouped.items() if v}
    return {
        "ts":      dump["ts"],
        "grouped": grouped,
        "delta":   dump["delta"].get("market", {}),
        "summary": {
            "total_entries":      len(entries),
            "unique_ingredients": len(grouped),
            "my_entries":         sum(1 for e in entries if e.get("is_mine")),
        },
    }

@app.get("/api/market/{ingredient:path}")
def get_market_ingredient(ingredient: str):
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    entries = dump["data"].get("market") or []
    grouped = group_market_by_ingredient(entries)
    matches = grouped.get(ingredient)
    if matches is None:
        ingredient_lower = ingredient.lower()
        for key, val in grouped.items():
            if ingredient_lower in key.lower():
                matches = val
                break
    if matches is None:
        raise HTTPException(404, f"Ingredient '{ingredient}' not found")
    return {
        "ts":              dump["ts"],
        "ingredient":      ingredient,
        "entries":         matches,
        "best_unit_price": min(e["unit_price"] for e in matches),
        "total_quantity":  sum(e["quantity"] for e in matches),
    }

# ── DUMP FIELD RESOLVER ───────────────────────────────────────────────────────

def resolve_path(dump: dict, path: str):
    """
    Walk a dot-separated path through a dump, with smart shortcuts.

    Shortcuts (no leading 'data.'):
        restaurant          → dump.data.restaurant
        restaurant.balance  → dump.data.restaurant.balance
        market              → dump.data.market
        market.ingredients  → unique ingredient names across all market entries
        recipes             → dump.data.recipes
        recipes.names       → list of recipe names
        recipes.<name>      → single recipe by name (case-insensitive)
        restaurants         → dump.data.restaurants
        delta               → dump.delta
        delta.market        → dump.delta.market
        delta.restaurant    → dump.delta.restaurant
        <any.dot.path>      → generic traversal through the dump dict

    Examples:
        restaurant.inventory
        restaurant.balance
        market.ingredients
        recipes.names
        recipes.Nebulosa Galattica
        delta.market._summary
        data.restaurants.0.name      ← numeric index for arrays
    """
    # ── Shortcuts ──
    if path == "market.ingredients":
        entries = dump.get("data", {}).get("market") or []
        names = sorted({
            (e.get("ingredient") or {}).get("name") or f"Ingrediente #{e.get('ingredientId')}"
            for e in entries
        })
        return names

    if path == "recipes.names":
        recipes = dump.get("data", {}).get("recipes") or []
        return [r.get("name") for r in recipes if r.get("name")]

    if path.startswith("recipes."):
        name_query = path[len("recipes."):].lower()
        recipes = dump.get("data", {}).get("recipes") or []
        match = next((r for r in recipes if r.get("name", "").lower() == name_query), None)
        if match is None:
            # partial match
            match = next((r for r in recipes if name_query in r.get("name", "").lower()), None)
        return match

    if path.startswith("restaurants."):
        # restaurants.<id> or restaurants.<name fragment>
        key = path[len("restaurants."):]
        rs = dump.get("data", {}).get("restaurants") or []
        # try by id
        by_id = next((r for r in rs if str(r.get("id")) == key), None)
        if by_id:
            return by_id
        # try by name (partial, case-insensitive)
        return next((r for r in rs if key.lower() in r.get("name", "").lower()), None)

    # ── Generic dot-path traversal ──
    # Prepend 'data.' if path starts with a known top-level data key
    DATA_KEYS = {"restaurant", "menu", "market", "recipes", "restaurants", "_errors"}
    top = path.split(".")[0]
    if top in DATA_KEYS:
        path = "data." + path
    elif top == "delta":
        pass  # already correct
    # else: assume caller knows the full path from dump root

    node = dump
    for part in path.split("."):
        if node is None:
            return None
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                # treat as filter: return items where any string value matches
                node = [item for item in node if part.lower() in json.dumps(item).lower()]
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def load_dump_file(filename: str) -> dict:
    """Load a dump by filename from disk."""
    if not filename.endswith(".json"):
        filename += ".json"
    fpath = os.path.join(CONFIG["dumps_dir"], os.path.basename(filename))
    if not os.path.exists(fpath):
        raise HTTPException(404, f"Dump file not found: {filename}")
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


def wrap_field(dump: dict, field: str | None):
    """Extract a field from a dump and wrap it with metadata."""
    if not field:
        return dump
    value = resolve_path(dump, field)
    if value is None:
        raise HTTPException(404, f"Field '{field}' not found in dump. "
            f"Try: restaurant, restaurant.balance, restaurant.inventory, "
            f"market, market.ingredients, recipes, recipes.names, "
            f"restaurants, delta, delta.market._summary")
    return {"ts": dump["ts"], "field": field, "value": value}


# ── DUMP ROUTES ───────────────────────────────────────────────────────────────

@app.get("/api/dump/history")
def dump_history():
    with dumps_lock:
        history = [{"ts": d["ts"], "delta_summary": d["delta"].get("market", {}).get("_summary")} for d in dumps]
    return {"count": len(history), "dumps": history}

@app.get("/api/dump/latest")
def dump_latest(field: str = None):
    """
    Get the latest dump, optionally extracting a specific field.

    Examples:
    - /api/dump/latest
    - /api/dump/latest?field=restaurant.inventory
    - /api/dump/latest?field=market.ingredients
    - /api/dump/latest?field=recipes.names
    - /api/dump/latest?field=delta.market._summary
    - /api/dump/latest?field=restaurant.balance
    """
    dump = latest_dump()
    if not dump:
        raise HTTPException(503, "No data yet")
    return wrap_field(dump, field)

@app.get("/api/dump/previous")
def dump_previous(field: str = None):
    """Get the previous dump, optionally extracting a specific field."""
    dump = previous_dump()
    if not dump:
        raise HTTPException(404, "No previous dump yet")
    return wrap_field(dump, field)

@app.get("/api/dump/files")
def dump_files():
    files = list_dump_files()
    return {"count": len(files), "dumps_dir": CONFIG["dumps_dir"], "files": files}

@app.get("/api/dump/file/{filename:path}")
def dump_file(filename: str, field: str = None):
    """
    Get a dump file by name, optionally extracting a specific field.

    Examples:
    - /api/dump/file/2026-02-28T14-30-00Z.json
    - /api/dump/file/2026-02-28T14-30-00Z.json?field=market.ingredients
    - /api/dump/file/2026-02-28T14-30-00Z.json?field=recipes.Nebulosa Galattica
    """
    dump = load_dump_file(filename)
    return wrap_field(dump, field)

@app.delete("/api/dump/file/{filename:path}")
def delete_dump_file(filename: str):
    if not filename.endswith(".json"):
        filename += ".json"
    fpath = os.path.join(CONFIG["dumps_dir"], os.path.basename(filename))
    if not os.path.exists(fpath):
        raise HTTPException(404, f"Dump file not found: {filename}")
    os.remove(fpath)
    return {"ok": True, "deleted": filename}

@app.get("/api/dump/at/{timestamp:path}")
def dump_at(timestamp: str, field: str = None):
    """
    Get a dump by ISO timestamp prefix, optionally extracting a field.

    Examples:
    - /api/dump/at/2026-02-28T14          ← first dump of that hour
    - /api/dump/at/2026-02-28T14?field=restaurant.balance
    """
    files = list_dump_files()
    safe = timestamp.replace(":", "-").replace(".", "-")
    match = next((f for f in files if f.startswith(safe)), None)
    if not match:
        raise HTTPException(404, f"No dump found for timestamp prefix '{timestamp}'")
    dump = load_dump_file(match)
    return wrap_field(dump, field)

@app.get("/api/dump/compare")
def dump_compare(field: str, ts1: str = None, ts2: str = None):
    """
    Compare a specific field across two dumps.
    Defaults to latest vs previous if ts1/ts2 not provided.

    Examples:
    - /api/dump/compare?field=restaurant.balance
    - /api/dump/compare?field=market.ingredients&ts1=2026-02-28T14&ts2=2026-02-28T15
    - /api/dump/compare?field=restaurants
    """
    if ts1 and ts2:
        files = list_dump_files()
        def find(ts):
            safe = ts.replace(":", "-").replace(".", "-")
            m = next((f for f in files if f.startswith(safe)), None)
            if not m:
                raise HTTPException(404, f"No dump found for '{ts}'")
            return load_dump_file(m)
        d1, d2 = find(ts1), find(ts2)
    else:
        d1, d2 = previous_dump(), latest_dump()
        if not d1 or not d2:
            raise HTTPException(404, "Need at least two dumps for comparison")

    v1 = resolve_path(d1, field)
    v2 = resolve_path(d2, field)

    return {
        "field": field,
        "from":  {"ts": d1["ts"], "value": v1},
        "to":    {"ts": d2["ts"], "value": v2},
        "changed": v1 != v2,
    }

@app.get("/api/dump/timeseries")
def dump_timeseries(field: str, limit: int = 50):
    """
    Extract a field's value across all dumps in chronological order.
    Useful for tracking how a value changes over time.

    Examples:
    - /api/dump/timeseries?field=restaurant.balance
    - /api/dump/timeseries?field=restaurant.reputation&limit=20
    - /api/dump/timeseries?field=market.ingredients
    """
    files = list_dump_files()
    if not files:
        raise HTTPException(404, "No dumps on disk yet")

    series = []
    for fname in files[-limit:]:
        try:
            with open(os.path.join(CONFIG["dumps_dir"], fname), "r", encoding="utf-8") as f:
                dump = json.load(f)
            value = resolve_path(dump, field)
            series.append({"ts": dump["ts"], "value": value})
        except Exception as e:
            series.append({"ts": fname, "error": str(e)})

    return {
        "field":  field,
        "count":  len(series),
        "series": series,
    }


# ── HISTORY ROUTES ────────────────────────────────────────────────────────────
#
# These routes scan all persisted dump files and extract a specific entity's
# data across time, returning a chronological series with embedded deltas.
#
# Add to the bottom of server.py


def iter_dumps(limit: int = 200) -> list[dict]:
    """Load up to `limit` most recent dump files from disk, sorted oldest→newest."""
    files = list_dump_files()[-limit:]
    result = []
    for fname in files:
        try:
            with open(os.path.join(CONFIG["dumps_dir"], fname), "r", encoding="utf-8") as f:
                result.append(json.load(f))
        except Exception as e:
            print(f"[history] skipped {fname}: {e}")
    return result


@app.get("/api/history/restaurant")
def history_restaurant(limit: int = 100):
    """
    Full history of MY restaurant stats across all dumps.

    Returns a chronological series of snapshots with delta vs previous entry.

    Example:
        GET /api/history/restaurant
        GET /api/history/restaurant?limit=20
    """
    all_dumps = iter_dumps(limit)
    if not all_dumps:
        raise HTTPException(404, "No dumps found on disk")

    series = []
    prev = None
    for dump in all_dumps:
        r = (dump.get("data") or {}).get("restaurant")
        if r is None:
            continue
        entry = {
            "ts":         dump["ts"],
            "balance":    r.get("balance"),
            "reputation": r.get("reputation"),
            "isOpen":     r.get("isOpen"),
            "inventory":  r.get("inventory", {}),
            "menu_items": [i["name"] for i in (r.get("menu") or {}).get("items", [])],
            "kitchen":    r.get("kitchen", []),
            "delta":      None,
        }
        if prev is not None:
            delta = {}
            for field in ("balance", "reputation", "isOpen"):
                d = scalar_delta(prev.get(field), entry.get(field))
                if d and d.get("changed"):
                    delta[field] = d
            # inventory diff
            inv_delta = {}
            for k in set(list(prev["inventory"].keys()) + list(entry["inventory"].keys())):
                if prev["inventory"].get(k) != entry["inventory"].get(k):
                    inv_delta[k] = {"prev": prev["inventory"].get(k), "curr": entry["inventory"].get(k)}
            if inv_delta:
                delta["inventory"] = inv_delta
            entry["delta"] = delta or None
        series.append(entry)
        prev = entry

    return {
        "restaurant_id": CONFIG["my_restaurant_id"],
        "count":         len(series),
        "series":        series,
    }


@app.get("/api/history/restaurant/{restaurant_id}")
def history_restaurant_by_id(restaurant_id: str, limit: int = 100):
    """
    History of any restaurant's stats across all dumps, looked up by ID.

    Returns balance, reputation, isOpen, inventory, menu per dump with deltas.

    Example:
        GET /api/history/restaurant/7
        GET /api/history/restaurant/7?limit=30
    """
    all_dumps = iter_dumps(limit)
    if not all_dumps:
        raise HTTPException(404, "No dumps found on disk")

    series = []
    prev = None
    name = None
    for dump in all_dumps:
        rs = (dump.get("data") or {}).get("restaurants") or []
        r = next((x for x in rs if str(x.get("id")) == str(restaurant_id)), None)
        if r is None:
            continue
        if name is None:
            name = r.get("name")
        entry = {
            "ts":         dump["ts"],
            "balance":    r.get("balance"),
            "reputation": r.get("reputation"),
            "isOpen":     r.get("isOpen"),
            "inventory":  r.get("inventory", {}),
            "menu_items": [i["name"] for i in (r.get("menu") or {}).get("items", [])],
            "delta":      None,
        }
        if prev is not None:
            delta = {}
            for field in ("balance", "reputation", "isOpen"):
                d = scalar_delta(prev.get(field), entry.get(field))
                if d and d.get("changed"):
                    delta[field] = d
            inv_delta = {}
            for k in set(list(prev["inventory"].keys()) + list(entry["inventory"].keys())):
                if prev["inventory"].get(k) != entry["inventory"].get(k):
                    inv_delta[k] = {"prev": prev["inventory"].get(k), "curr": entry["inventory"].get(k)}
            if inv_delta:
                delta["inventory"] = inv_delta
            entry["delta"] = delta or None
        series.append(entry)
        prev = entry

    if not series:
        raise HTTPException(404, f"Restaurant '{restaurant_id}' not found in any dump")

    return {
        "restaurant_id": restaurant_id,
        "name":          name,
        "count":         len(series),
        "series":        series,
    }


@app.get("/api/history/ingredient/{ingredient_name:path}")
def history_ingredient(ingredient_name: str, limit: int = 100, side: str = None):
    """
    History of a market ingredient across all dumps.

    Shows all market entries for this ingredient per dump, with pricing trends,
    volume, and status changes. Supports partial/case-insensitive name matching.

    Query params:
        limit  — how many dumps to scan (default 100)
        side   — filter by BUY or SELL

    Example:
        GET /api/history/ingredient/Alghe Bioluminescenti
        GET /api/history/ingredient/alghe          <- partial match
        GET /api/history/ingredient/alghe?side=BUY

    For the complete flat list of every individual entry ever seen,
    use: GET /api/history/ingredient/{name}/entries
    """
    all_dumps = iter_dumps(limit)
    if not all_dumps:
        raise HTTPException(404, "No dumps found on disk")

    side_filter = side.upper() if side else None
    resolved_name = None
    series = []

    for dump in all_dumps:
        market = (dump.get("data") or {}).get("market") or []
        # Resolve canonical name on first match
        if resolved_name is None:
            for e in market:
                candidate = (e.get("ingredient") or {}).get("name") or ""
                if candidate.lower() == ingredient_name.lower() or ingredient_name.lower() in candidate.lower():
                    resolved_name = candidate
                    break

        if resolved_name is None:
            continue

        entries = [
            e for e in market
            if ((e.get("ingredient") or {}).get("name") or "") == resolved_name
            and (side_filter is None or e.get("side") == side_filter)
        ]

        if not entries:
            # Still record the gap so the caller can see when it disappeared
            series.append({
                "ts":               dump["ts"],
                "entries":          [],
                "total_entries":    0,
                "buy_count":        0,
                "sell_count":       0,
                "min_unit_price":   None,
                "max_unit_price":   None,
                "avg_unit_price":   None,
                "total_volume":     0,
                "statuses":         {},
            })
            continue

        unit_prices = [e.get("unit_price") or (e["totalPrice"] / max(e["quantity"], 1)) for e in entries]
        statuses: dict[str, int] = {}
        for e in entries:
            s = e.get("status", "unknown")
            statuses[s] = statuses.get(s, 0) + 1

        series.append({
            "ts":             dump["ts"],
            "entries":        entries,
            "total_entries":  len(entries),
            "buy_count":      sum(1 for e in entries if e.get("side") == "BUY"),
            "sell_count":     sum(1 for e in entries if e.get("side") == "SELL"),
            "min_unit_price": round(min(unit_prices), 4),
            "max_unit_price": round(max(unit_prices), 4),
            "avg_unit_price": round(sum(unit_prices) / len(unit_prices), 4),
            "total_volume":   sum(e.get("quantity", 0) for e in entries),
            "statuses":       statuses,
        })

    if resolved_name is None:
        raise HTTPException(404, f"Ingredient '{ingredient_name}' not found in any dump")

    # Compute price deltas across series
    prev_avg = None
    for point in series:
        if point["avg_unit_price"] is not None:
            point["price_delta"] = round(point["avg_unit_price"] - prev_avg, 4) if prev_avg is not None else None
            prev_avg = point["avg_unit_price"]
        else:
            point["price_delta"] = None

    non_empty = [p for p in series if p["total_entries"] > 0]
    all_prices = [p["avg_unit_price"] for p in non_empty if p["avg_unit_price"] is not None]

    return {
        "ingredient":          resolved_name,
        "side_filter":         side_filter,
        "count":               len(series),
        "summary": {
            "appearances":     len(non_empty),
            "overall_min_price": round(min(all_prices), 4) if all_prices else None,
            "overall_max_price": round(max(all_prices), 4) if all_prices else None,
            "overall_avg_price": round(sum(all_prices) / len(all_prices), 4) if all_prices else None,
            "price_trend":     (
                "rising"  if len(all_prices) >= 2 and all_prices[-1] > all_prices[0] else
                "falling" if len(all_prices) >= 2 and all_prices[-1] < all_prices[0] else
                "stable"
            ),
        },
        "series": series,
    }


def _resolve_ingredient_entries(ingredient_name: str, limit: int, side: str | None) -> tuple[str, list[dict]]:
    """
    Shared logic: scan all dumps, resolve canonical ingredient name,
    collect every individual market entry ever seen for it.
    Returns (resolved_name, flat_entries_with_metadata).
    """
    all_dumps = iter_dumps(limit)
    if not all_dumps:
        raise HTTPException(404, "No dumps found on disk")

    side_filter = side.upper() if side else None
    resolved_name = None

    # entry_id → { entry_data, first_seen, last_seen, snapshots: [{ts, status}] }
    seen: dict[int, dict] = {}

    for dump in all_dumps:
        market = (dump.get("data") or {}).get("market") or []

        # resolve canonical name once
        if resolved_name is None:
            for e in market:
                candidate = (e.get("ingredient") or {}).get("name") or ""
                if candidate.lower() == ingredient_name.lower() or ingredient_name.lower() in candidate.lower():
                    resolved_name = candidate
                    break

        if resolved_name is None:
            continue

        for e in market:
            if (e.get("ingredient") or {}).get("name") != resolved_name:
                continue
            if side_filter and e.get("side") != side_filter:
                continue

            eid = e["id"]
            if eid not in seen:
                seen[eid] = {
                    **e,
                    "first_seen": dump["ts"],
                    "last_seen":  dump["ts"],
                    "status_history": [{"ts": dump["ts"], "status": e.get("status")}],
                }
            else:
                prev_status = seen[eid]["status_history"][-1]["status"]
                seen[eid]["last_seen"] = dump["ts"]
                if e.get("status") != prev_status:
                    seen[eid]["status_history"].append({"ts": dump["ts"], "status": e.get("status")})
                # update to latest state
                seen[eid]["status"] = e.get("status")

    if resolved_name is None:
        raise HTTPException(404, f"Ingredient '{ingredient_name}' not found in any dump")

    entries = sorted(seen.values(), key=lambda e: e["id"])
    return resolved_name, entries


@app.get("/api/history/ingredient-entries/{ingredient_name:path}")
def history_ingredient_entries(
    ingredient_name: str,
    limit: int = 100,
    side: str = None,
    status: str = None,
    restaurant_id: str = None,
):
    """
    Complete history of every individual market entry ever seen for an ingredient.

    Each entry in the response is a unique market order, enriched with:
    - first_seen / last_seen timestamps
    - status_history: list of status changes over time
    - unit_price, restaurant_name, is_mine (from server enrichment)

    Query params:
        limit         — dumps to scan (default 100)
        side          — filter by BUY or SELL
        status        — filter by final status: open, cancelled, filled
        restaurant_id — filter by creator restaurant ID

    Examples:
        GET /api/history/ingredient-entries/Alghe Bioluminescenti
        GET /api/history/ingredient-entries/alghe?side=BUY
        GET /api/history/ingredient-entries/alghe?status=open
        GET /api/history/ingredient-entries/alghe?restaurant_id=7
    """
    resolved_name, entries = _resolve_ingredient_entries(ingredient_name, limit, side)

    # optional filters on final state
    if status:
        entries = [e for e in entries if e.get("status") == status.lower()]
    if restaurant_id:
        entries = [e for e in entries if str(e.get("createdByRestaurantId")) == str(restaurant_id)]

    unit_prices = [e.get("unit_price") or (e["totalPrice"] / max(e["quantity"], 1)) for e in entries]

    return {
        "ingredient":  resolved_name,
        "side_filter": side.upper() if side else None,
        "total":       len(entries),
        "summary": {
            "min_unit_price": round(min(unit_prices), 4) if unit_prices else None,
            "max_unit_price": round(max(unit_prices), 4) if unit_prices else None,
            "avg_unit_price": round(sum(unit_prices) / len(unit_prices), 4) if unit_prices else None,
            "total_volume":   sum(e.get("quantity", 0) for e in entries),
            "by_status":      {s: sum(1 for e in entries if e.get("status") == s)
                               for s in {e.get("status") for e in entries}},
            "by_side":        {s: sum(1 for e in entries if e.get("side") == s)
                               for s in {e.get("side") for e in entries}},
            "by_restaurant":  {e.get("restaurant_name", f"#{e.get('createdByRestaurantId')}"):
                               sum(1 for x in entries if x.get("createdByRestaurantId") == e.get("createdByRestaurantId"))
                               for e in entries},
        },
        "entries": entries,
    }


@app.get("/api/history/ingredients")
def history_all_ingredients(limit: int = 50):
    """
    Summary of ALL ingredients that have appeared in the market across recent dumps.
    Shows price trend, total appearances, and last seen timestamp for each.

    Example:
        GET /api/history/ingredients
        GET /api/history/ingredients?limit=20
    """
    all_dumps = iter_dumps(limit)
    if not all_dumps:
        raise HTTPException(404, "No dumps found on disk")

    # ingredient_name → list of (ts, avg_unit_price, volume)
    ingredient_data: dict[str, list] = {}

    for dump in all_dumps:
        market = (dump.get("data") or {}).get("market") or []
        grouped: dict[str, list] = {}
        for e in market:
            name = (e.get("ingredient") or {}).get("name") or f"#{e.get('ingredientId')}"
            grouped.setdefault(name, []).append(e)

        for name, entries in grouped.items():
            prices = [e.get("unit_price") or (e["totalPrice"] / max(e["quantity"], 1)) for e in entries]
            ingredient_data.setdefault(name, []).append({
                "ts":        dump["ts"],
                "avg_price": round(sum(prices) / len(prices), 4),
                "volume":    sum(e.get("quantity", 0) for e in entries),
                "entries":   len(entries),
            })

    summary = []
    for name, points in sorted(ingredient_data.items()):
        prices = [p["avg_price"] for p in points]
        summary.append({
            "ingredient":    name,
            "appearances":   len(points),
            "last_seen":     points[-1]["ts"],
            "latest_price":  prices[-1],
            "min_price":     round(min(prices), 4),
            "max_price":     round(max(prices), 4),
            "avg_price":     round(sum(prices) / len(prices), 4),
            "price_trend": (
                "rising"  if len(prices) >= 2 and prices[-1] > prices[0] else
                "falling" if len(prices) >= 2 and prices[-1] < prices[0] else
                "stable"
            ),
        })

    return {
        "count":   len(summary),
        "dumps_scanned": len(all_dumps),
        "ingredients": summary,
    }
