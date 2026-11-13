"""Shared test fixtures."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "websockets", "httpx")


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Put the logging configuration back after every test.

    ``cli.main`` points the root logger at the stderr of the moment (which pytest
    closes after the test), and a ``uvicorn.Config`` built without ``log_config=None``
    applies uvicorn's own dictConfig, which stops ``uvicorn.access`` records from
    propagating. Either would leak into later tests: closed-stream logging errors, or
    log assertions that see nothing.
    """
    saved = {}
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (list(logger.handlers), logger.level, logger.propagate, logger.disabled)
    yield
    for name, (handlers, level, propagate, disabled) in saved.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
