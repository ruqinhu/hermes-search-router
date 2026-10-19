"""Hermes Search Router plugin.

The import stays lazy so tooling (self-check, tests) can import the config and
backend modules without a full Hermes installation.
"""

from __future__ import annotations

__version__ = "0.2.0"


def register(ctx) -> None:
    """Plugin entry point. Called once by the Hermes plugin loader."""
    from .provider import SearchRouterProvider

    ctx.register_web_search_provider(SearchRouterProvider())
