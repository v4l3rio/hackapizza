"""
Ristorante Dashboard — Python API Client
=========================================
A clean, typed client for all dump and history routes.

Usage:
    from client import RistoranteClient

    client = RistoranteClient("http://localhost:8765")

    # Latest snapshot
    snap = client.status()
    print(snap.restaurant.balance)

    # Field access
    ingredients = client.dump_field("latest", "market.ingredients")
    balance_over_time = client.timeseries("restaurant.balance", limit=30)

    # History
    history = client.restaurant_history()
    for point in history.series:
        print(point.ts, point.balance, point.delta)

    ingredient = client.ingredient_history("Alghe Bioluminescenti", side="BUY")
    print(ingredient.summary.price_trend)
"""

from __future__ import annotations

import json
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Optional


# ── HTTP PRIMITIVES ───────────────────────────────────────────────────────────

class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}")


def _get(base_url: str, path: str, params: dict = None) -> Any:
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get("detail", body)
        except Exception:
            msg = body
        raise APIError(e.code, msg)


def _delete(base_url: str, path: str) -> Any:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method="DELETE", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise APIError(e.code, json.loads(body).get("detail", body))


def _post(base_url: str, path: str, payload: dict) -> Any:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise APIError(e.code, json.loads(body).get("detail", body))


# ── LIGHTWEIGHT DATA CLASSES ──────────────────────────────────────────────────
# These wrap raw dicts and provide attribute access + helpers.
# We avoid heavy dependencies — no pydantic, no dataclasses magic.

class _Wrap:
    """Wrap a dict for attribute-style access. Falls back to None for missing keys."""
    def __init__(self, data: dict | None):
        self._data = data or {}

    def __getattr__(self, name: str):
        try:
            val = self._data[name]
        except KeyError:
            return None
        if isinstance(val, dict):
            return _Wrap(val)
        if isinstance(val, list):
            return [_Wrap(i) if isinstance(i, dict) else i for i in val]
        return val

    def raw(self) -> dict:
        return self._data

    def __repr__(self):
        return f"{self.__class__.__name__}({self._data!r})"


@dataclass
class FieldResult:
    """Result of a field extraction from a dump."""
    ts: str
    field: str
    value: Any

    @classmethod
    def from_raw(cls, raw: dict, field: str) -> "FieldResult":
        return cls(ts=raw.get("ts", ""), field=field, value=raw.get("value"))


@dataclass
class TimeseriesPoint:
    ts: str
    value: Any


@dataclass
class Timeseries:
    field: str
    count: int
    series: list[TimeseriesPoint]

    def values(self) -> list:
        """Extract just the values in order."""
        return [p.value for p in self.series]

    def timestamps(self) -> list[str]:
        return [p.ts for p in self.series]

    def non_null(self) -> list[TimeseriesPoint]:
        return [p for p in self.series if p.value is not None]

    def as_pairs(self) -> list[tuple[str, Any]]:
        return [(p.ts, p.value) for p in self.series]

    @classmethod
    def from_raw(cls, raw: dict) -> "Timeseries":
        return cls(
            field=raw["field"],
            count=raw["count"],
            series=[TimeseriesPoint(ts=p["ts"], value=p["value"]) for p in raw["series"]],
        )


@dataclass
class CompareResult:
    field: str
    from_ts: str
    from_value: Any
    to_ts: str
    to_value: Any
    changed: bool

    @classmethod
    def from_raw(cls, raw: dict) -> "CompareResult":
        return cls(
            field=raw["field"],
            from_ts=raw["from"]["ts"],
            from_value=raw["from"]["value"],
            to_ts=raw["to"]["ts"],
            to_value=raw["to"]["value"],
            changed=raw["changed"],
        )


@dataclass
class DumpFile:
    filename: str

    @property
    def ts(self) -> str:
        return self.filename.replace(".json", "").replace("-", ":", 2)


