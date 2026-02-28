# Ristorante Dashboard — Server

A lightweight Python web service (zero external dependencies) that:
- Serves the dashboard HTML at `http://localhost:8765/`
- Schedules periodic dumps of all upstream API data
- Computes field-level deltas between each dump
- Exposes a clean REST API for agents and tooling

---

## Quick Start

```bash
# Option A: env vars
BASE_URL="https://your-api-url.com" API_KEY="your-key" python3 server.py

# Option B: start server, configure via dashboard UI or API
python3 server.py
# then POST /api/config (see below)
```

Open http://localhost:8765 in your browser.

---

## Environment Variables

| Variable             | Default | Description                        |
|----------------------|---------|------------------------------------|
| `BASE_URL`           | —       | Upstream API base URL              |
| `API_KEY`            | —       | Value for `x-api-key` header       |
| `MY_RESTAURANT_ID`   | `5`     | Your restaurant ID                 |
| `REFRESH_INTERVAL`   | `30`    | Dump interval in seconds           |
| `PORT`               | `8765`  | HTTP port                          |

---

## REST API Reference

### Configuration

**GET /api/config**
Returns current config (API key redacted).

**POST /api/config**
```json
{ "base_url": "https://...", "api_key": "...", "refresh_interval": 30 }
```
Triggers an immediate dump after updating.

**POST /api/refresh**
Triggers an immediate dump without changing config.

---

### Status & Snapshots

**GET /api/status**
Full current snapshot + deltas vs previous dump.
```json
{
  "ts": "2026-02-28T14:00:00Z",
  "prev_ts": "2026-02-28T13:59:30Z",
  "data": { "restaurant": {...}, "market": [...], "restaurants": [...], "recipes": [...] },
  "delta": {
    "restaurant": { "balance": { "prev": 3500, "curr": 4000, "diff": 500, "pct": 14.29, "changed": true } },
    "market": { "added": [...], "removed": [...], "changed": [...], "_summary": {...} },
    "restaurants": { "7": { "balance": { ... } } }
  }
}
```

**GET /api/dump/history**
List of all dump timestamps (up to 100).

**GET /api/dump/latest**
Raw latest dump (data + delta).

**GET /api/dump/previous**
Raw previous dump.

---

### My Restaurant

**GET /api/restaurant**
My restaurant info + menu + delta since last dump.

**GET /api/menu**
My current menu items.

---

### Market

**GET /api/market**
All market entries grouped by ingredient, sorted alphabetically.
Each group is sorted by unit price ascending.
```json
{
  "ts": "...",
  "grouped": {
    "Alghe Bioluminescenti": [
      { "id": 42, "side": "BUY", "unit_price": 45.0, "is_mine": false, "restaurant_name": "Siesta dopo pizza", ... }
    ]
  },
  "delta": { "added": [...], "removed": [...], "changed": [...], "_summary": {...} },
  "summary": { "total_entries": 12, "unique_ingredients": 5, "my_entries": 2 }
}
```

**GET /api/market?side=BUY** or **?side=SELL**
Filter grouped entries by side.

**GET /api/market/{ingredient}**
All entries for a specific ingredient (URL-encoded). Falls back to case-insensitive partial match.
```
GET /api/market/Alghe%20Bioluminescenti
GET /api/market/alghe        ← partial match works too
```
```json
{
  "ingredient": "Alghe Bioluminescenti",
  "entries": [...],
  "best_unit_price": 45.0,
  "total_quantity": 15
}
```

---

### Other Restaurants

**GET /api/restaurants**
All restaurants with embedded `_delta` field per restaurant.

**GET /api/recipes**
All recipes.

---

## Delta Format

Numeric fields:
```json
{ "prev": 3500, "curr": 4000, "diff": 500, "pct": 14.29, "changed": true }
```

Non-numeric fields:
```json
{ "prev": false, "curr": true, "changed": true }
```

Inventory changes:
```json
{ "Radici di Gravità": { "prev": null, "curr": 1 } }
```

Market delta summary:
```json
{
  "added": [...full entry objects...],
  "removed": [...],
  "changed": [{ "id": 42, "field": "status", "prev": "open", "curr": "cancelled" }],
  "_summary": { "added_count": 2, "removed_count": 1, "changed_count": 0 }
}
```
