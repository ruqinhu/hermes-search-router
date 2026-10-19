"""Environment lookup and backend construction."""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

from .backends.anysearch import DEFAULT_BASE_URL as ANYSEARCH_DEFAULT
from .backends.anysearch import AnySearchBackend
from .backends.base import RateLimiter
from .backends.deepseek import DEFAULT_BASE_URL as DEEPSEEK_DEFAULT
from .backends.deepseek import DeepSeekBackend
from .backends.script import ScriptBackend
from .config import BackendConfig, RouteConfig

logger = logging.getLogger(__name__)


def env_value(name: str) -> str:
    """Config-aware env lookup (Hermes secrets first, then os.environ)."""

    value: Any = None
    try:
        from hermes_cli.config import get_env_value

        value = get_env_value(name)
    except Exception:  # noqa: BLE001 - config layer is optional for tooling
        value = None
    if value is None:
        value = os.environ.get(name, "")
    return str(value or "").strip()


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def deepseek_base_url() -> str:
    raw = env_value("DEEPSEEK_BASE_URL")
    if not raw:
        return DEEPSEEK_DEFAULT
    try:
        parts = urlsplit(raw)
    except ValueError:
        logger.warning("ignoring invalid DEEPSEEK_BASE_URL")
        return DEEPSEEK_DEFAULT
    host = (parts.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    safe = False
    if not parts.username and not parts.password:
        if parts.scheme == "https" and host == "api.deepseek.com":
            safe = parts.path in ("", "/")
        elif parts.scheme in ("http", "https") and loopback:
            safe = True
    if not safe:
        logger.warning(
            "ignoring DEEPSEEK_BASE_URL=%r; expected https://api.deepseek.com "
            "or a loopback host for tests",
            raw,
        )
        return DEEPSEEK_DEFAULT
    return raw.rstrip("/")


def anysearch_base_url() -> str:
    raw = env_value("ANYSEARCH_URL")
    return (raw or ANYSEARCH_DEFAULT).rstrip("/")


def build_backend(
    config: BackendConfig,
    *,
    client: Any = None,
    sleep=time.sleep,
):
    if config.type == "deepseek":
        return DeepSeekBackend(
            api_key=env_value("DEEPSEEK_API_KEY"),
            base_url=deepseek_base_url(),
            timeout=config.timeout,
            need_content=_truthy(env_value("DEEPSEEK_SEARCH_NEED_CONTENT")),
            client=client,
            limiter=RateLimiter(0.21, sleep=sleep),
            sleep=sleep,
        )
    if config.type == "anysearch":
        return AnySearchBackend(
            api_key=env_value("ANYSEARCH_API_KEY"),
            base_url=anysearch_base_url(),
            timeout=config.timeout,
            client=client,
            limiter=RateLimiter(0.0),
            sleep=sleep,
        )
    if config.type == "script":
        return ScriptBackend(config, sleep=sleep)
    raise ValueError(f"unknown backend type {config.type!r}")


def build_backends(
    config: RouteConfig,
    *,
    client: Any = None,
    sleep=time.sleep,
) -> dict[str, Any]:
    return {
        name: build_backend(backend, client=client, sleep=sleep)
        for name, backend in config.backends.items()
    }
