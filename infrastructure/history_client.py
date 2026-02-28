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


# ── Price Board (compact per-snapshot offers) ─────────────────────────────────


@dataclass
class Offer:
    """A single offer on the market at a given point in time."""
    restaurant: str
    side: str
    price: float
    quantity: int


@dataclass
class PriceSnapshot:
    """All offers for an ingredient at one dump timestamp."""
    ts: str
    prices: list[Offer]


# ── Dish Price History ────────────────────────────────────────────────────────


@dataclass
class DishObservation:
    """A single price observation of a dish at one restaurant in one dump."""
    ts: str
    restaurant_id: str
    restaurant_name: str
    price: float


@dataclass
class DishSummary:
    total_observations: int = 0
    total_changes: int = 0
    restaurants: list[str] = field(default_factory=list)
    restaurant_count: int = 0
    min_price: float = 0.0
    max_price: float = 0.0
    avg_price: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    current_price: float | None = None


@dataclass
class DishRestaurantBreakdown:
    """Price history of a dish at one specific restaurant."""
    observations: int = 0
    min_price: float = 0.0
    max_price: float = 0.0
    avg_price: float = 0.0
    current_price: float = 0.0
    price_history: list[DishObservation] = field(default_factory=list)


@dataclass
class DishHistory:
    """Complete price history for a single dish across all restaurants."""
    dish: str
    restaurant_filter: str | None
    dumps_scanned: int
    summary: DishSummary
    changes: list[DishObservation]
    by_restaurant: dict[str, DishRestaurantBreakdown]
    observations: list[DishObservation]


@dataclass
class DishBoardEntry:
    """Summary entry for one dish in the full dish board."""
    summary: DishSummary
    observations: list[DishObservation]
    changes: list[DishObservation]


@dataclass
class DishBoard:
    """Price history board for all dishes."""
    dish_count: int
    restaurant_filter: str | None
    dumps_scanned: int
    dishes: dict[str, DishBoardEntry]


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

# ── Meal dataclasses ──────────────────────────────────────────────────────────

@dataclass
class MealCustomer:
    name: str = ""

@dataclass
class Meal:
    id: int = 0
    turn_id: int = 0
    customer_id: int = 0
    restaurant_id: int = 0
    request: str = ""
    start_time: str = ""
    served_dish_id: int | None = None
    status: str = ""
    customer: MealCustomer | None = None
    executed: bool = False
    first_seen: str = ""
    last_seen: str = ""
    status_history: list[StatusChange] = field(default_factory=list)

@dataclass
class MealsSummary:
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    unique_dishes: int = 0

@dataclass
class Meals:
    ts: str = ""
    turn_id: int | None = None
    data: list[Meal] = field(default_factory=list)
    by_dish: dict[str, list[Meal]] = field(default_factory=dict)
    summary: MealsSummary | None = None

@dataclass
class MealsSnapshotDish:
    count: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

@dataclass
class MealsSnapshot:
    ts: str = ""
    turn_id: int | None = None
    total: int = 0
    new_count: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_dish: dict[str, MealsSnapshotDish] = field(default_factory=dict)
    unique_dishes: int = 0
    meals: list[Meal] = field(default_factory=list)

@dataclass
class MealsHistory:
    restaurant_id: str = ""
    count: int = 0
    series: list[MealsSnapshot] = field(default_factory=list)

@dataclass
class MealsEntriesSummary:
    by_status: dict[str, int] = field(default_factory=dict)
    by_dish: dict[str, int] = field(default_factory=dict)

@dataclass
class MealsEntries:
    total: int = 0
    summary: MealsEntriesSummary | None = None
    entries: list[Meal] = field(default_factory=list)


# ── Bid dataclasses ───────────────────────────────────────────────────────────

@dataclass
class BidRestaurant:
    name: str = ""

@dataclass
class BidIngredient:
    id: int = 0
    name: str = ""

@dataclass
class Bid:
    id: int = 0
    turn_id: int = 0
    restaurant_id: int = 0
    ingredient_id: int = 0
    quantity: int = 0
    price_for_each: float = 0
    status: str = ""
    restaurant: BidRestaurant | None = None
    ingredient: BidIngredient | None = None
    first_seen: str = ""
    last_seen: str = ""
    status_history: list[StatusChange] = field(default_factory=list)

@dataclass
class BidHistorySummary:
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    unique_ingredients: int = 0
    unique_restaurants: int = 0
    total_spent: float = 0

