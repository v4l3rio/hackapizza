import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from threading import Lock

from opentelemetry import trace


# ==============================
# Timezone (Rome, DST aware)
# ==============================

ROME_TZ = ZoneInfo("Europe/Rome")


# ==============================
# Internal state
# ==============================

_LOGS_DIR = Path("logs")
_log_buffer: list[str] = []
_log_lock = Lock()


# ==============================
# Tee stream to capture ALL stdout/stderr
# ==============================

class TeeStream:
    """
    Duplicates writes to:
      - original stream (terminal)
      - in-memory log buffer
    """

    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, data: str):
        self.original_stream.write(data)

        if data.strip():
            timestamp = datetime.now(ROME_TZ).strftime("%H:%M:%S")
            with _log_lock:
                _log_buffer.append(f"[{timestamp}][STD] {data.rstrip()}")

    def flush(self):
        self.original_stream.flush()


# Redirect stdout and stderr immediately
sys.stdout = TeeStream(sys.stdout)
sys.stderr = TeeStream(sys.stderr)


# ==============================
# Logging config
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger("hackapizza")


# ==============================
# OpenTelemetry integration
# ==============================

def _add_span_event(name: str, phase: str, turn: int | str, tag: str, msg: str) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(
            name,
            {
                "phase": phase,
                "turn": str(turn),
                "tag": tag,
                "message": msg,
            },
        )


# ==============================
# Public logging API
# ==============================

def log(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.now(ROME_TZ).strftime("%H:%M:%S")
    formatted = f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] {msg}"

    _logger.info(formatted)
    _add_span_event("log", phase, turn, tag, msg)


def log_error(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.now(ROME_TZ).strftime("%H:%M:%S")
    formatted = f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] ERROR: {msg}"

    _logger.error(formatted)
    _add_span_event("log.error", phase, turn, tag, msg)


# ==============================
# Dump logs to file
# ==============================

def dump_logs(turn_id: int) -> Path:
    """
    Dump all buffered terminal output + logs to file and clear buffer.

    File naming:
        logs/turn-{turn_id}-{timestamp_YYYYMMDD_HHMMSS}.log
    """

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now(ROME_TZ)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    filename = f"turn-{turn_id}-{timestamp_str}.log"
    filepath = _LOGS_DIR / filename

    with _log_lock:
        logs_to_write = _log_buffer.copy()
        _log_buffer.clear()

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(logs_to_write))

    _logger.info(f"[LOG DUMP] Saved {len(logs_to_write)} entries to {filepath}")

    return filepath