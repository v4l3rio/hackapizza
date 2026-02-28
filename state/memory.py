from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from infrastructure.http_client import HttpClient


@dataclass
class StrategyMemory:
    bid_history: list[dict[str, Any]] = field(default_factory=list)
    clearing_prices: dict[str, float] = field(default_factory=dict)
    served_orders: list[dict[str, Any]] = field(default_factory=list)
    revenue_per_turn: dict[int, float] = field(default_factory=dict)
    focus_recipes: list[str] = field(default_factory=list)  # recipe names chosen by strategy agent
    news_insights: list[dict[str, Any]] = field(default_factory=list)  # from NewsWatcherAgent

    def get_news_context(self, max_items: int = 5) -> str:
        """Stringa formattata degli insight per i prompt degli altri agenti."""
        if not self.news_insights:
            return ""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        top = sorted(
            self.news_insights,
            key=lambda x: (priority_order.get(x.get("priority", "low"), 2), -x.get("recorded_at", 0)),
        )[:max_items]
        lines = ["=== Notizie recenti da 'Cronache dal Cosmo' ==="]
        for ins in top:
            pri = ins.get("priority", "?").upper()
            line = f"[{pri}] {ins.get('headline', '')}"
            if ins.get("ingredients_affected"):
                line += f" | Ingredienti: {', '.join(ins['ingredients_affected'])}"
            if ins.get("actions"):
                line += f" | Azioni: {'; '.join(ins['actions'])}"
            lines.append(line)
        lines.append("=== Fine notizie ===")
        return "\n".join(lines)

    async def consolidate(self, http: HttpClient, turn_id: int = 0) -> None:
        """
        Aggregate clearing prices from /bid_history.
        Called at the end of each turn (on 'stopped' event or at bidding phase start).
        """
        history = await http.get_bid_history(turn_id)
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
