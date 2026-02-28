"""
Hackapizza — entrypoint.
Bootstraps config, creates all components, and starts the SSE event loop.
"""

import argparse
import asyncio

from opentelemetry import trace as otel_trace

import config
from datapizza.tracing import DatapizzaMonitoringInstrumentor
from state.game_state import GameState
from state.memory import StrategyMemory
from infrastructure.sse_listener import SSEListener
from infrastructure.http_client import HttpClient
from infrastructure.mcp_client import MCPClient
from agents.manager import AgentManager
from utils.logger import log

instrumentor = DatapizzaMonitoringInstrumentor(
    api_key=config.DATAPIZZA_MONITORING_API_KEY,
    project_id=config.DATAPIZZA_MONITORING_PROJECT_ID,
    endpoint=config.DATAPIZZA_MONITORING_OTLP_ENDPOINT,
)
instrumentor.instrument()
tracer = instrumentor.get_tracer(__name__)


async def sse_only() -> None:
    log("main", 0, "boot", "SSE-only mode — printing all events")
    log("main", 0, "boot", f"SSE_URL={config.SSE_URL}")

    sse_headers = {
        "x-api-key": config.TEAM_API_KEY,
        "Accept": "text/event-stream",
    }
    sse = SSEListener(url=config.SSE_URL, headers=sse_headers)
    await sse.listen()


async def main() -> None:
    log("main", 0, "boot", "Hackapizza agent starting")
    log("main", 0, "boot", f"TEAM_ID={config.TEAM_ID}")
    log("main", 0, "boot", f"BASE_URL={config.BASE_URL}")

    # Shared state
    state = GameState()
    memory = StrategyMemory()

    # Infrastructure
    http = HttpClient(
        base_url=config.BASE_URL,
        team_id=config.TEAM_ID,
        api_key=config.TEAM_API_KEY,
    )
    mcp = MCPClient(
        mcp_url=config.MCP_URL,
        team_id=config.TEAM_ID,
        api_key=config.TEAM_API_KEY,
    )

    sse_headers = {
        "x-api-key": config.TEAM_API_KEY,
        "Accept": "text/event-stream",
    }
    sse = SSEListener(url=config.SSE_URL, headers=sse_headers)

    # Wire up agent manager (registers all SSE handlers)
    AgentManager(state=state, memory=memory, http=http, mcp=mcp, sse=sse)

    log("main", 0, "boot", "All components initialized. Starting SSE listener...")

    await sse.listen()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-sse", action="store_true", help="Solo SSE listener, stampa gli eventi raw")
    args = parser.parse_args()

    try:
        if args.only_sse:
            asyncio.run(sse_only())
        else:
            with tracer.start_as_current_span("hackapizza_session"):
                asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[main] Interrupted by user. Goodbye!")
    finally:
        otel_trace.get_tracer_provider().force_flush()