@dataclass
class DumpFileList:
    count: int
    dumps_dir: str
    files: list[DumpFile]

    @classmethod
    def from_raw(cls, raw: dict) -> "DumpFileList":
        return cls(
            count=raw["count"],
            dumps_dir=raw["dumps_dir"],
            files=[DumpFile(f) for f in raw["files"]],
        )


@dataclass
class RestaurantHistoryPoint:
    ts: str
    balance: Optional[float]
    reputation: Optional[float]
    is_open: Optional[bool]
    inventory: dict
    menu_items: list[str]
    kitchen: list
    delta: Optional[dict]

    @classmethod
    def from_raw(cls, raw: dict) -> "RestaurantHistoryPoint":
        return cls(
            ts=raw["ts"],
            balance=raw.get("balance"),
            reputation=raw.get("reputation"),
            is_open=raw.get("isOpen"),
            inventory=raw.get("inventory", {}),
            menu_items=raw.get("menu_items", []),
            kitchen=raw.get("kitchen", []),
            delta=raw.get("delta"),
        )

    def balance_changed(self) -> bool:
        return bool(self.delta and "balance" in self.delta)

    def reputation_changed(self) -> bool:
        return bool(self.delta and "reputation" in self.delta)


@dataclass
class RestaurantHistory:
    restaurant_id: str
    name: Optional[str]
    count: int
    series: list[RestaurantHistoryPoint]

    def balances(self) -> list[Optional[float]]:
        return [p.balance for p in self.series]

    def reputations(self) -> list[Optional[float]]:
        return [p.reputation for p in self.series]

    def open_states(self) -> list[Optional[bool]]:
        return [p.is_open for p in self.series]

    def changes_only(self) -> list[RestaurantHistoryPoint]:
        """Return only points where something changed vs the previous dump."""
        return [p for p in self.series if p.delta]

    @classmethod
    def from_raw(cls, raw: dict) -> "RestaurantHistory":
        return cls(
            restaurant_id=str(raw["restaurant_id"]),
            name=raw.get("name"),
            count=raw["count"],
            series=[RestaurantHistoryPoint.from_raw(p) for p in raw["series"]],
        )


@dataclass
class IngredientHistoryPoint:
    ts: str
    total_entries: int
    buy_count: int
    sell_count: int
    min_unit_price: Optional[float]
    max_unit_price: Optional[float]
    avg_unit_price: Optional[float]
    total_volume: int
    statuses: dict[str, int]
    price_delta: Optional[float]
    entries: list[dict]

    @classmethod
    def from_raw(cls, raw: dict) -> "IngredientHistoryPoint":
        return cls(
            ts=raw["ts"],
            total_entries=raw.get("total_entries", 0),
            buy_count=raw.get("buy_count", 0),
            sell_count=raw.get("sell_count", 0),
            min_unit_price=raw.get("min_unit_price"),
            max_unit_price=raw.get("max_unit_price"),
            avg_unit_price=raw.get("avg_unit_price"),
            total_volume=raw.get("total_volume", 0),
            statuses=raw.get("statuses", {}),
            price_delta=raw.get("price_delta"),
            entries=raw.get("entries", []),
        )

    def is_empty(self) -> bool:
        return self.total_entries == 0

    def spread(self) -> Optional[float]:
        """Price spread (max - min) at this point in time."""
        if self.min_unit_price is not None and self.max_unit_price is not None:
            return round(self.max_unit_price - self.min_unit_price, 4)
        return None


@dataclass
class IngredientSummary:
    appearances: int
    overall_min_price: Optional[float]
    overall_max_price: Optional[float]
    overall_avg_price: Optional[float]
    price_trend: str  # "rising" | "falling" | "stable"

    @classmethod
    def from_raw(cls, raw: dict) -> "IngredientSummary":
        return cls(
            appearances=raw.get("appearances", 0),
            overall_min_price=raw.get("overall_min_price"),
            overall_max_price=raw.get("overall_max_price"),
            overall_avg_price=raw.get("overall_avg_price"),
            price_trend=raw.get("price_trend", "stable"),
        )


