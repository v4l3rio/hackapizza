import logging
import sys
from datetime import datetime

from opentelemetry import trace

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)

_logger = logging.getLogger("hackapizza")


def _add_span_event(name: str, phase: str, turn: int | str, tag: str, msg: str) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, {
            "phase": phase,
            "turn": str(turn),
            "tag": tag,
            "message": msg,
        })


def log(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    _logger.info(f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] {msg}")
    _add_span_event("log", phase, turn, tag, msg)


def log_error(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    _logger.error(f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] ERROR: {msg}")
    _add_span_event("log.error", phase, turn, tag, msg)