@dataclass
class BidHistory:
    ts: str = ""
    turn_id: int | None = None
    data: list[Bid] = field(default_factory=list)
    by_ingredient: dict[str, list[Bid]] = field(default_factory=dict)
    by_restaurant: dict[str, list[Bid]] = field(default_factory=dict)
    summary: BidHistorySummary | None = None

@dataclass
class BidIngredientAgg:
    count: int = 0
    total_qty: int = 0
    total_spent: float = 0

@dataclass
class BidRestaurantAgg:
    count: int = 0
    total_spent: float = 0

@dataclass
class BidsSnapshot:
    ts: str = ""
    turn_id: int | None = None
    total: int = 0
    new_count: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_ingredient: dict[str, BidIngredientAgg] = field(default_factory=dict)
    by_restaurant: dict[str, BidRestaurantAgg] = field(default_factory=dict)
    total_spent: float = 0
    bids: list[Bid] = field(default_factory=list)

@dataclass
class BidsHistory:
    count: int = 0
    series: list[BidsSnapshot] = field(default_factory=list)

@dataclass
class BidsEntriesSummary:
    by_status: dict[str, int] = field(default_factory=dict)
    by_ingredient: dict[str, int] = field(default_factory=dict)
    by_restaurant: dict[str, int] = field(default_factory=dict)
    total_spent: float = 0

@dataclass
class BidsEntries:
    total: int = 0
    summary: BidsEntriesSummary | None = None
    entries: list[Bid] = field(default_factory=list)

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


def _parse_dish_observation(o: dict) -> DishObservation:
    return DishObservation(
        ts=o["ts"],
        restaurant_id=str(o.get("restaurant_id", "")),
        restaurant_name=o.get("restaurant_name", ""),
        price=o.get("price", 0),
    )


def _parse_dish_history(data: dict) -> DishHistory:
    s = data.get("summary", {})
    by_restaurant = {}
    for rname, rdata in data.get("by_restaurant", {}).items():
        by_restaurant[rname] = DishRestaurantBreakdown(
            observations=rdata.get("observations", 0),
            min_price=rdata.get("min_price", 0),
            max_price=rdata.get("max_price", 0),
            avg_price=rdata.get("avg_price", 0),
            current_price=rdata.get("current_price", 0),
            price_history=[_parse_dish_observation(o) for o in rdata.get("price_history", [])],
        )
    return DishHistory(
        dish=data["dish"],
        restaurant_filter=data.get("restaurant_filter"),
        dumps_scanned=data.get("dumps_scanned", 0),
        summary=DishSummary(
            total_observations=s.get("total_observations", 0),
            total_changes=s.get("total_changes", 0),
            restaurants=s.get("restaurants", []),
            restaurant_count=s.get("restaurant_count", 0),
            min_price=s.get("min_price", 0),
            max_price=s.get("max_price", 0),
            avg_price=s.get("avg_price", 0),
            first_seen=s.get("first_seen", ""),
            last_seen=s.get("last_seen", ""),
        ),
        changes=[_parse_dish_observation(o) for o in data.get("changes", [])],
        by_restaurant=by_restaurant,
        observations=[_parse_dish_observation(o) for o in data.get("observations", [])],
    )


def _parse_dish_board(data: dict) -> DishBoard:
    dishes = {}
    for name, d in data.get("dishes", {}).items():
        s = d.get("summary", {})
        dishes[name] = DishBoardEntry(
            summary=DishSummary(
                total_observations=s.get("total_observations", 0),
                total_changes=s.get("total_changes", 0),
                restaurants=s.get("restaurants", []),
                restaurant_count=s.get("restaurant_count", 0),
                min_price=s.get("min_price", 0),
                max_price=s.get("max_price", 0),
                avg_price=s.get("avg_price", 0),
                first_seen=s.get("first_seen", ""),
                last_seen=s.get("last_seen", ""),
                current_price=s.get("current_price"),
            ),
            observations=[_parse_dish_observation(o) for o in d.get("observations", [])],
            changes=[_parse_dish_observation(o) for o in d.get("changes", [])],
        )
    return DishBoard(
        dish_count=data.get("dish_count", 0),
        restaurant_filter=data.get("restaurant_filter"),
        dumps_scanned=data.get("dumps_scanned", 0),
        dishes=dishes,
    )

# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_meal(m: dict) -> Meal:
    customer = None
    if m.get("customer"):
        customer = MealCustomer(name=m["customer"].get("name", ""))
    return Meal(
        id=m.get("id", 0),
        turn_id=m.get("turnId", 0),
        customer_id=m.get("customerId", 0),
        restaurant_id=m.get("restaurantId", 0),
        request=m.get("request", ""),
        start_time=m.get("startTime", ""),
        served_dish_id=m.get("servedDishId"),
        status=m.get("status", ""),
        customer=customer,
        executed=m.get("executed", False),
        first_seen=m.get("first_seen", ""),
        last_seen=m.get("last_seen", ""),
        status_history=[
            StatusChange(ts=sh["ts"], status=sh.get("status"))
            for sh in m.get("status_history", [])
        ],
    )


def _parse_bid(b: dict) -> Bid:
    restaurant = None
    if b.get("restaurant"):
        restaurant = BidRestaurant(name=b["restaurant"].get("name", ""))
    ingredient = None
    if b.get("ingredient"):
        ingredient = BidIngredient(
            id=b["ingredient"].get("id", 0),
            name=b["ingredient"].get("name", ""),
        )
    return Bid(
        id=b.get("id", 0),
        turn_id=b.get("turnId", 0),
        restaurant_id=b.get("restaurantId", 0),
        ingredient_id=b.get("ingredientId", 0),
        quantity=b.get("quantity", 0),
        price_for_each=b.get("priceForEach", 0),
        status=b.get("status", ""),
        restaurant=restaurant,
        ingredient=ingredient,
        first_seen=b.get("first_seen", ""),
        last_seen=b.get("last_seen", ""),
        status_history=[
            StatusChange(ts=sh["ts"], status=sh.get("status"))
            for sh in b.get("status_history", [])
        ],
    )


def _parse_meals(data: dict) -> Meals:
    meals = [_parse_meal(m) for m in data.get("data", [])]
    by_dish: dict[str, list[Meal]] = {}
    for dish_name, meal_list in data.get("by_dish", {}).items():
        by_dish[dish_name] = [_parse_meal(m) for m in meal_list]
    s = data.get("summary", {})
    return Meals(
        ts=data.get("ts", ""),
        turn_id=data.get("turn_id"),
        data=meals,
        by_dish=by_dish,
        summary=MealsSummary(
            total=s.get("total", 0),
            by_status=s.get("by_status", {}),
            unique_dishes=s.get("unique_dishes", 0),
        ),
    )


def _parse_bid_history(data: dict) -> BidHistory:
    bids = [_parse_bid(b) for b in data.get("data", [])]
    by_ingredient: dict[str, list[Bid]] = {}
    for name, bid_list in data.get("by_ingredient", {}).items():
        by_ingredient[name] = [_parse_bid(b) for b in bid_list]
    by_restaurant: dict[str, list[Bid]] = {}
    for name, bid_list in data.get("by_restaurant", {}).items():
        by_restaurant[name] = [_parse_bid(b) for b in bid_list]
    s = data.get("summary", {})
    return BidHistory(
        ts=data.get("ts", ""),
        turn_id=data.get("turn_id"),
        data=bids,
        by_ingredient=by_ingredient,
        by_restaurant=by_restaurant,
        summary=BidHistorySummary(
            total=s.get("total", 0),
            by_status=s.get("by_status", {}),
            unique_ingredients=s.get("unique_ingredients", 0),
            unique_restaurants=s.get("unique_restaurants", 0),
            total_spent=s.get("total_spent", 0),
        ),
    )


def _parse_meals_history(data: dict) -> MealsHistory:
    series = []
    for snap in data.get("series", []):
        by_dish = {}
        for name, d in snap.get("by_dish", {}).items():
            by_dish[name] = MealsSnapshotDish(
                count=d.get("count", 0),
                statuses=d.get("statuses", {}),
            )
        series.append(MealsSnapshot(
            ts=snap.get("ts", ""),
            turn_id=snap.get("turn_id"),
            total=snap.get("total", 0),
            new_count=snap.get("new_count", 0),
            by_status=snap.get("by_status", {}),
            by_dish=by_dish,
            unique_dishes=snap.get("unique_dishes", 0),
            meals=[_parse_meal(m) for m in snap.get("meals", [])],
        ))
    return MealsHistory(
        restaurant_id=str(data.get("restaurant_id", "")),
        count=data.get("count", 0),
        series=series,
    )