@dataclass
class IngredientHistory:
    ingredient: str
    side_filter: Optional[str]
    count: int
    summary: IngredientSummary
    series: list[IngredientHistoryPoint]

    def avg_prices(self) -> list[Optional[float]]:
        return [p.avg_unit_price for p in self.series]

    def non_empty(self) -> list[IngredientHistoryPoint]:
        return [p for p in self.series if not p.is_empty()]

    def price_deltas(self) -> list[Optional[float]]:
        return [p.price_delta for p in self.series]

    @classmethod
    def from_raw(cls, raw: dict) -> "IngredientHistory":
        return cls(
            ingredient=raw["ingredient"],
            side_filter=raw.get("side_filter"),
            count=raw["count"],
            summary=IngredientSummary.from_raw(raw["summary"]),
            series=[IngredientHistoryPoint.from_raw(p) for p in raw["series"]],
        )


@dataclass
class IngredientOverview:
    ingredient: str
    appearances: int
    last_seen: str
    latest_price: Optional[float]
    min_price: Optional[float]
    max_price: Optional[float]
    avg_price: Optional[float]
    price_trend: str


@dataclass
class AllIngredientsHistory:
    count: int
    dumps_scanned: int
    ingredients: list[IngredientOverview]

    def rising(self) -> list[IngredientOverview]:
        return [i for i in self.ingredients if i.price_trend == "rising"]

    def falling(self) -> list[IngredientOverview]:
        return [i for i in self.ingredients if i.price_trend == "falling"]

    def stable(self) -> list[IngredientOverview]:
        return [i for i in self.ingredients if i.price_trend == "stable"]

    def sorted_by_price(self, reverse: bool = False) -> list[IngredientOverview]:
        return sorted(
            [i for i in self.ingredients if i.latest_price is not None],
            key=lambda i: i.latest_price,
            reverse=reverse,
        )

    @classmethod
    def from_raw(cls, raw: dict) -> "AllIngredientsHistory":
        return cls(
            count=raw["count"],
            dumps_scanned=raw["dumps_scanned"],
            ingredients=[
                IngredientOverview(
                    ingredient=i["ingredient"],
                    appearances=i["appearances"],
                    last_seen=i["last_seen"],
                    latest_price=i.get("latest_price"),
                    min_price=i.get("min_price"),
                    max_price=i.get("max_price"),
                    avg_price=i.get("avg_price"),
                    price_trend=i.get("price_trend", "stable"),
                )
                for i in raw["ingredients"]
            ],
        )


# ── CLIENT ────────────────────────────────────────────────────────────────────

