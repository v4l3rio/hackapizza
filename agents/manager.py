from __future__ import annotations

from typing import Any

from config import WEB_APP_URL
from infrastructure.history_client import HistoryClient
from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.http_client import HttpClient
from infrastructure.mcp_client import MCPClient
from infrastructure.sse_listener import SSEListener
from agents.speaking import SpeakingAgent
from agents.bidding import BiddingAgent
from agents.menu import MenuAgent
from agents.market import MarketAgent
from agents.serving import ServingAgent
from agents.recipe_strategy import RecipeStrategyAgent
from utils.logger import log, log_error
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
        sse: SSEListener,
    ) -> None:
        self.state = state
        self.memory = memory
        self.http = http
        self.mcp = mcp
        self.sse = sse

        self._speaking = SpeakingAgent()
        self._bidding = BiddingAgent()
        self._menu = MenuAgent()
        self._market = MarketAgent()
        self._serving = ServingAgent()
        self._strategy = RecipeStrategyAgent(http)

        # Register persistent SSE handlers (serving events span the whole session)
        self._serving.register(sse, state, memory, mcp, http)

        # Register game lifecycle handlers
        sse.on("heartbeat", self._on_heartbeat)
        sse.on("game_started", self._on_game_started)
        sse.on("game_phase_changed", self._on_phase_changed)
        sse.on("game_reset", self._on_game_reset)
        sse.on("message", self._on_message)
        sse.on("new_message", self._on_new_message)

    async def _on_heartbeat(self, data: dict[str, Any]) -> None:
        turn_id = data.get("turn_id", 0)
        # log("manager", int(turn_id), "heartbeat", f"Heartbeat received")


    async def _on_game_started(self, data: dict[str, Any]) -> None:
        turn_id = data.get("turn_id", 0)
        self.state.turn_id = int(turn_id)
        with HistoryClient(WEB_APP_URL) as c:
            c.set_turn(self.state.turn_id)
        log("manager", self.state.turn_id, "turn", f"Game started — turn {self.state.turn_id}")
        await self._run_strategy()

    async def _on_game_reset(self, data: dict[str, Any]) -> None:
        log("manager", self.state.turn_id, "reset", f"Game reset: {data}")
        self.state.turn_id = 0
        self.state.phase = "unknown"
        await self._run_strategy()

    async def _run_strategy(self) -> None:
        """Run RecipeStrategyAgent to pick focus recipes for this game session."""
        try:
            clearing_prices = self.memory.clearing_prices if self.memory.clearing_prices else None
            strategy = await self._strategy.execute(clearing_prices=clearing_prices)
            self.memory.focus_recipes = [r["name"] for r in strategy if r.get("name")]
            log("manager", self.state.turn_id, "strategy", f"Focus recipes: {self.memory.focus_recipes}")
        except Exception as exc:
            log_error("manager", self.state.turn_id, "strategy", f"Strategy selection failed: {exc}")

    async def _on_message(self, data: dict[str, Any]) -> None:
        """Broadcast message (e.g. market entry created by another team)."""
        sender = data.get("sender", "unknown")
        text = data.get("payload", "")
        log("manager", self.state.turn_id, "message", f"Broadcast from {sender}: {text}")

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

        # Refresh state before dispatching
        try:
            await self.state.refresh_all(self.http)
            log(
                "manager",
                self.state.turn_id,
                "state",
                f"Balance={self.state.balance:.2f} | Inventory={self.state.inventory}",
            )
        except Exception as exc:
            log_error("manager", self.state.turn_id, "refresh", f"State refresh failed: {exc}")

        await self._dispatch(phase)

    async def _dispatch(self, phase: str) -> None:
        with tracer.start_as_current_span(f"manager.dispatch.{phase}") as span:
            span.set_attribute("phase", phase)
            span.set_attribute("turn_id", self.state.turn_id)
            try:
                if phase == "speaking":
                    await self._speaking.execute(self.state, self.memory, self.mcp)

                elif phase == "closed_bid":
                    # Consolidate memory from the PREVIOUS turn's bid results before bidding.
                    # (turn_id - 1 so we fetch already-settled history, not the current auction.)
                    # When turn_id == 1 this becomes 0, which returns all history — fine for T1.
                    try:
                        await self.memory.consolidate(self.http, self.state.turn_id - 1)
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "memory", f"Memory consolidate failed: {exc}")

                    await self._bidding.execute(self.state, self.memory, self.mcp)

                elif phase == "waiting":
                    await self._menu.execute(self.state, self.memory, self.mcp)
                    await self._market.execute_waiting(self.state, self.memory, self.mcp)
                    try:
                        await self.mcp.update_restaurant_is_open(True)
                        log("manager", self.state.turn_id, "phase", "Restaurant opened")
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "open", f"Failed to open restaurant: {exc}")

                elif phase == "serving":
                    await self._serving.execute(self.state, self.memory, self.mcp)
                    await self._market.execute_serving(self.state, self.memory, self.mcp, self.http)

                elif phase == "stopped":
                    log("manager", self.state.turn_id, "phase", "Game stopped / turn ended")
                    try:
                        await self.mcp.update_restaurant_is_open(False)
                        log("manager", self.state.turn_id, "phase", "Restaurant closed")
                    except Exception as exc:
                        log_error("manager", self.state.turn_id, "open", f"Failed to close restaurant: {exc}")
                    if self.state.turn_id > 0:
                        try:
                            await self.memory.consolidate(self.http, self.state.turn_id)
                        except Exception as exc:
                            log_error("manager", self.state.turn_id, "memory", f"Final consolidate failed: {exc}")

                else:
                    log("manager", self.state.turn_id, "phase", f"Unknown phase '{phase}' — ignoring")

            except Exception as exc:
                span.record_exception(exc)
                log_error("manager", self.state.turn_id, "dispatch", f"Agent error in phase '{phase}': {exc}")
