"""Hermes Search Router plugin.

The import stays lazy so tooling (self-check, tests) can import the config and
backend modules without a full Hermes installation.
"""

from __future__ import annotations

import logging

__version__ = "0.4.0"
logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Plugin entry point. Called once by the Hermes plugin loader."""
    from .provider import SearchRouterProvider

    ctx.register_web_search_provider(SearchRouterProvider())
    try:
        from .admin import register_cli

        register_cli(ctx)
    except Exception as exc:  # noqa: BLE001 - CLI is optional, search is not
        logger.warning("search-router: CLI registration skipped: %s", exc)
