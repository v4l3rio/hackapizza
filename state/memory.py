from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from infrastructure.http_client import HttpClient


@dataclass
class StrategyMemory:
    bid_history: list[dict[str, Any]] = field(default_factory=list)
    clearing_prices: dict[str, float] = field(default_factory=dict)
    served_orders: list[dict[str, Any]] = field(default_factory=list)
    revenue_per_turn: dict[int, float] = field(default_factory=dict)

    async def consolidate(self, http: "HttpClient") -> None:
        """
        Aggregate clearing prices from /bid_history.
        Called at the end of each turn (on 'stopped' event or at bidding phase start).
        """
        history = await http.get_bid_history()
        self.bid_history = history

        # Build clearing price map: ingredient -> latest clearing price
        for entry in history:
            ingredient = entry.get("ingredient") or entry.get("name")
            clearing = entry.get("clearing_price") or entry.get("price")
            if ingredient and clearing is not None:
                self.clearing_prices[ingredient] = float(clearing)

    def bid_for(self, ingredient: str, flat: float) -> float:
        """Return bid price for ingredient based on history or flat default."""
        if ingredient in self.clearing_prices:
            from config import BID_CLEARING_MULTIPLIER
            return self.clearing_prices[ingredient] * BID_CLEARING_MULTIPLIER
        return flat
