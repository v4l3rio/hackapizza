from __future__ import annotations

import asyncio
from typing import Any

from datapizza.tools import Tool
from datapizza.tools.mcp_client import MCPClient

from agents.bidding import BiddingAgent
from agents.customer_profiler import CustomerProfilerAgent, load_customer_profiles
from agents.market import MarketAgent
from agents.menu import MenuAgent
from agents.news_watcher import NewsWatcherAgent
from agents.serving import ServingAgent
from agents.speaking import SpeakingAgent
from config import DASHBOARD
from infrastructure.http_client import HttpClient
from infrastructure.sse_listener import SSEListener
from state.game_state import GameState
from state.memory import StrategyMemory
from state.planner import plan_next_n_recipies
from utils.logger import log, log_error, dump_logs
from utils.tracing import get_tracer

tracer = get_tracer(__name__)

KNOWN_PHASES = {"speaking", "closed_bid", "waiting", "serving", "stopped"}


class AgentManager:
    """
    Central router: receives SSE phase-change events,
    refreshes state, and dispatches to the correct agent.
    """

    def __init__(
        self,
        state: GameState,
        memory: StrategyMemory,
        http: HttpClient,
        mcp: MCPClient,
        mcp_tools: list[Tool],
        sse: SSEListener,
    ) -> None:
        self.state = state
        self.memory = memory
        self.http = http
        self.mcp = mcp
        self.sse = sse

        def filter_tools(names: list[str]) -> list[Tool]:
            return [t for t in mcp_tools if t.name in names]

        self._speaking = SpeakingAgent()
        self._bidding = BiddingAgent(mcp_tools=filter_tools(["closed_bid"]))
        self._menu = MenuAgent(mcp_tools=filter_tools(["save_menu"]))
        self._market = MarketAgent()
        self._serving = ServingAgent(mcp=mcp, mcp_tools=filter_tools(["prepare_dish", "serve_dish", "update_restaurant_is_open"]))
        self._news = NewsWatcherAgent()
        self._profiler = CustomerProfilerAgent()

        # Carica i profili cliente esistenti all'avvio
        memory.customer_profiles = load_customer_profiles()
        log("manager", 0, "profiler", f"Profili cliente caricati: {len(memory.customer_profiles)}")

        # Avvia subito il polling delle notizie in background (popola memory.news_insights)
        self._news.start(memory=memory)
        log("manager", 0, "news", "NewsWatcherAgent avviato in background")

        # Register persistent SSE handlers (serving events span the whole session)
        self._serving.register(sse, state, mcp, http)

        # Register game lifecycle handlers
        sse.on("game_started", self._on_game_started)
        sse.on("game_phase_changed", self._on_phase_changed)
        sse.on("game_reset", self._on_game_reset)
        sse.on("message", self._on_message)
        sse.on("new_message", self._on_new_message)


    async def _on_game_started(self, data: dict[str, Any]) -> None:
        turn_id = data.get("turn_id", 0)
        self.state.turn_id = int(turn_id)
        log("manager", self.state.turn_id, "turn", f"Game started — turn {self.state.turn_id}")
        try:
            await self.state.refresh_restaurants(self.http)
        except Exception as exc:
            log_error("manager", self.state.turn_id, "refresh", f"Restaurants refresh on game_started failed: {exc}")
        await self._speaking.execute(self.state, self.memory, self.mcp)

    async def _on_game_reset(self, data: dict[str, Any]) -> None:
        log("manager", self.state.turn_id, "reset", f"Game reset: {data}")
        self.state.turn_id = 0
        self.state.phase = "ND"

    async def _on_message(self, data: dict[str, Any]) -> None:
        """Broadcast message (e.g. market entry created by another team)."""
        sender = data.get("sender", "unknown")
        text = data.get("payload", "")
        # log("manager", self.state.turn_id, "message", f"Broadcast from {sender}: {text}")

    async def _on_new_message(self, data: dict[str, Any]) -> None:
        """Direct private message from another team (new_message SSE event)."""
        sender_id = data.get("senderId", "?")
        sender_name = data.get("senderName", "unknown")
        text = data.get("text", "")
        msg_id = data.get("messageId", "")
        log(
            "manager",
            self.state.turn_id,
            "new_message",
            f"DM from {sender_name} (id={sender_id}, msgId={msg_id}): {text}",
        )

    async def _on_phase_changed(self, data: dict[str, Any]) -> None:
        phase = data.get("phase", "unknown")
        turn_id = data.get("turn_id", self.state.turn_id)

        self.state.phase = phase
        self.state.turn_id = int(turn_id)

        log("manager", self.state.turn_id, "phase", f"→ {phase.upper()}")
        await self._dispatch(phase)

    async def _dispatch(self, phase: str) -> None:
        with tracer.start_as_current_span(f"manager.dispatch.{phase}") as span:
            span.set_attribute("phase", phase)
            span.set_attribute("turn_id", self.state.turn_id)
            try:
                if phase == "closed_bid":
                    # Consolidate memory from the PREVIOUS turn's bid results before bidding.
                    try:
                        await self.memory.consolidate(self.http, self.state.turn_id - 1)
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "memory", f"Memory consolidate failed: {exc}")

                    # Bidding only needs balance
                    try:
                        await self.state.refresh_info(self.http)
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "refresh", f"Info refresh failed: {exc}")

                    await self._bidding.execute(self.state, self.memory, self.http)

                elif phase == "waiting":
                    # Menu needs balance, reputation, inventory, recipes
                    try:
                        await asyncio.gather(
                            self.state.refresh_info(self.http),
                            self.state.refresh_recipes(self.http),
                        )
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "refresh", f"Pre-menu refresh failed: {exc}")

                    await self._menu.execute(self.state, self.memory)

                    # After save_menu, refresh menu + inventory for serving/market
                    try:
                        await asyncio.gather(
                            self.state.refresh_info(self.http),
                            self.state.refresh_menu(self.http),
                            self.state.refresh_recipes(self.http),
                        )
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "refresh", f"Post-menu refresh failed: {exc}")

                    await self._market.execute_waiting(self.state, self.memory, self.mcp)
                    try:
                        await self.mcp.call_tool("update_restaurant_is_open", {"is_open": True})
                        log("manager", self.state.turn_id, "phase", "Restaurant opened")
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "open", f"Failed to open restaurant: {exc}")

                elif phase == "serving":
                    # Serving needs inventory, menu, recipes
                    try:
                        await asyncio.gather(
                            self.state.refresh_info(self.http),
                            self.state.refresh_menu(self.http),
                            self.state.refresh_recipes(self.http),
                        )
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "refresh", f"Pre-serving refresh failed: {exc}")

                    await self._serving.execute(self.state)
                    await self._market.execute_serving(self.state, self.memory, self.mcp, self.http)

                elif phase == "stopped":
                    log_file = dump_logs(self.state.turn_id)
                    log("manager", self.state.turn_id, "turn", f"Logs saved to {log_file}")
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, DASHBOARD.run_dump, self.state.turn_id)

                    self._bidding.max_recipes = plan_next_n_recipies(self._bidding.max_recipes)
                    if self.state.turn_id > 0:
                        try:
                            await self.memory.consolidate(self.http, self.state.turn_id)
                        except Exception as exc:
                            log_error("manager", self.state.turn_id, "memory", f"Final consolidate failed: {exc}")
                    try:
                        new_count = await self._profiler.run_once(self.memory)
                        log("manager", self.state.turn_id, "profiler", f"{new_count} nuovi profili cliente aggiunti")
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "profiler", f"CustomerProfiler failed: {exc}")

                else:
                    log("manager", self.state.turn_id, "phase", f"Unknown phase '{phase}' — ignoring")

            except Exception as exc:
                span.record_exception(exc)
                log_error("manager", self.state.turn_id, "dispatch", f"Agent error in phase '{phase}': {exc}")
