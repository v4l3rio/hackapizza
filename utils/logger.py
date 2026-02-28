import logging
import sys
from datetime import datetime
from pathlib import Path

from opentelemetry import trace

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)

_logger = logging.getLogger("hackapizza")

# In-memory buffer for all log messages
_log_buffer: list[str] = []

# Logs directory
_LOGS_DIR = Path("logs")


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
    formatted = f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] {msg}"
    _logger.info(formatted)
    _log_buffer.append(formatted)
    _add_span_event("log", phase, turn, tag, msg)


def log_error(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    formatted = f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] ERROR: {msg}"
    _logger.error(formatted)
    _log_buffer.append(formatted)
    _add_span_event("log.error", phase, turn, tag, msg)


def dump_logs(turn_id: int) -> Path:
    """
    Dump all buffered log messages to a file and clear the buffer.

    Creates a 'logs' directory if it doesn't exist.
    File naming: logs/turn-{turn_id}-{timestamp_YYYYMMDD_HHMM}.log

    Returns the Path to the created file.
    """
    # Create logs directory if it doesn't exist
    _LOGS_DIR.mkdir(exist_ok=True)

    # Generate filename with turn_id and timestamp (till minute)
    now = datetime.utcnow()
    timestamp_str = now.strftime("%Y%m%d_%H%M")
    filename = f"turn-{turn_id}-{timestamp_str}.log"
    filepath = _LOGS_DIR / filename

    # Write buffer to file
    if _log_buffer:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(_log_buffer))
        _logger.info(f"[LOG DUMP] Saved {len(_log_buffer)} entries to {filepath}")
    else:
        # Even if empty, create the file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("")
        _logger.info(f"[LOG DUMP] Created empty log file {filepath}")

    # Clear the buffer
    _log_buffer.clear()

    return filepath