def _parse_meals_entries(data: dict) -> MealsEntries:
    s = data.get("summary", {})
    return MealsEntries(
        total=data.get("total", 0),
        summary=MealsEntriesSummary(
            by_status=s.get("by_status", {}),
            by_dish=s.get("by_dish", {}),
        ),
        entries=[_parse_meal(m) for m in data.get("entries", [])],
    )


def _parse_bids_history(data: dict) -> BidsHistory:
    series = []
    for snap in data.get("series", []):
        by_ingredient = {}
        for name, d in snap.get("by_ingredient", {}).items():
            by_ingredient[name] = BidIngredientAgg(
                count=d.get("count", 0),
                total_qty=d.get("total_qty", 0),
                total_spent=d.get("total_spent", 0),
            )
        by_restaurant = {}
        for name, d in snap.get("by_restaurant", {}).items():
            by_restaurant[name] = BidRestaurantAgg(
                count=d.get("count", 0),
                total_spent=d.get("total_spent", 0),
            )
        series.append(BidsSnapshot(
            ts=snap.get("ts", ""),
            turn_id=snap.get("turn_id"),
            total=snap.get("total", 0),
            new_count=snap.get("new_count", 0),
            by_status=snap.get("by_status", {}),
            by_ingredient=by_ingredient,
            by_restaurant=by_restaurant,
            total_spent=snap.get("total_spent", 0),
            bids=[_parse_bid(b) for b in snap.get("bids", [])],
        ))
    return BidsHistory(
        count=data.get("count", 0),
        series=series,
    )


