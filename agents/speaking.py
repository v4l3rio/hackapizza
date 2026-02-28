from __future__ import annotations

import json

from datapizza.agents import Agent
from datapizza.tools import tool

from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.mcp_client import MCPClient
from infrastructure.llm_factory import get_llm_client
from utils.logger import log, log_error
from utils.tracing import get_tracer

tracer = get_tracer(__name__)


class SpeakingAgent(Agent):
    """
    Handles the 'speaking' (negotiation) phase using LLM reasoning.

    Strategy:
      - Identifies ingredients we need and ingredients others might need from us
      - Sends targeted negotiation messages to other restaurants
      - Proposes ingredient swaps or purchases at mutually beneficial prices
    """

    name = "speaking_agent"
    system_prompt = (
        "You are the negotiation agent for our restaurant in a competitive cooking game. "
        "During the speaking phase you can send messages to other restaurants to negotiate "
        "ingredient trades and swaps. "
        "Analyze what we need and what others might have (based on their visible menus). "
        "Send concise, strategic messages proposing swaps or purchases. "
        "Use the send_message tool to contact other restaurants. "
        "If no beneficial negotiation is possible, do nothing."
    )

    def __init__(self) -> None:
        self._state: GameState | None = None
        self._strat: StrategyMemory | None = None
        self._mcp: MCPClient | None = None
        super().__init__(client=get_llm_client(), max_steps=5)

    # ------------------------------------------------------------------ tools

    @tool(
        name="send_message",
        description=(
            "Send a negotiation message to another restaurant. "
            "Provide the recipient's team ID (integer) and the message text. "
            "Use this to propose ingredient swaps, purchases, or alliances."
        ),
    )
    async def send_message(self, recipient_id: int, message: str) -> str:
        """Send a direct message to another restaurant's team."""
        try:
            result = await self._mcp.send_message(recipient_id, message)
            turn = self._state.turn_id if self._state else "?"
            log("speaking", turn, "tool", f"Message sent to team {recipient_id}: {result}")
            return f"Message sent to team {recipient_id}: {result}"
        except Exception as exc:
            turn = self._state.turn_id if self._state else "?"
            log_error("speaking", turn, "tool", f"send_message failed: {exc}")
            return f"Error sending message to team {recipient_id}: {exc}"

    # ------------------------------------------------------------------ phase entry

    async def execute(
        self,
        state: GameState,
        memory: StrategyMemory,
        mcp: MCPClient,
    ) -> None:
        self._state = state
        self._strat = memory
        self._mcp = mcp

        with tracer.start_as_current_span("speaking_agent.execute") as span:
            span.set_attribute("turn_id", state.turn_id)

            log("speaking", state.turn_id, "agent", "SpeakingAgent started")
            log(
                "speaking",
                state.turn_id,
                "state",
                f"Balance={state.balance:.2f} | Inventory={state.inventory}",
            )

            # Compute what we need
            needed: dict[str, int] = {}
            for recipe in state.recipes:
                for ing, qty in recipe.get("ingredients", {}).items():
                    have = state.inventory.get(ing, 0)
                    shortfall = max(0, qty - have)
                    if shortfall > 0:
                        needed[ing] = max(needed.get(ing, 0), shortfall)

            task = (
                f"Current inventory: {json.dumps(state.inventory)}\n"
                f"Ingredients we need (shortfall): {json.dumps(needed)}\n"
                f"Our recipes: {json.dumps(state.recipes)}\n"
                f"Other restaurants in the game: {json.dumps(state.restaurants)}\n"
                f"Last clearing prices: {json.dumps(memory.clearing_prices)}\n\n"
                "Decide whether to negotiate with any other restaurant. "
                "If we need ingredients and others might have surplus (based on their menus), "
                "send a targeted message proposing a trade. "
                "Keep messages short and business-like. "
                "Only send messages if there is a clear strategic benefit."
            )

            try:
                await self.a_run(task)
            except Exception as exc:
                span.record_exception(exc)
                log_error("speaking", state.turn_id, "agent", f"SpeakingAgent failed: {exc}")
