"""
Ristorante Dashboard — History API Client
==========================================
Fetches complete history for all ingredients from the dashboard API.

Usage:
    from history_client import HistoryClient

    with HistoryClient("http://localhost:8765") as c:
        data = c.all_ingredients_history(include_entries=True, include_prices=True)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from urllib.parse import quote

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency: pip install httpx")


# ── Ingredient History (aggregated per-dump stats) ────────────────────────────


@dataclass
class IngredientSnapshot:
    """One dump's aggregated stats for an ingredient."""
    ts: str
    total_entries: int = 0
    buy_count: int = 0
    sell_count: int = 0
    min_unit_price: float | None = None
    max_unit_price: float | None = None
    avg_unit_price: float | None = None
    total_volume: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    price_delta: float | None = None


@dataclass
class IngredientHistorySummary:
    appearances: int = 0
    overall_min_price: float | None = None
    overall_max_price: float | None = None
    overall_avg_price: float | None = None
    price_trend: str = "stable"


@dataclass
class IngredientHistory:
    """Aggregated per-dump stats for an ingredient across time."""
    ingredient: str
    side_filter: str | None
    count: int
    summary: IngredientHistorySummary
    series: list[IngredientSnapshot]


# ── Ingredient Entries (deduplicated individual orders) ───────────────────────


@dataclass
class StatusChange:
    ts: str
    status: str | None


@dataclass
class MarketEntry:
    """A unique market order, tracked across dumps."""
    id: int
    side: str | None = None
    status: str | None = None
    unit_price: float = 0.0
    total_price: float = 0.0
    quantity: int = 0
    restaurant_name: str = ""
    restaurant_id: int | str | None = None
    is_mine: bool = False
    first_seen: str = ""
    last_seen: str = ""
    status_history: list[StatusChange] = field(default_factory=list)


@dataclass
class IngredientEntriesSummary:
    min_unit_price: float | None = None
    max_unit_price: float | None = None
    avg_unit_price: float | None = None
    total_volume: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_side: dict[str, int] = field(default_factory=dict)
    by_restaurant: dict[str, int] = field(default_factory=dict)


@dataclass
class IngredientEntries:
    """Every individual market entry ever seen for an ingredient."""
    ingredient: str
    side_filter: str | None
    total: int
    summary: IngredientEntriesSummary
    entries: list[MarketEntry]


# ── Ingredient Prices (raw price timeline) ────────────────────────────────────


@dataclass
class PricePoint:
    """A single price observation from one dump + one entry."""
    ts: str
    entry_id: int
    side: str | None = None
    unit_price: float = 0.0
    total_price: float = 0.0
    quantity: int = 0
    status: str | None = None
    restaurant_name: str = ""
    restaurant_id: int | str | None = None
    is_mine: bool = False


@dataclass
class IngredientPricesSummary:
    min_price: float | None = None
    max_price: float | None = None
    avg_price: float | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    dumps_scanned: int = 0


@dataclass
class IngredientPrices:
    """Raw price timeline for an ingredient."""
    ingredient: str
    side_filter: str | None
    total: int
    summary: IngredientPricesSummary
    timeline: list[PricePoint]


# ── Restaurant History ────────────────────────────────────────────────────────


@dataclass
class InventoryDelta:
    prev: float | None
    curr: float | None


@dataclass
class ScalarDelta:
    prev: float | None
    curr: float | None
    diff: float | None = None
    pct: float | None = None
    changed: bool = True


@dataclass
class RestaurantSnapshotDelta:
    balance: ScalarDelta | None = None
    reputation: ScalarDelta | None = None
    is_open: ScalarDelta | None = None
    inventory: dict[str, InventoryDelta] = field(default_factory=dict)


@dataclass
class RestaurantSnapshot:
    """One dump's snapshot of a restaurant."""
    ts: str
    balance: float | None = None
    reputation: float | None = None
    is_open: bool | None = None
    inventory: dict[str, float] = field(default_factory=dict)
    menu_items: list[str] = field(default_factory=list)
    kitchen: list = field(default_factory=list)
    delta: RestaurantSnapshotDelta | None = None