def _parse_bids_entries(data: dict) -> BidsEntries:
    s = data.get("summary", {})
    return BidsEntries(
        total=data.get("total", 0),
        summary=BidsEntriesSummary(
            by_status=s.get("by_status", {}),
            by_ingredient=s.get("by_ingredient", {}),
            by_restaurant=s.get("by_restaurant", {}),
            total_spent=s.get("total_spent", 0),
        ),
        entries=[_parse_bid(b) for b in data.get("entries", [])],
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

    # ── Price board ───────────────────────────────────────────────────

    def price_board(
        self,
        ingredients: list[str] | None = None,
        limit: int = 100,
        side: str | None = None,
        delay: float = 0.1,
    ) -> dict[str, list[PriceSnapshot]]:
        """
        Compact price board for all (or selected) ingredients.

        Returns a dict keyed by ingredient name, each value being a
        chronological list of PriceSnapshot (one per dump), containing
        only the individual offers observed at that point in time.

        Example output structure:
            {
                "Alghe Bioluminescenti": [
                    PriceSnapshot(ts="2026-...", prices=[
                        Offer(restaurant="Da Mario", side="BUY", price=10.0, quantity=5),
                        Offer(restaurant="La Stella", side="SELL", price=12.5, quantity=3),
                    ]),
                    ...
                ],
            }
        """
        if ingredients is None:
            ingredients = self.market_ingredients()
            print(f"Found {len(ingredients)} ingredients")

        results: dict[str, list[PriceSnapshot]] = {}
        for i, name in enumerate(ingredients, 1):
            print(f"  [{i}/{len(ingredients)}] {name}...", end=" ", flush=True)
            try:
                h = self.ingredient_history(name, limit, side)
                snapshots: list[PriceSnapshot] = []
                for snap in h.series:
                    # snap.entries isn't available in IngredientSnapshot (aggregated),
                    # so we pull from the raw prices endpoint per ingredient
                    pass  # filled below via ingredient_prices
                # Use the prices timeline, grouped by ts
                p = self.ingredient_prices(name, limit, side)
                by_ts: dict[str, list[Offer]] = {}
                for pt in p.timeline:
                    by_ts.setdefault(pt.ts, []).append(Offer(
                        restaurant=pt.restaurant_name,
                        side=pt.side or "",
                        price=pt.unit_price,
                        quantity=pt.quantity,
                    ))
                results[name] = [
                    PriceSnapshot(ts=ts, prices=offers)
                    for ts, offers in by_ts.items()
                ]
                print("ok")
            except httpx.HTTPStatusError as e:
                print(f"error {e.response.status_code}")
                results[name] = []
            if delay and i < len(ingredients):
                time.sleep(delay)

        return results

    # ── Dish prices ───────────────────────────────────────────────────

    def dish_board(
        self, limit: int = 200, restaurant_id: str | None = None
    ) -> DishBoard:
        """
        Price history for every dish across all restaurants and dumps.

        Returns a DishBoard with a dict of DishBoardEntry per dish name,
        each containing observations, changes, and summary stats.
        """
        params: dict = {"limit": limit}
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        data = self._get("/api/history/dishes", params)
        return _parse_dish_board(data)

    def dish_history(
        self, name: str, limit: int = 200, restaurant_id: str | None = None
    ) -> DishHistory:
        """
        Price history for a single dish (partial/case-insensitive match).

        Returns full observations, changes, per-restaurant breakdown, and summary.
        """
        params: dict = {"limit": limit}
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        data = self._get(f"/api/history/dish/{quote(name, safe='')}", params)
        return _parse_dish_history(data)

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

    def set_turn(self, turn_id: int) -> int:
        """Set the current turn_id on the server (triggers a fresh dump)."""
        resp = self.client.post(self.base_url + "/api/turn", json={"turn_id": turn_id})
        resp.raise_for_status()
        return resp.json().get("turn_id")

    def get_turn(self) -> int | None:
        """Get the current turn_id from the server."""
        return self._get("/api/turn").get("turn_id")

    # ── Meals (current snapshot) ──────────────────────────────────────

    def meals(self, status: str | None = None) -> Meals:
        """Get meals from the latest dump, optionally filtered by status."""
        params = {}
        if status:
            params["status"] = status
        data = self._get("/api/meals", params)
        return _parse_meals(data)

    def bid_history(
            self,
            restaurant_id: str | None = None,
            ingredient: str | None = None,
            status: str | None = None,
    ) -> BidHistory:
        """Get bid history from the latest dump, with optional filters."""
        params = {}
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        if ingredient:
            params["ingredient"] = ingredient
        if status:
            params["status"] = status
        data = self._get("/api/bid_history", params)
        return _parse_bid_history(data)

    # ── Meals & Bids (history) ────────────────────────────────────────

    def meals_history(self, limit: int = 200) -> MealsHistory:
        """Per-dump aggregated meal snapshots."""
        data = self._get("/api/history/meals", {"limit": limit})
        return _parse_meals_history(data)

    def meals_entries(
            self, status: str | None = None, dish: str | None = None, limit: int = 200
    ) -> MealsEntries:
        """Deduplicated meal entries with status tracking."""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if dish:
            params["dish"] = dish
        data = self._get("/api/history/meals/entries", params)
        return _parse_meals_entries(data)

    def bids_history(self, limit: int = 200) -> BidsHistory:
        """Per-dump aggregated bid snapshots."""
        data = self._get("/api/history/bids", {"limit": limit})
        return _parse_bids_history(data)

    def bids_entries(
            self,
            status: str | None = None,
            ingredient: str | None = None,
            restaurant_id: str | None = None,
            limit: int = 200,
    ) -> BidsEntries:
        """Deduplicated bid entries with status tracking."""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if ingredient:
            params["ingredient"] = ingredient
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        data = self._get("/api/history/bids/entries", params)
        return _parse_bids_entries(data)

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
        print(c.dish_history("Cosmic Synchrony: Il Destino di Pulsar"))

        print(c.ingredient_history("Essenza di Tachioni"))

        print(c.bids_history())
        # ── List all ingredients ──────────────────────────────────────
        # ingredients = c.market_ingredients()
        # print(f"\n{'='*60}")
        # print(f"  {len(ingredients)} ingredients on the market")
        # print(f"{'='*60}")
        # for name in ingredients:
        #     print(f"  • {name}")
        #
        # if not ingredients:
        #     print("No ingredients found — is the server running?")
        #     sys.exit(1)
        #
        # sample = ingredients[0]
        #
        # # ── Ingredient history (aggregated per dump) ──────────────────
        # print(f"\n{'='*60}")
        # print(f"  History: {sample}")
        # print(f"{'='*60}")
        # h = c.ingredient_history(sample)
        # print(f"  Trend:     {h.summary.price_trend}")
        # print(f"  Avg price: {h.summary.overall_avg_price}")
        # print(f"  Min price: {h.summary.overall_min_price}")
        # print(f"  Max price: {h.summary.overall_max_price}")
        # print(f"  Snapshots: {h.count}")
        # for snap in h.series[-3:]:
        #     print(f"    {snap.ts}  avg={snap.avg_unit_price}  vol={snap.total_volume}  Δ={snap.price_delta}")
        #
        # # ── Ingredient entries (individual orders) ────────────────────
        # print(f"\n{'='*60}")
        # print(f"  Entries: {sample}")
        # print(f"{'='*60}")
        # e = c.ingredient_entries(sample)
        # print(f"  Total orders: {e.total}")
        # print(f"  By status:    {e.summary.by_status}")
        # print(f"  By side:      {e.summary.by_side}")
        # for entry in e.entries[:5]:
        #     print(f"    #{entry.id}  {entry.side}  {entry.unit_price:.4f}/u  "
        #           f"qty={entry.quantity}  {entry.status}  by {entry.restaurant_name}")
        #
        # # ── Ingredient prices (raw timeline) ──────────────────────────
        # print(f"\n{'='*60}")
        # print(f"  Prices: {sample}")
        # print(f"{'='*60}")
        # p = c.ingredient_prices(sample)
        # print(f"  Observations: {p.total}")
        # print(f"  Range: {p.summary.min_price} — {p.summary.max_price}")
        # for pt in p.timeline[-5:]:
        #     mine = " ★" if pt.is_mine else ""
        #     print(f"    {pt.ts}  {pt.side}  {pt.unit_price:.4f}/u  [{pt.status}]{mine}")
        #
        # # ── Restaurant history ────────────────────────────────────────
        # print(f"\n{'='*60}")
        # print(f"  My restaurant history")
        # print(f"{'='*60}")
        # r = c.restaurant_history()
        # print(f"  Restaurant ID: {r.restaurant_id}")
        # print(f"  Snapshots:     {r.count}")
        # for snap in r.series[-3:]:
        #     delta_info = ""
        #     if snap.delta and snap.delta.balance:
        #         delta_info = f"  Δbal={snap.delta.balance.diff:+.2f}"
        #     print(f"    {snap.ts}  bal={snap.balance}  rep={snap.reputation}{delta_info}")
        #
        # # ── Price board (compact view) ────────────────────────────────
        # print(f"\n{'='*60}")
        # print(f"  Price board: {sample}")
        # print(f"{'='*60}")
        # board = c.price_board(ingredients=[sample])
        # for snap in board.get(sample, [])[-3:]:
        #     print(f"  {snap.ts}")
        #     for o in snap.prices:
        #         print(f"    {o.restaurant:30s}  {o.side:4s}  {o.price:.4f}/u  qty={o.quantity}")
        #
        # # ── Dish board (all dishes) ───────────────────────────────────
        # print(f"\n{'='*60}")
        # print(f"  Dish board (all dishes, all restaurants)")
        # print(f"{'='*60}")
        # dboard = c.dish_board()
        # print(f"  {dboard.dish_count} dishes across {dboard.dumps_scanned} dumps")
        # for dname, entry in list(dboard.dishes.items())[:5]:
        #     s = entry.summary
        #     print(f"    {dname:40s}  {s.min_price:>8.2f} — {s.max_price:<8.2f}  "
        #           f"avg={s.avg_price:.2f}  ({s.restaurant_count} restaurants)")
        #
        # # ── Single dish history ───────────────────────────────────────
        # if dboard.dishes:
        #     sample_dish = next(iter(dboard.dishes))
        #     print(f"\n{'='*60}")
        #     print(f"  Dish history: {sample_dish}")
        #     print(f"{'='*60}")
        #     dh = c.dish_history(sample_dish)
        #     print(f"  Price range: {dh.summary.min_price} — {dh.summary.max_price}")
        #     print(f"  Avg price:   {dh.summary.avg_price}")
        #     print(f"  Restaurants: {', '.join(dh.summary.restaurants)}")
        #     print(f"  Changes:")
        #     for ch in dh.changes[-5:]:
        #         print(f"    {ch.ts}  {ch.restaurant_name:30s}  {ch.price:.2f}")
        #     print(f"  Per restaurant:")
        #     for rname, rb in dh.by_restaurant.items():
        #         print(f"    {rname:30s}  {rb.min_price:.2f} — {rb.max_price:.2f}  "
        #               f"avg={rb.avg_price:.2f}  current={rb.current_price:.2f}")
        #
        # # ── Bulk fetch all ingredients ────────────────────────────────
        # print(f"\n{'='*60}")
        # print(f"  Bulk fetch (all ingredients, with entries & prices)")
        # print(f"{'='*60}")
        # all_data = c.all_ingredients_history(include_entries=True, include_prices=True)
        #
        # # Export to JSON
        # output_path = "history_dump.json"
        # with open(output_path, "w", encoding="utf-8") as f:
        #     json.dump({name: to_dict(fh) for name, fh in all_data.items()}, f, ensure_ascii=False, indent=2)
        # print(f"\nSaved {len(all_data)} ingredients to {output_path}")