from __future__ import annotations

import asyncio
import json
from typing import Callable, Any, Awaitable

import aiohttp

from utils.logger import log, log_error


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SSEListener:
    """
    Connects to the game SSE endpoint and dispatches events.

    Wire format (per reference client):
        data: {"type": "event_name", "data": {...}}

    Each `data:` line carries a JSON object with `type` and `data` fields.
    A special `data: connected` line signals a successful handshake — not an event.

    Reconnects automatically on disconnect or 409 (another connection already active).
    A 409 means a previous connection is still alive on the server; we wait and retry.
    """

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        self._handlers: dict[str, list[EventHandler]] = {}

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Register an async handler for a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def dispatch(self, event_type: str, data: dict[str, Any]) -> None:
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            log("SSE", "?", "dispatch", f"No handler for '{event_type}': {data}")
            return
        for handler in handlers:
            try:
                await handler(data)
            except Exception as exc:
                log_error("SSE", "?", "dispatch", f"Handler error for '{event_type}': {exc}")

    async def listen(self) -> None:
        """
        Open the SSE connection and process events.
        Retries with backoff on disconnect or 409 (conflict).
        """
        retry_delay = 5  # seconds
        max_retry_delay = 60

        while True:
            try:
                await self._connect_once()
                # Connection closed cleanly — retry immediately
                log("SSE", "?", "connect", "Connection closed — reconnecting in 2s")
                await asyncio.sleep(2)
                retry_delay = 5  # reset backoff

            except aiohttp.ClientResponseError as exc:
                if exc.status == 409:
                    log(
                        "SSE",
                        "?",
                        "connect",
                        f"409 Conflict: another connection active. Retrying in {retry_delay}s...",
                    )
                elif exc.status == 401:
                    log_error("SSE", "?", "connect", "401 Unauthorized — check API key. Stopping.")
                    raise  # Fatal — don't retry
                else:
                    log_error("SSE", "?", "connect", f"HTTP {exc.status} error: {exc}. Retrying in {retry_delay}s...")

                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log_error("SSE", "?", "connect", f"Connection error: {exc}. Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    async def _connect_once(self) -> None:
        """Open one SSE connection and read until it closes."""
        log("SSE", "?", "connect", f"Connecting to {self.url}")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self.url, headers=self.headers) as response:
                response.raise_for_status()
                log("SSE", "?", "connect", f"Connected (HTTP {response.status})")

                async for raw_line in response.content:
                    await self._handle_line(raw_line)

    async def _handle_line(self, raw_line: bytes) -> None:
        if not raw_line:
            return

        line = raw_line.decode("utf-8", errors="ignore").strip()
        if not line:
            return

        # Strip "data:" prefix if present (standard SSE format)
        if line.startswith("data:"):
            line = line[5:].strip()
            # Handshake sentinel — not a real event
            if line == "connected":
                log("SSE", "?", "connect", "Handshake received")
                return

        try:
            event_json = json.loads(line)
        except json.JSONDecodeError:
            log("SSE", "?", "raw", f"Could not parse: {line}")
            return

        event_type = event_json.get("type", "unknown")
        event_data = event_json.get("data", {})

        if not isinstance(event_data, dict):
            event_data = {"value": event_data}

        log("SSE", "?", "event", f"← {event_type}: {event_data}")
        await self.dispatch(event_type, event_data)
