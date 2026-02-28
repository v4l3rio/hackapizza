"""
Ristorante Dashboard — History API Client
==========================================
Fetches complete history for all ingredients from the dashboard API.

Usage:
    python history_client.py [--base-url http://localhost:8765] [--output history_dump.json]
"""

import argparse
import json
import sys
import time
from urllib.parse import quote

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency: pip install httpx")


class HistoryClient:
    def __init__(self, base_url: str = "http://localhost:8765", timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict | None = None):
        resp = self.client.get(self.base_url + path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Core history routes ───────────────────────────────────────────

    def market_ingredients(self) -> list[str]:
        """Get list of all ingredient names from latest dump."""
        data = self._get("/api/dump/latest", {"field": "market.ingredients"})
        return data.get("value", [])

    def ingredient_history(self, name: str, limit: int = 100, side: str | None = None) -> dict:
        """Aggregated stats per dump for an ingredient."""
        params = {"limit": limit}
        if side:
            params["side"] = side
        return self._get(f"/api/history/ingredient/{quote(name, safe='')}", params)

    def ingredient_entries(self, name: str, limit: int = 100, side: str | None = None,
                           status: str | None = None, restaurant_id: str | None = None) -> dict:
        """Every individual market entry ever seen for an ingredient."""
        params = {"limit": limit}
        if side:
            params["side"] = side
        if status:
            params["status"] = status
        if restaurant_id:
            params["restaurant_id"] = restaurant_id
        return self._get(f"/api/history/ingredient-entries/{quote(name, safe='')}", params)

    def ingredient_prices(self, name: str, limit: int = 100, side: str | None = None) -> dict:
        """Raw price timeline for an ingredient."""
        params = {"limit": limit}
        if side:
            params["side"] = side
        return self._get(f"/api/history/ingredient-prices/{quote(name, safe='')}", params)

    def restaurant_history(self, restaurant_id: str | None = None, limit: int = 100) -> dict:
        """History of a restaurant's stats."""
        if restaurant_id:
            return self._get(f"/api/history/restaurant/{restaurant_id}", {"limit": limit})
        return self._get("/api/history/restaurant", {"limit": limit})

    # ── Bulk fetch ────────────────────────────────────────────────────

    def all_ingredients_history(self, limit: int = 100, side: str | None = None,
                                include_entries: bool = False,
                                include_prices: bool = False,
                                delay: float = 0.1) -> dict[str, dict]:
        """
        Fetch complete history for every ingredient on the market.

        Returns a dict keyed by ingredient name, each containing:
            - history:  aggregated per-dump stats
            - entries:  (optional) every individual market entry
            - prices:   (optional) raw price timeline
        """
        ingredients = self.market_ingredients()
        print(f"Found {len(ingredients)} ingredients")

        results = {}
        for i, name in enumerate(ingredients, 1):
            #print(f"  [{i}/{len(ingredients)}] {name}...", end=" ", flush=True)
            try:
                entry: dict = {"history": self.ingredient_history(name, limit, side)}
                if include_entries:
                    entry["entries"] = self.ingredient_entries(name, limit, side)
                if include_prices:
                    entry["prices"] = self.ingredient_prices(name, limit, side)
                results[name] = entry
                print("ok")
            except httpx.HTTPStatusError as e:
                print(f"error {e.response.status_code}")
                results[name] = {"error": str(e)}
            if delay and i < len(ingredients):
                time.sleep(delay)

        return results

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser(description="Fetch all ingredient histories")
    parser.add_argument("--base-url", default="http://localhost:8765")
    parser.add_argument("--output", "-o", default="history_dump.json")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--side", choices=["BUY", "SELL"], default=None)
    parser.add_argument("--entries", action="store_true", help="Include individual entries")
    parser.add_argument("--prices", action="store_true", help="Include price timeline")
    args = parser.parse_args()

    with HistoryClient(args.base_url) as client:
        data = client.all_ingredients_history(
            limit=args.limit,
            side=args.side,
            include_entries=args.entries,
            include_prices=args.prices,
        )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(data)} ingredients to {args.output}")


if __name__ == "__main__":
    main()