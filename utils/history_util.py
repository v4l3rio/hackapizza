"""
Ristorante Dashboard — Utility functions
=========================================
Standalone functions to fetch and query game state.
No web server, no deltas, no inventory coverage.

Usage:
    from dashboard_utils import DashboardClient

    client = DashboardClient(base_url="https://...", api_key="...", my_restaurant_id="5")

    # Live fetches (hit upstream API)
    recipes = client.get_recipes()
    restaurant = client.get_restaurant()
    restaurants = client.get_restaurants()
    market = client.get_market()
    meals = client.get_meals(turn_id=3)
    bids = client.get_bid_history(turn_id=3)

    # Computed
    optimal = client.get_optimal_recipe_set(size=3)

    # Full dump (fetches everything, persists to disk)
    dump = client.run_dump(turn_id=3)

    # History (reads from persisted dumps on disk)
    history = client.get_dump_history()
    ts = client.get_timeseries("restaurant.balance")
"""

import httpx
import json
import os
import time
from datetime import datetime, timezone
from typing import Any


class DashboardClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        my_restaurant_id: str = "5",
        dumps_dir: str = "dumps",
        timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.my_restaurant_id = my_restaurant_id
        self.dumps_dir = dumps_dir
        self.timeout = timeout
        self._cached_recipes: list[dict] | None = None

        os.makedirs(self.dumps_dir, exist_ok=True)

    # ── UPSTREAM FETCH ────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> Any:
        """GET from upstream API."""
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.base_url + path, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()

    # ── LIVE DATA FETCHES ─────────────────────────────────────────────────────

    def get_restaurant(self) -> dict | None:
        """Fetch my restaurant data."""
        try:
            return self._get(f"/restaurant/{self.my_restaurant_id}")
        except Exception as e:
            print(f"[fetch] restaurant failed: {e}")
            return None

    def get_menu(self) -> dict | None:
        """Fetch my restaurant's menu."""
        try:
            return self._get(f"/restaurant/{self.my_restaurant_id}/menu")
        except Exception as e:
            print(f"[fetch] menu failed: {e}")
            return None

    def get_restaurants(self) -> list[dict] | None:
        """Fetch all restaurants."""
        try:
            return self._get("/restaurants")
        except Exception as e:
            print(f"[fetch] restaurants failed: {e}")
            return None

    def get_market(self) -> list[dict] | None:
        """Fetch market entries."""
        try:
            return self._get("/market/entries")
        except Exception as e:
            print(f"[fetch] market failed: {e}")
            return None

    def get_recipes(self) -> list[dict]:
        """Fetch recipes from upstream, with cache fallback."""
        try:
            recipes = self._get("/recipes")
            if recipes:
                self._cached_recipes = recipes
            return recipes
        except Exception as e:
            print(f"[fetch] recipes failed: {e}")
            if self._cached_recipes:
                print(f"[fetch] using cached recipes ({len(self._cached_recipes)})")
                return self._cached_recipes
            return []

    def get_meals(self, turn_id: int, restaurant_id: str = None) -> list[dict] | None:
        """Fetch meals for a turn. Defaults to my restaurant."""
        rid = restaurant_id or self.my_restaurant_id
        try:
            return self._get("/meals", {"turn_id": turn_id, "restaurant_id": rid})
        except Exception as e:
            print(f"[fetch] meals failed: {e}")
            return None

    def get_bid_history(self, turn_id: int) -> list[dict] | None:
        """Fetch bid history for a turn."""
        try:
            return self._get("/bid_history", {"turn_id": turn_id})
        except Exception as e:
            print(f"[fetch] bid_history failed: {e}")
            return None

    # ── MARKET HELPERS ────────────────────────────────────────────────────────

    @staticmethod
    def enrich_market_entries(entries: list, restaurants: list) -> list:
        """Add restaurant_name, unit_price, is_mine to market entries."""
        r_map = {r["id"]: r["name"] for r in (restaurants or [])}
        for e in entries:
            rid = str(e.get("createdByRestaurantId", ""))
            e["restaurant_name"] = r_map.get(rid, f"Ristorante #{rid}")
            e["unit_price"] = round(e["totalPrice"] / max(e["quantity"], 1), 4)
        return entries

    @staticmethod
    def group_market_by_ingredient(entries: list) -> dict[str, list]:
        """Group market entries by ingredient name, sorted by unit price."""
        groups: dict[str, list] = {}
        for e in entries:
            name = (e.get("ingredient") or {}).get("name") or f"Ingrediente #{e.get('ingredientId')}"
            groups.setdefault(name, []).append(e)
        for name in groups:
            groups[name].sort(key=lambda e: e["totalPrice"] / max(e["quantity"], 1))
        return dict(sorted(groups.items()))

    # ── OPTIMAL RECIPE SET ────────────────────────────────────────────────────

    def get_optimal_recipe_set(self, size: int = 3) -> dict:
        """
        Find the subset of `size` recipes sharing the most common ingredients.

        Greedy algorithm: O(n²) — starts with best pair, adds recipe introducing
        fewest new ingredients at each step.

        Returns dict with: recipes, shared_ingredients, exclusive_ingredients,
        all_ingredients, summary.
        """
        recipes = self.get_recipes()
        if not recipes:
            return {"error": "No recipes available", "recipes": [], "summary": {}}

        size = max(1, min(size, len(recipes)))

        ing_sets: list[set[str]] = [
            set((r.get("ingredients") or {}).keys()) for r in recipes
        ]
        n = len(recipes)

        if size >= n:
            selected_indices = list(range(n))
        elif size == 1:
            selected_indices = [min(range(n), key=lambda i: len(ing_sets[i]))]
        else:
            # Best starting pair (max intersection)
            best_pair = (0, 1)
            best_overlap = -1
            for i in range(n):
                for j in range(i + 1, n):
                    overlap = len(ing_sets[i] & ing_sets[j])
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_pair = (i, j)

            selected_indices = [best_pair[0], best_pair[1]]
            current_ingredients = ing_sets[best_pair[0]] | ing_sets[best_pair[1]]

            remaining = set(range(n)) - set(selected_indices)
            while len(selected_indices) < size and remaining:
                best_idx = None
                best_new_count = float("inf")
                best_overlap_count = -1

                for idx in remaining:
                    new_count = len(ing_sets[idx] - current_ingredients)
                    overlap_count = len(ing_sets[idx] & current_ingredients)
                    if (new_count < best_new_count or
                            (new_count == best_new_count and overlap_count > best_overlap_count)):
                        best_new_count = new_count
                        best_overlap_count = overlap_count
                        best_idx = idx

                selected_indices.append(best_idx)
                current_ingredients |= ing_sets[best_idx]
                remaining.discard(best_idx)

        selected = [recipes[i] for i in selected_indices]

        # Analyze
        all_ingredients: dict[str, dict] = {}
        for r in selected:
            for ing_name, qty in (r.get("ingredients") or {}).items():
                if ing_name not in all_ingredients:
                    all_ingredients[ing_name] = {"ingredient": ing_name, "total_quantity": 0, "used_by": []}
                all_ingredients[ing_name]["total_quantity"] += qty
                all_ingredients[ing_name]["used_by"].append(r.get("name", ""))

        shared = {k: v for k, v in all_ingredients.items() if len(v["used_by"]) >= 2}
        exclusive = {k: v for k, v in all_ingredients.items() if len(v["used_by"]) == 1}

        return {
            "size": size,
            "recipes": selected,
            "shared_ingredients": shared,
            "exclusive_ingredients": exclusive,
            "all_ingredients": all_ingredients,
            "summary": {
                "unique_ingredient_count": len(all_ingredients),
                "shared_ingredient_count": len(shared),
                "exclusive_ingredient_count": len(exclusive),
                "total_ingredient_quantity": sum(v["total_quantity"] for v in all_ingredients.values()),
                "total_recipes_available": len(recipes),
            },
        }

    # ── DUMP: FETCH ALL & PERSIST ─────────────────────────────────────────────

    def fetch_all(self, turn_id: int | None = None) -> dict:
        """Fetch all game state from upstream."""
        data: dict[str, Any] = {}
        data["restaurant"] = self.get_restaurant()
        data["menu"] = self.get_menu()
        data["restaurants"] = self.get_restaurants()
        data["recipes"] = self.get_recipes()

        market = self.get_market()
        if market and data["restaurants"]:
            market = self.enrich_market_entries(market, data["restaurants"])
        data["market"] = market

        if turn_id is not None:
            data["turn_id"] = turn_id
            data["meals"] = self.get_meals(turn_id)
            data["bid_history"] = self.get_bid_history(turn_id)
        else:
            data["turn_id"] = None
            data["meals"] = None
            data["bid_history"] = None

        return data

    def run_dump(self, turn_id: int | None = None) -> dict:
        """Fetch everything and persist to disk as {turn_id}.json. Updates if exists."""
        ts = datetime.now(timezone.utc).isoformat()
        data = self.fetch_all(turn_id)
        dump = {"ts": ts, "turn_id": turn_id, "data": data}
        self._persist_dump(dump, turn_id)
        print(f"[dump] turn={turn_id} ts={ts} — market:{len(data.get('market') or [])} "
              f"meals:{len(data.get('meals') or [])} "
              f"bids:{len(data.get('bid_history') or [])}")
        return dump

    def _persist_dump(self, dump: dict, turn_id: int | None = None):
        name = str(turn_id) if turn_id is not None else dump["ts"].replace(":", "-").replace(".", "-")
        fpath = os.path.join(self.dumps_dir, f"{name}.json")
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)

    # ── DUMP FILE OPERATIONS ──────────────────────────────────────────────────

    def list_dump_files(self) -> list[str]:
        return sorted(f for f in os.listdir(self.dumps_dir) if f.endswith(".json"))

    def load_dump(self, filename: str) -> dict:
        if not filename.endswith(".json"):
            filename += ".json"
        fpath = os.path.join(self.dumps_dir, filename)
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_dumps(self, limit: int = 200) -> list[dict]:
        """Load up to `limit` most recent dumps from disk."""
        files = self.list_dump_files()[-limit:]
        result = []
        for fname in files:
            try:
                result.append(self.load_dump(fname))
            except Exception as e:
                print(f"[load] skipped {fname}: {e}")
        return result

    def latest_dump(self) -> dict | None:
        files = self.list_dump_files()
        return self.load_dump(files[-1]) if files else None

    def delete_dump(self, filename: str):
        if not filename.endswith(".json"):
            filename += ".json"
        os.remove(os.path.join(self.dumps_dir, filename))

    # ── FIELD RESOLVER ────────────────────────────────────────────────────────

    @staticmethod
    def resolve_path(dump: dict, path: str) -> Any:
        """
        Walk a dot-separated path through a dump.

        Shortcuts:
            restaurant.balance  → dump.data.restaurant.balance
            market.ingredients  → unique ingredient names
            recipes.names       → list of recipe names
            meals, bid_history  → dump.data.*
        """
        if path == "market.ingredients":
            entries = dump.get("data", {}).get("market") or []
            return sorted({
                (e.get("ingredient") or {}).get("name") or f"Ingrediente #{e.get('ingredientId')}"
                for e in entries
            })

        if path == "recipes.names":
            recipes = dump.get("data", {}).get("recipes") or []
            return [r.get("name") for r in recipes if r.get("name")]

        if path.startswith("recipes."):
            name_query = path[len("recipes."):].lower()
            recipes = dump.get("data", {}).get("recipes") or []
            match = next((r for r in recipes if r.get("name", "").lower() == name_query), None)
            if match is None:
                match = next((r for r in recipes if name_query in r.get("name", "").lower()), None)
            return match

        if path.startswith("restaurants."):
            key = path[len("restaurants."):]
            rs = dump.get("data", {}).get("restaurants") or []
            by_id = next((r for r in rs if str(r.get("id")) == key), None)
            if by_id:
                return by_id
            return next((r for r in rs if key.lower() in r.get("name", "").lower()), None)

        DATA_KEYS = {"restaurant", "menu", "market", "recipes", "restaurants", "meals", "bid_history", "_errors"}
        top = path.split(".")[0]
        if top in DATA_KEYS:
            path = "data." + path

        node = dump
        for part in path.split("."):
            if node is None:
                return None
            if isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    node = [item for item in node if part.lower() in json.dumps(item).lower()]
            elif isinstance(node, dict):
                node = node.get(part)
            else:
                return None
        return node

    # ── TIMESERIES ────────────────────────────────────────────────────────────

    def get_timeseries(self, field: str, limit: int = 50) -> list[dict]:
        """Extract a field across all dumps chronologically."""
        series = []
        for dump in self.load_dumps(limit):
            value = self.resolve_path(dump, field)
            series.append({"ts": dump["ts"], "value": value})
        return series

    # ── HISTORY: RESTAURANT ───────────────────────────────────────────────────

    def history_restaurant(self, restaurant_id: str = None, limit: int = 100) -> list[dict]:
        """
        History of a restaurant's stats across dumps.
        Defaults to my restaurant.
        """
        rid = restaurant_id or self.my_restaurant_id
        is_mine = (rid == self.my_restaurant_id)
        all_dumps = self.load_dumps(limit)

        series = []
        for dump in all_dumps:
            if is_mine:
                r = (dump.get("data") or {}).get("restaurant")
            else:
                rs = (dump.get("data") or {}).get("restaurants") or []
                r = next((x for x in rs if str(x.get("id")) == str(rid)), None)
            if r is None:
                continue
            series.append({
                "ts":         dump["ts"],
                "balance":    r.get("balance"),
                "reputation": r.get("reputation"),
                "isOpen":     r.get("isOpen"),
                "inventory":  r.get("inventory", {}),
                "menu_items": [i["name"] for i in (r.get("menu") or {}).get("items", [])],
            })
        return series

    # ── HISTORY: INGREDIENT ───────────────────────────────────────────────────

    def history_ingredient(self, ingredient_name: str, limit: int = 100, side: str = None) -> dict:
        """
        Per-dump aggregated stats for a market ingredient.
        Returns {ingredient, series: [...], summary}.
        """
        all_dumps = self.load_dumps(limit)
        side_filter = side.upper() if side else None
        resolved_name = None
        series = []

        for dump in all_dumps:
            market = (dump.get("data") or {}).get("market") or []
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
                series.append({"ts": dump["ts"], "total_entries": 0, "avg_unit_price": None})
                continue

            unit_prices = [e.get("unit_price") or (e["totalPrice"] / max(e["quantity"], 1)) for e in entries]
            series.append({
                "ts":             dump["ts"],
                "total_entries":  len(entries),
                "buy_count":      sum(1 for e in entries if e.get("side") == "BUY"),
                "sell_count":     sum(1 for e in entries if e.get("side") == "SELL"),
                "min_unit_price": round(min(unit_prices), 4),
                "max_unit_price": round(max(unit_prices), 4),
                "avg_unit_price": round(sum(unit_prices) / len(unit_prices), 4),
                "total_volume":   sum(e.get("quantity", 0) for e in entries),
            })

        all_prices = [p["avg_unit_price"] for p in series if p.get("avg_unit_price") is not None]
        return {
            "ingredient": resolved_name,
            "count": len(series),
            "summary": {
                "overall_min_price": round(min(all_prices), 4) if all_prices else None,
                "overall_max_price": round(max(all_prices), 4) if all_prices else None,
                "overall_avg_price": round(sum(all_prices) / len(all_prices), 4) if all_prices else None,
            },
            "series": series,
        }

    def history_ingredient_entries(self, ingredient_name: str, limit: int = 100, side: str = None) -> dict:
        """
        Deduplicated list of every market entry ever seen for an ingredient,
        with first_seen, last_seen, status_history.
        """
        all_dumps = self.load_dumps(limit)
        side_filter = side.upper() if side else None
        resolved_name = None
        seen: dict[int, dict] = {}

        for dump in all_dumps:
            market = (dump.get("data") or {}).get("market") or []
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
                    seen[eid] = {**e, "first_seen": dump["ts"], "last_seen": dump["ts"],
                                 "status_history": [{"ts": dump["ts"], "status": e.get("status")}]}
                else:
                    prev_status = seen[eid]["status_history"][-1]["status"]
                    seen[eid]["last_seen"] = dump["ts"]
                    if e.get("status") != prev_status:
                        seen[eid]["status_history"].append({"ts": dump["ts"], "status": e.get("status")})
                    seen[eid]["status"] = e.get("status")

        entries = sorted(seen.values(), key=lambda e: e["id"])
        return {"ingredient": resolved_name, "total": len(entries), "entries": entries}

    # ── HISTORY: DISHES ───────────────────────────────────────────────────────

    def history_dishes(self, limit: int = 200, restaurant_id: str = None) -> dict[str, list]:
        """Price history for every dish across restaurants. Returns {dish_name: [observations]}."""
        all_dumps = self.load_dumps(limit)
        dishes: dict[str, list[dict]] = {}

        for dump in all_dumps:
            ts = dump["ts"]
            for r in ((dump.get("data") or {}).get("restaurants") or []):
                rid = str(r.get("id", ""))
                if restaurant_id and rid != str(restaurant_id):
                    continue
                rname = r.get("name", f"Ristorante #{rid}")
                for item in (r.get("menu") or {}).get("items") or []:
                    name, price = item.get("name"), item.get("price")
                    if name and price is not None:
                        dishes.setdefault(name, []).append({
                            "ts": ts, "restaurant_id": rid, "restaurant_name": rname, "price": price,
                        })
        return dishes

    # ── HISTORY: MEALS ────────────────────────────────────────────────────────

    def history_meals_entries(self, limit: int = 200) -> list[dict]:
        """Deduplicated meal entries with first_seen, last_seen, status_history."""
        seen: dict[int, dict] = {}
        for dump in self.load_dumps(limit):
            for m in ((dump.get("data") or {}).get("meals") or []):
                mid = m["id"]
                if mid not in seen:
                    seen[mid] = {**m, "first_seen": dump["ts"], "last_seen": dump["ts"],
                                 "status_history": [{"ts": dump["ts"], "status": m.get("status")}]}
                else:
                    prev_status = seen[mid]["status_history"][-1]["status"]
                    seen[mid]["last_seen"] = dump["ts"]
                    if m.get("status") != prev_status:
                        seen[mid]["status_history"].append({"ts": dump["ts"], "status": m.get("status")})
                    seen[mid]["status"] = m.get("status")
                    seen[mid]["servedDishId"] = m.get("servedDishId")
                    seen[mid]["executed"] = m.get("executed")
        return sorted(seen.values(), key=lambda e: e["id"])

    # ── HISTORY: BIDS ─────────────────────────────────────────────────────────

    def history_bids_entries(self, limit: int = 200) -> list[dict]:
        """Deduplicated bid entries with first_seen, last_seen, status_history."""
        seen: dict[int, dict] = {}
        for dump in self.load_dumps(limit):
            for b in ((dump.get("data") or {}).get("bid_history") or []):
                bid = b["id"]
                if bid not in seen:
                    seen[bid] = {**b, "first_seen": dump["ts"], "last_seen": dump["ts"],
                                 "status_history": [{"ts": dump["ts"], "status": b.get("status")}]}
                else:
                    prev_status = seen[bid]["status_history"][-1]["status"]
                    seen[bid]["last_seen"] = dump["ts"]
                    if b.get("status") != prev_status:
                        seen[bid]["status_history"].append({"ts": dump["ts"], "status": b.get("status")})
                    seen[bid]["status"] = b.get("status")
        return sorted(seen.values(), key=lambda e: e["id"])

    # ── DELTA: LAST TWO DUMPS ─────────────────────────────────────────────

    def get_restaurant_delta(self) -> dict | None:
        """
        Compare reputation and balance between the last two dumps.

        Returns:
            {
                "prev": {"ts", "balance", "reputation"},
                "curr": {"ts", "balance", "reputation"},
                "delta": {"balance": diff, "reputation": diff},
            }
            or None if fewer than 2 dumps exist.
        """
        files = self.list_dump_files()
        if len(files) < 2:
            return None

        prev_dump = self.load_dump(files[-2])
        curr_dump = self.load_dump(files[-1])

        prev_r = (prev_dump.get("data") or {}).get("restaurant") or {}
        curr_r = (curr_dump.get("data") or {}).get("restaurant") or {}

        prev_bal = prev_r.get("balance", 0)
        curr_bal = curr_r.get("balance", 0)
        prev_rep = prev_r.get("reputation", 0)
        curr_rep = curr_r.get("reputation", 0)

        return {
            "prev": {"ts": prev_dump["ts"], "balance": prev_bal, "reputation": prev_rep},
            "curr": {"ts": curr_dump["ts"], "balance": curr_bal, "reputation": curr_rep},
            "delta": {
                "balance": curr_bal - prev_bal,
                "reputation": curr_rep - prev_rep,
            },
        }