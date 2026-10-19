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
    """Config-aware env lookup (Hermes secrets first, then os.environ).

    Uses Hermes' own ``get_provider_env`` when available so profile-scoped
    secrets stay scoped. Do not swallow ``UnscopedSecretError``: falling back
    to raw ``os.environ`` under a multiplexed profile would leak another
    profile's credential.
    """

    try:
        from agent.web_search_provider import get_provider_env
    except ImportError:
        get_provider_env = None
    if get_provider_env is not None:
        return get_provider_env(name)

    value: Any = None
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        get_env_value = None
    if get_env_value is not None:
        value = get_env_value(name)
    if value is None:
        value = os.environ.get(name, "")
    return str(value or "").strip()


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_base_url(
    raw: str, *, default: str, official_host: str, env_name: str
) -> str:
    if not raw:
        return default
    try:
        parts = urlsplit(raw)
    except ValueError:
        logger.warning("ignoring invalid %s", env_name)
        return default
    host = (parts.hostname or "").lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    allow_loopback = os.environ.get(
        "SEARCH_ROUTER_ALLOW_LOOPBACK", ""
    ).strip().lower() in {"1", "true", "yes", "on"} or os.environ.get(
        "HERMES_DEV"
    ) == "1"
    try:
        port = parts.port
    except ValueError:
        logger.warning("ignoring invalid port in %s", env_name)
        return default
    safe = False
    if not parts.username and not parts.password:
        if parts.scheme == "https" and host == official_host and port in (None, 443):
            safe = parts.path in ("", "/")
        elif parts.scheme in ("http", "https") and loopback and allow_loopback:
            safe = True
    if not safe:
        # Never log the full URL: an override could embed credentials in userinfo.
        logger.warning(
            "ignoring unsafe %s override (host=%r); expected https://%s "
            "or a loopback host for tests",
            env_name,
            host or "?",
            official_host,
        )
        return default
    return raw.rstrip("/")


def deepseek_base_url() -> str:
    return _safe_base_url(
        env_value("DEEPSEEK_BASE_URL"),
        default=DEEPSEEK_DEFAULT,
        official_host="api.deepseek.com",
        env_name="DEEPSEEK_BASE_URL",
    )


def anysearch_base_url() -> str:
    return _safe_base_url(
        env_value("ANYSEARCH_URL"),
        default=ANYSEARCH_DEFAULT,
        official_host="api.anysearch.com",
        env_name="ANYSEARCH_URL",
    )


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
