"""Structured logging setup (structlog).

JSON output with ISO timestamps and log levels, suitable for shipping Job/Stage
logs to a collector (SPEC §8). Use :func:`configure_logging` once at process
start, then :func:`get_logger` to obtain a bound logger. Per-Job/Stage context
is attached with the standard structlog ``logger.bind(job_id=..., stage=...)``.
"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import Processor

_configured = False


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog for JSON (or console) structured output.

    Idempotent-ish: safe to call multiple times; the last call wins.

    Args:
        level: Minimum log level name (e.g. ``"INFO"``, ``"DEBUG"``).
        json_output: Emit JSON lines (default) or human-readable console output.
    """
    global _configured

    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    # Send everything through the stdlib root logger so library logs and our
    # structlog logs share one stream/level.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configuring defaults on first use.

    Args:
        name: Optional logger name, bound as the ``logger`` key.
    """
    if not _configured:
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if name is not None:
        logger = logger.bind(logger=name)
    return logger


def bind_job_context(
    logger: structlog.stdlib.BoundLogger,
    *,
    job_id: str,
    stage: str | None = None,
) -> structlog.stdlib.BoundLogger:
    """Return a logger bound with ``job_id`` (and optional ``stage``).

    Convenience for the orchestrator/services to correlate logs per Job/Stage
    (SPEC §8, T-13.1).
    """
    bound: structlog.stdlib.BoundLogger = logger.bind(job_id=job_id)
    if stage is not None:
        bound = bound.bind(stage=stage)
    return bound
