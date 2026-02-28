import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)

_logger = logging.getLogger("hackapizza")


def log(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    _logger.info(f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] {msg}")


def log_error(phase: str, turn: int | str, tag: str, msg: str) -> None:
    timestamp = datetime.utcnow().strftime("%H:%M:%S")
    _logger.error(f"[{timestamp}][{phase.upper()}][T{turn}][{tag}] ERROR: {msg}")
