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

    Connection is exit-on-drop (matches reference client behaviour).
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
            return
        for handler in handlers:
            try:
                await handler(data)
            except Exception as exc:
                log_error("sse", 0, "dispatch", f"Handler error for '{event_type}': {exc}")

    async def listen(self, max_retries: int = 10, base_delay: float = 1.0, max_delay: float = 60.0) -> None:
        """Open the SSE connection and process events, reconnecting on drop."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        attempt = 0

        while True:
            try:
                log("sse", 0, "connect", f"Connecting to {self.url} (attempt {attempt + 1})")
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self.url, headers=self.headers) as response:
                        response.raise_for_status()
                        log("sse", 0, "connect", f"Connected (HTTP {response.status})")
                        attempt = 0  # reset on successful connection

                        async for raw_line in response.content:
                            await self._handle_line(raw_line)

                log("sse", 0, "connect", "Connection closed by server — reconnecting...")

            except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
                attempt += 1
                if attempt > max_retries:
                    log_error("sse", 0, "connect", f"Max retries ({max_retries}) exceeded. Giving up.")
                    raise

                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                log_error("sse", 0, "connect", f"Connection error ({exc.__class__.__name__}: {exc}). Retry {attempt}/{max_retries} in {delay:.1f}s...")
                await asyncio.sleep(delay)

            except Exception as exc:
                log_error("sse", 0, "connect", f"Unexpected error: {exc}")
                raise

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
                log("sse", 0, "connect", "Handshake received")
                return

        try:
            event_json = json.loads(line)
        except json.JSONDecodeError:
            log_error("sse", 0, "raw", f"Could not parse: {line}")
            return

        event_type = event_json.get("type", "unknown")
        event_data = event_json.get("data", {})

        if not isinstance(event_data, dict):
            event_data = {"value": event_data}

        await self.dispatch(event_type, event_data)
