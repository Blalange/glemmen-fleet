"""Logging configuration for the inventory collector."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from inventory_util import InventoryError

_ROOT = "glemmen.collector"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a collector logger, optionally scoped to a module name."""
    if not name:
        return logging.getLogger(_ROOT)
    suffix = name.removeprefix("collector.")
    return logging.getLogger(f"{_ROOT}.{suffix}")


def configure_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
) -> logging.Logger:
    """Configure collector logging to stderr and an optional file."""
    normalized = str(level).upper()
    if normalized not in _LEVELS:
        raise InventoryError(
            f"invalid log level {level!r}; expected one of {', '.join(sorted(_LEVELS))}"
        )

    logger = get_logger()
    logger.setLevel(normalized)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file:
        path = Path(log_file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, encoding="utf-8")
        except OSError as exc:
            raise InventoryError(f"cannot open log file {path}: {exc}") from exc
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
