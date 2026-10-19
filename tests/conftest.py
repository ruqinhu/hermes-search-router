"""Test bootstrap: stub the Hermes ABC and load the plugin as ``hsr``."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _install_hermes_stub() -> None:
    if "agent.web_search_provider" in sys.modules:
        return
    agent = types.ModuleType("agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    web = types.ModuleType("agent.web_search_provider")

    class WebSearchProvider:  # noqa: D401 - minimal test double
        pass

    web.WebSearchProvider = WebSearchProvider
    agent.web_search_provider = web  # type: ignore[attr-defined]
    sys.modules["agent"] = agent
    sys.modules["agent.web_search_provider"] = web


def _load_plugin():
    if "hsr" in sys.modules:
        return sys.modules["hsr"]
    _install_hermes_stub()
    spec = importlib.util.spec_from_file_location(
        "hsr",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hsr"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