class RistoranteClient:
    """
    Client for the Ristorante Dashboard API.

    All methods return typed dataclasses or raw dicts where types
    would be overly rigid (e.g. full dump snapshots).

    Args:
        base_url: Server base URL, e.g. "http://localhost:8765"
    """

    def __init__(self, base_url: str = "http://localhost:8765"):
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: dict = None) -> Any:
        return _get(self.base_url, path, params)

    def _delete(self, path: str) -> Any:
        return _delete(self.base_url, path)

    def _post(self, path: str, payload: dict) -> Any:
        return _post(self.base_url, path, payload)

    # ── CONFIG ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict:
        """Return current server config (API key redacted)."""
        return self._get("/api/config")

    def set_config(self, base_url: str = None, api_key: str = None,
                   my_restaurant_id: str = None, refresh_interval: int = None) -> dict:
        """Update server config. Triggers an immediate dump."""
        payload = {}
        if base_url:            payload["base_url"] = base_url
        if api_key:             payload["api_key"] = api_key
        if my_restaurant_id:    payload["my_restaurant_id"] = my_restaurant_id
        if refresh_interval:    payload["refresh_interval"] = refresh_interval
        return self._post("/api/config", payload)

    def refresh(self) -> dict:
        """Trigger an immediate upstream dump."""
        return self._post("/api/refresh", {})

    # ── CURRENT STATUS ────────────────────────────────────────────────────────

    def status(self) -> _Wrap:
        """
        Full current snapshot with deltas.
        Returns a _Wrap for attribute access: status().data.restaurant.balance
        """
        return _Wrap(self._get("/api/status"))

    def restaurant(self) -> _Wrap:
        """My restaurant info + menu + delta."""
        return _Wrap(self._get("/api/restaurant"))

    def restaurants(self) -> list[_Wrap]:
        """All restaurants with embedded _delta."""
        raw = self._get("/api/restaurants")
        return [_Wrap(r) for r in raw.get("data", [])]

    def recipes(self) -> list[_Wrap]:
        """All recipes."""
        raw = self._get("/api/recipes")
        return [_Wrap(r) for r in (raw.get("data") or [])]

    def menu(self) -> _Wrap:
        """My current menu."""
        return _Wrap(self._get("/api/menu"))

    def market(self, side: str = None) -> dict:
        """
        Market entries grouped by ingredient.
        side: optional "BUY" or "SELL" filter.
        Returns raw dict with keys: grouped, delta, summary, ts.
        """
        return self._get("/api/market", {"side": side})

    def market_ingredient(self, ingredient: str) -> dict:
        """All current market entries for a specific ingredient (partial match)."""
        encoded = urllib.parse.quote(ingredient, safe="")
        return self._get(f"/api/market/{encoded}")

    # ── DUMP: SINGLE DUMP ACCESS ──────────────────────────────────────────────

    def dump_latest(self, field: str = None) -> Any:
        """
        Latest dump, optionally extracting a field.

        Examples:
            client.dump_latest()
            client.dump_latest("restaurant.balance")
            client.dump_latest("market.ingredients")
            client.dump_latest("delta.market._summary")
        """
        raw = self._get("/api/dump/latest", {"field": field})
        if field:
            return FieldResult.from_raw(raw, field)
        return raw

    def dump_previous(self, field: str = None) -> Any:
        """Previous dump, optionally extracting a field."""
        raw = self._get("/api/dump/previous", {"field": field})
        if field:
            return FieldResult.from_raw(raw, field)
        return raw

    def dump_at(self, timestamp: str, field: str = None) -> Any:
        """
        Dump at a given ISO timestamp prefix (fuzzy match).

        Examples:
            client.dump_at("2026-02-28T14")         # first dump of that hour
            client.dump_at("2026-02-28T14", "restaurant.balance")
        """
        encoded = urllib.parse.quote(timestamp, safe="")
        raw = self._get(f"/api/dump/at/{encoded}", {"field": field})
        if field:
            return FieldResult.from_raw(raw, field)
        return raw

    def dump_file(self, filename: str, field: str = None) -> Any:
        """
        Dump by filename from disk.

        Examples:
            client.dump_file("2026-02-28T14-30-00Z.json")
            client.dump_file("2026-02-28T14-30-00Z", "market.ingredients")
        """
        encoded = urllib.parse.quote(filename, safe="")
        raw = self._get(f"/api/dump/file/{encoded}", {"field": field})
        if field:
            return FieldResult.from_raw(raw, field)
        return raw

    def delete_dump_file(self, filename: str) -> dict:
        """Delete a dump file from disk."""
        encoded = urllib.parse.quote(filename, safe="")
        return self._delete(f"/api/dump/file/{encoded}")

    # ── DUMP: LISTING & HISTORY ───────────────────────────────────────────────

    def dump_history(self) -> dict:
        """In-memory dump list with delta summaries."""
        return self._get("/api/dump/history")

    def dump_files(self) -> DumpFileList:
        """All persisted dump files on disk."""
        return DumpFileList.from_raw(self._get("/api/dump/files"))

    # ── DUMP: FIELD TOOLS ─────────────────────────────────────────────────────

    def dump_field(self, which: str, field: str) -> FieldResult:
        """
        Shorthand to extract a field from "latest" or "previous".

        Examples:
            client.dump_field("latest", "restaurant.inventory")
            client.dump_field("previous", "market.ingredients")
        """
        if which == "latest":
            return self.dump_latest(field=field)
        elif which == "previous":
            return self.dump_previous(field=field)
        else:
            raise ValueError("which must be 'latest' or 'previous'")

    def timeseries(self, field: str, limit: int = 50) -> Timeseries:
        """
        Extract a field's value across all dumps chronologically.

        Examples:
            client.timeseries("restaurant.balance")
            client.timeseries("restaurant.reputation", limit=20)
            client.timeseries("market.ingredients")

        Returns a Timeseries with .values(), .timestamps(), .as_pairs() helpers.
        """
        raw = self._get("/api/dump/timeseries", {"field": field, "limit": limit})
        return Timeseries.from_raw(raw)

    def compare(self, field: str, ts1: str = None, ts2: str = None) -> CompareResult:
        """
        Compare a field between two dumps.
        Defaults to latest vs previous if ts1/ts2 are not provided.

        Examples:
            client.compare("restaurant.balance")
            client.compare("market.ingredients", ts1="2026-02-28T14", ts2="2026-02-28T15")
        """
        raw = self._get("/api/dump/compare", {"field": field, "ts1": ts1, "ts2": ts2})
        return CompareResult.from_raw(raw)

    # ── HISTORY ROUTES ────────────────────────────────────────────────────────

    def restaurant_history(self, limit: int = 100) -> RestaurantHistory:
        """
        Full history of MY restaurant stats across all dumps.

        Returns a RestaurantHistory with helpers:
            .balances()         → list of balance values over time
            .reputations()      → list of reputation values over time
            .open_states()      → list of isOpen booleans over time
            .changes_only()     → only points where something changed

        Example:
            history = client.restaurant_history()
            for p in history.changes_only():
                print(p.ts, p.delta)
        """
        raw = self._get("/api/history/restaurant", {"limit": limit})
        return RestaurantHistory.from_raw(raw)

    def restaurant_history_by_id(self, restaurant_id: str | int, limit: int = 100) -> RestaurantHistory:
        """
        History of any restaurant by ID.

        Example:
            history = client.restaurant_history_by_id(7)
            print(history.name)
            print(history.balances())
        """
        raw = self._get(f"/api/history/restaurant/{restaurant_id}", {"limit": limit})
        return RestaurantHistory.from_raw(raw)

    def ingredient_history(self, ingredient: str, limit: int = 100,
                           side: str = None) -> IngredientHistory:
        """
        History of a market ingredient across all dumps (statistics per dump).

        For the complete flat list of every individual entry ever seen,
        use ingredient_entries_history() instead.

        Supports partial/case-insensitive ingredient name matching.

        Args:
            ingredient: ingredient name or partial name
            limit:      how many dumps to scan
            side:       optional "BUY" or "SELL" filter

        Returns an IngredientHistory with helpers:
            .avg_prices()       → list of avg prices over time
            .price_deltas()     → price change vs previous dump
            .non_empty()        → only points where the ingredient was present

        Example:
            h = client.ingredient_history("alghe", side="BUY")
            print(h.ingredient)           # resolved canonical name
            print(h.summary.price_trend)  # "rising" / "falling" / "stable"
            for p in h.non_empty():
                print(p.ts, p.avg_unit_price, p.price_delta)
        """
        encoded = urllib.parse.quote(ingredient, safe="")
        raw = self._get(f"/api/history/ingredient/{encoded}", {"limit": limit, "side": side})
        return IngredientHistory.from_raw(raw)

    def ingredient_entries_history(self, ingredient: str, limit: int = 100,
                                   side: str = None, status: str = None,
                                   restaurant_id: str | int = None) -> dict:
        """
        Complete history of every individual market entry ever seen for an ingredient.

        Each entry includes first_seen, last_seen, status_history (list of status
        changes over time), unit_price, restaurant_name, and all original fields.

        Args:
            ingredient:     ingredient name or partial name (case-insensitive)
            limit:          how many dumps to scan (default 100)
            side:           filter by "BUY" or "SELL"
            status:         filter by final status: "open", "cancelled", "filled"
            restaurant_id:  filter by creator restaurant ID

        Returns raw dict with keys:
            ingredient, total, summary, entries

        Example:
            result = client.ingredient_entries_history("alghe")
            print(result["ingredient"])         # resolved name
            print(result["total"])              # number of unique orders
            print(result["summary"])            # aggregated stats
            for entry in result["entries"]:
                print(entry["id"], entry["side"], entry["unit_price"],
                      entry["status"], entry["status_history"])

            # Only open BUY orders
            buys = client.ingredient_entries_history("alghe", side="BUY", status="open")
        """
        encoded = urllib.parse.quote(ingredient, safe="")
        return self._get(f"/api/history/ingredient-entries/{encoded}", {
            "limit": limit,
            "side": side,
            "status": status,
            "restaurant_id": str(restaurant_id) if restaurant_id else None,
        })

    def all_ingredients_history(self, limit: int = 50) -> AllIngredientsHistory:
        """
        Summary of all ingredients that have appeared in the market.

        Returns an AllIngredientsHistory with helpers:
            .rising()               → ingredients with rising price trend
            .falling()              → ingredients with falling price trend
            .stable()               → stable ingredients
            .sorted_by_price()      → sorted cheapest first
            .sorted_by_price(reverse=True)  → most expensive first

        Example:
            overview = client.all_ingredients_history()
            for ing in overview.rising():
                print(ing.ingredient, ing.latest_price, ing.price_trend)
        """
        raw = self._get("/api/history/ingredients", {"limit": limit})
        return AllIngredientsHistory.from_raw(raw)