@dataclass
class RestaurantHistory:
    """Chronological history of a restaurant's stats."""
    restaurant_id: str
    name: str | None = None
    count: int = 0
    series: list[RestaurantSnapshot] = field(default_factory=list)


# ── Full bulk result ──────────────────────────────────────────────────────────


@dataclass
class IngredientFullHistory:
    """Complete history bundle for a single ingredient."""
    history: IngredientHistory
    entries: IngredientEntries | None = None
    prices: IngredientPrices | None = None
    error: str | None = None


# ── Parsing helpers ───────────────────────────────────────────────────────────


def _parse_ingredient_history(data: dict) -> IngredientHistory:
    s = data.get("summary", {})
    return IngredientHistory(
        ingredient=data["ingredient"],
        side_filter=data.get("side_filter"),
        count=data.get("count", 0),
        summary=IngredientHistorySummary(
            appearances=s.get("appearances", 0),
            overall_min_price=s.get("overall_min_price"),
            overall_max_price=s.get("overall_max_price"),
            overall_avg_price=s.get("overall_avg_price"),
            price_trend=s.get("price_trend", "stable"),
        ),
        series=[
            IngredientSnapshot(
                ts=p["ts"],
                total_entries=p.get("total_entries", 0),
                buy_count=p.get("buy_count", 0),
                sell_count=p.get("sell_count", 0),
                min_unit_price=p.get("min_unit_price"),
                max_unit_price=p.get("max_unit_price"),
                avg_unit_price=p.get("avg_unit_price"),
                total_volume=p.get("total_volume", 0),
                statuses=p.get("statuses", {}),
                price_delta=p.get("price_delta"),
            )
            for p in data.get("series", [])
        ],
    )


def _parse_ingredient_entries(data: dict) -> IngredientEntries:
    s = data.get("summary", {})
    return IngredientEntries(
        ingredient=data["ingredient"],
        side_filter=data.get("side_filter"),
        total=data.get("total", 0),
        summary=IngredientEntriesSummary(
            min_unit_price=s.get("min_unit_price"),
            max_unit_price=s.get("max_unit_price"),
            avg_unit_price=s.get("avg_unit_price"),
            total_volume=s.get("total_volume", 0),
            by_status=s.get("by_status", {}),
            by_side=s.get("by_side", {}),
            by_restaurant=s.get("by_restaurant", {}),
        ),
        entries=[
            MarketEntry(
                id=e["id"],
                side=e.get("side"),
                status=e.get("status"),
                unit_price=e.get("unit_price", 0),
                total_price=e.get("totalPrice", 0),
                quantity=e.get("quantity", 0),
                restaurant_name=e.get("restaurant_name", ""),
                restaurant_id=e.get("createdByRestaurantId"),
                is_mine=e.get("is_mine", False),
                first_seen=e.get("first_seen", ""),
                last_seen=e.get("last_seen", ""),
                status_history=[
                    StatusChange(ts=sh["ts"], status=sh.get("status"))
                    for sh in e.get("status_history", [])
                ],
            )
            for e in data.get("entries", [])
        ],
    )


def _parse_ingredient_prices(data: dict) -> IngredientPrices:
    s = data.get("summary", {})
    return IngredientPrices(
        ingredient=data["ingredient"],
        side_filter=data.get("side_filter"),
        total=data.get("total", 0),
        summary=IngredientPricesSummary(
            min_price=s.get("min_price"),
            max_price=s.get("max_price"),
            avg_price=s.get("avg_price"),
            first_ts=s.get("first_ts"),
            last_ts=s.get("last_ts"),
            dumps_scanned=s.get("dumps_scanned", 0),
        ),
        timeline=[
            PricePoint(
                ts=r["ts"],
                entry_id=r["entry_id"],
                side=r.get("side"),
                unit_price=r.get("unit_price", 0),
                total_price=r.get("total_price", 0),
                quantity=r.get("quantity", 0),
                status=r.get("status"),
                restaurant_name=r.get("restaurant_name", ""),
                restaurant_id=r.get("restaurant_id"),
                is_mine=r.get("is_mine", False),
            )
            for r in data.get("timeline", [])
        ],
    )


