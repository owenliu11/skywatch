"""Logging setup.

Phase 1 read `LOG_LEVEL` from settings but nothing applied it, so the
scheduler's decision lines (the ones worth screenshotting for the README)
were dropped under uvicorn's default root level. `configure_logging` is
called once at the top of the FastAPI lifespan and again from the standalone
`python -m app.migrate` entrypoint.
"""

from __future__ import annotations

import logging

from app.config import settings

_configured = False

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging() -> None:
    """Route logs to stdout: `app.*` at LOG_LEVEL, everything else at WARNING.

    Idempotent. Configures the root handler (so `python -m app.migrate` and
    third-party warnings surface) rather than only the `app` logger. uvicorn's
    default config touches only its own loggers, so this is not swallowed.
    """

    global _configured
    if _configured:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=logging.WARNING,
        format=_FORMAT,
        datefmt=_DATEFMT,
        force=True,
    )
    logging.getLogger("app").setLevel(level)

    _configured = True
