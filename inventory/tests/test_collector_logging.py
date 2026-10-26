from __future__ import annotations

import logging
from pathlib import Path

import pytest
from collector.log import configure_logging, get_logger
from inventory_util import SyncError


@pytest.fixture
def root_logger() -> logging.Logger:
    logger = get_logger()
    yield logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


def test_child_logger_uses_collector_namespace() -> None:
    assert get_logger("collector.cli").name == "glemmen.collector.cli"
    assert get_logger("collector.sources.unifi").name == "glemmen.collector.sources.unifi"


def test_configure_logging_writes_stream_and_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    root_logger: logging.Logger,
) -> None:
    log_path = tmp_path / "collector.log"
    configure_logging("INFO", log_path)
    get_logger("collector.test").info("hello %s", "collector")
    for handler in root_logger.handlers:
        handler.flush()
    assert "hello collector" in capsys.readouterr().err
    assert "hello collector" in log_path.read_text(encoding="utf-8")


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(SyncError, match="invalid log level"):
        configure_logging("LOUD")
