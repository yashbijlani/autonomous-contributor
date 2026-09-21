"""Structured logging setup."""
from __future__ import annotations

import json
import logging
import sys

_configured = False


def setup_logging(level: str = "INFO", json_logs: bool = False) -> logging.Logger:
    global _configured
    logger = logging.getLogger("contributor")
    if _configured:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(logging.Formatter('{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.handlers = [handler]
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str = "contributor") -> logging.Logger:
    return logging.getLogger(name)