# ── QUICK DEMO ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
    client = RistoranteClient(url)

    print(f"\n{'='*50}")
    print(f"  Ristorante Client — {url}")
    print(f"{'='*50}\n")

    try:
        # Config
        cfg = client.get_config()
        print(f"Config: base_url={cfg.get('base_url')}, restaurant={cfg.get('my_restaurant_id')}\n")

        # Latest field
        bal = client.dump_field("latest", "restaurant.balance")
        print(f"Latest balance:     {bal.value} @ {bal.ts}")

        rep = client.dump_field("latest", "restaurant.reputation")
        print(f"Latest reputation:  {rep.value}")

        ingredients = client.dump_field("latest", "market.ingredients")
        print(f"Market ingredients: {ingredients.value}\n")

        # Timeseries
        ts = client.timeseries("restaurant.balance", limit=5)
        print(f"Balance timeseries ({ts.count} points):")
        for t, v in ts.as_pairs():
            print(f"  {t}  →  {v}")
        print()

        # Compare
        cmp = client.compare("restaurant.balance")
        print(f"Balance changed: {cmp.changed}  ({cmp.from_value} → {cmp.to_value})\n")

        # Dump files
        files = client.dump_files()
        print(f"Dump files on disk: {files.count} in {files.dumps_dir}")
        if files.files:
            print(f"  Latest: {files.files[-1].filename}\n")

        # My restaurant history
        hist = client.restaurant_history(limit=5)
        print(f"My restaurant history ({hist.count} points):")
        for p in hist.series[-3:]:
            print(f"  {p.ts}  balance={p.balance}  reputation={p.reputation}  open={p.is_open}"
                  + (f"  Δ{list(p.delta.keys())}" if p.delta else ""))
        print()

        # Ingredient overview
        overview = client.all_ingredients_history(limit=10)
        print(f"Ingredient overview ({overview.count} ingredients across {overview.dumps_scanned} dumps):")
        for ing in overview.sorted_by_price():
            print(ing)
            trend_arrow = {"rising": "↑", "falling": "↓", "stable": "→"}.get(ing.price_trend, "?")
            print(f"  {trend_arrow} {ing.ingredient:<35} €{ing.latest_price}/u  avg €{ing.avg_price}")

    except APIError as e:
        print(f"API Error {e.status}: {e.message}")
    except Exception as e:
        print(f"Error: {e}")
        raise