def _parse_restaurant_history(data: dict) -> RestaurantHistory:
    series = []
    for snap in data.get("series", []):
        delta = None
        d = snap.get("delta")
        if d:
            delta = RestaurantSnapshotDelta(
                balance=ScalarDelta(**d["balance"]) if d.get("balance") else None,
                reputation=ScalarDelta(**d["reputation"]) if d.get("reputation") else None,
                is_open=ScalarDelta(**d["isOpen"]) if d.get("isOpen") else None,
                inventory={
                    k: InventoryDelta(prev=v.get("prev"), curr=v.get("curr"))
                    for k, v in d.get("inventory", {}).items()
                },
            )
        series.append(RestaurantSnapshot(
            ts=snap["ts"],
            balance=snap.get("balance"),
            reputation=snap.get("reputation"),
            is_open=snap.get("isOpen"),
            inventory=snap.get("inventory", {}),
            menu_items=snap.get("menu_items", []),
            kitchen=snap.get("kitchen", []),
            delta=delta,
        ))
    return RestaurantHistory(
        restaurant_id=str(data.get("restaurant_id", "")),
        name=data.get("name"),
        count=data.get("count", 0),
        series=series,
    )


# ── Client ────────────────────────────────────────────────────────────────────


class HistoryClient:
    def __init__(self, base_url: str = "http://localhost:8765", timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self.client.get(self.base_url + path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Core history routes ───────────────────────────────────────────

    def market_ingredients(self) -> list[str]:
        """Get list of all ingredient names from the latest dump."""
        data = self._get("/api/dump/latest", {"field": "market.ingredients"})
        return data.get("value", [])

    def ingredient_history(
        self, name: str, limit: int = 100, side: str | None = None
    ) -> IngredientHistory:
        """Aggregated stats per dump for an ingredient."""
        params: dict = {"limit": limit}
        if side:
            params["side"] = side
        data = self._get(f"/api/history/ingredient/{quote(name, safe='')}", params)
        return _parse_ingredient_history(data)

    def ingredient_entries(
        self,
        name: str,
        limit: int = 100,
        side: str | None = None,
        status: str | None = None,
        restaurant_id: str | None = None,
    ) -> IngredientEntries:
        """Every individual market entry ever seen for an ingredient."""
        params: dict = {"limit": limit}
        if side:
            params["side"] = side
        if status:
            params["status"] = status
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        data = self._get(f"/api/history/ingredient-entries/{quote(name, safe='')}", params)
        return _parse_ingredient_entries(data)

    def ingredient_prices(
        self, name: str, limit: int = 100, side: str | None = None
    ) -> IngredientPrices:
        """Raw price timeline for an ingredient."""
        params: dict = {"limit": limit}
        if side:
            params["side"] = side
        data = self._get(f"/api/history/ingredient-prices/{quote(name, safe='')}", params)
        return _parse_ingredient_prices(data)

    def restaurant_history(
        self, restaurant_id: str | None = None, limit: int = 100
    ) -> RestaurantHistory:
        """History of a restaurant's stats. Omit restaurant_id for your own."""
        if restaurant_id:
            data = self._get(f"/api/history/restaurant/{restaurant_id}", {"limit": limit})
        else:
            data = self._get("/api/history/restaurant", {"limit": limit})
        return _parse_restaurant_history(data)

    # ── Bulk fetch ────────────────────────────────────────────────────

    def all_ingredients_history(
        self,
        limit: int = 100,
        side: str | None = None,
        include_entries: bool = False,
        include_prices: bool = False,
        delay: float = 0.1,
    ) -> dict[str, IngredientFullHistory]:
        """
        Fetch complete history for every ingredient on the market.

        Returns a dict keyed by ingredient name, each containing an
        IngredientFullHistory with history, entries, and prices fields.
        """
        ingredients = self.market_ingredients()
        print(f"Found {len(ingredients)} ingredients")

        results: dict[str, IngredientFullHistory] = {}
        for i, name in enumerate(ingredients, 1):
            print(f"  [{i}/{len(ingredients)}] {name}...", end=" ", flush=True)
            try:
                history = self.ingredient_history(name, limit, side)
                entries = self.ingredient_entries(name, limit, side) if include_entries else None
                prices = self.ingredient_prices(name, limit, side) if include_prices else None
                results[name] = IngredientFullHistory(
                    history=history, entries=entries, prices=prices
                )
                print("ok")
            except httpx.HTTPStatusError as e:
                print(f"error {e.response.status_code}")
                results[name] = IngredientFullHistory(
                    history=IngredientHistory(
                        ingredient=name, side_filter=side, count=0,
                        summary=IngredientHistorySummary(), series=[],
                    ),
                    error=str(e),
                )
            if delay and i < len(ingredients):
                time.sleep(delay)

        return results

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── Serialization helper ─────────────────────────────────────────────────────


def to_dict(obj) -> dict:
    """Convert any dataclass result to a plain dict (e.g. for JSON export)."""
    return asdict(obj)


if __name__ == "__main__":
    BASE_URL = "http://localhost:8765"

    with HistoryClient(BASE_URL) as c:
        # ── List all ingredients ──────────────────────────────────────
        ingredients = c.market_ingredients()
        print(f"\n{'='*60}")
        print(f"  {len(ingredients)} ingredients on the market")
        print(f"{'='*60}")
        for name in ingredients:
            print(f"  • {name}")

        if not ingredients:
            print("No ingredients found — is the server running?")
            sys.exit(1)

        sample = ingredients[0]

        # ── Ingredient history (aggregated per dump) ──────────────────
        print(f"\n{'='*60}")
        print(f"  History: {sample}")
        print(f"{'='*60}")
        h = c.ingredient_history(sample)
        print(f"  Trend:     {h.summary.price_trend}")
        print(f"  Avg price: {h.summary.overall_avg_price}")
        print(f"  Min price: {h.summary.overall_min_price}")
        print(f"  Max price: {h.summary.overall_max_price}")
        print(f"  Snapshots: {h.count}")
        for snap in h.series[-3:]:
            print(f"    {snap.ts}  avg={snap.avg_unit_price}  vol={snap.total_volume}  Δ={snap.price_delta}")

        # ── Ingredient entries (individual orders) ────────────────────
        print(f"\n{'='*60}")
        print(f"  Entries: {sample}")
        print(f"{'='*60}")
        e = c.ingredient_entries(sample)
        print(f"  Total orders: {e.total}")
        print(f"  By status:    {e.summary.by_status}")
        print(f"  By side:      {e.summary.by_side}")
        for entry in e.entries[:5]:
            print(f"    #{entry.id}  {entry.side}  {entry.unit_price:.4f}/u  "
                  f"qty={entry.quantity}  {entry.status}  by {entry.restaurant_name}")

        # ── Ingredient prices (raw timeline) ──────────────────────────
        print(f"\n{'='*60}")
        print(f"  Prices: {sample}")
        print(f"{'='*60}")
        p = c.ingredient_prices(sample)
        print(f"  Observations: {p.total}")
        print(f"  Range: {p.summary.min_price} — {p.summary.max_price}")
        for pt in p.timeline[-5:]:
            mine = " ★" if pt.is_mine else ""
            print(f"    {pt.ts}  {pt.side}  {pt.unit_price:.4f}/u  [{pt.status}]{mine}")

        # ── Restaurant history ────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  My restaurant history")
        print(f"{'='*60}")
        r = c.restaurant_history()
        print(f"  Restaurant ID: {r.restaurant_id}")
        print(f"  Snapshots:     {r.count}")
        for snap in r.series[-3:]:
            delta_info = ""
            if snap.delta and snap.delta.balance:
                delta_info = f"  Δbal={snap.delta.balance.diff:+.2f}"
            print(f"    {snap.ts}  bal={snap.balance}  rep={snap.reputation}{delta_info}")

        # ── Bulk fetch all ingredients ────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  Bulk fetch (all ingredients, with entries & prices)")
        print(f"{'='*60}")
        all_data = c.all_ingredients_history(include_entries=True, include_prices=True)

        # Export to JSON
        output_path = "history_dump.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({name: to_dict(fh) for name, fh in all_data.items()}, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(all_data)} ingredients to {output_path}")