"""Route configuration: parsing, validation, defaults, hot reload."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_FALLBACK = ("deepseek", "anysearch")
DEFAULT_EXTRACT_FALLBACK = ("deepseek", "anysearch")
KNOWN_TYPES = {"deepseek", "anysearch", "script"}
SCRIPT_MODES = {"canonical", "argv"}
ON_EMPTY_VALUES = {"fallback", "accept"}


class ConfigError(ValueError):
    """Invalid or unreadable route configuration."""


@dataclass(frozen=True)
class BackendConfig:
    name: str
    type: str
    timeout: float = 30.0

    # script-only fields
    mode: str = "canonical"
    command: tuple[str, ...] = ()
    output: str = "json"
    results_path: str = "results"
    fields: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    max_output_bytes: int = 1_000_000
    supports_extract: bool = False


@dataclass(frozen=True)
class RouteConfig:
    version: int
    active: str
    search_fallback: tuple[str, ...]
    extract_fallback: tuple[str, ...]
    on_empty: str
    search_total_timeout: float
    extract_total_timeout: float
    cooldown_seconds: float
    backends: dict[str, BackendConfig] = field(default_factory=dict)

    def backend_for(self, name: str) -> BackendConfig | None:
        return self.backends.get(normalize_backend_name(name))


def normalize_backend_name(name: Any) -> str:
    value = str(name or "").strip()
    if value.startswith("script:"):
        return value.split(":", 1)[1].strip()
    return value


def default_config_path() -> Path:
    override = os.environ.get("SEARCH_ROUTER_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
    return Path(home).expanduser() / "plugin-data" / "search-router" / "routes.yaml"


def default_config() -> RouteConfig:
    return RouteConfig(
        version=1,
        active="auto",
        search_fallback=DEFAULT_SEARCH_FALLBACK,
        extract_fallback=DEFAULT_EXTRACT_FALLBACK,
        on_empty="fallback",
        search_total_timeout=45.0,
        extract_total_timeout=100.0,
        cooldown_seconds=60.0,
        backends={
            "deepseek": BackendConfig(name="deepseek", type="deepseek"),
            "anysearch": BackendConfig(name="anysearch", type="anysearch"),
        },
    )


def load_config(path: str | Path | None = None) -> RouteConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        return default_config()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{config_path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping")
    return parse_config(raw, config_path)


def parse_config(raw: dict[str, Any], config_path: Path | None = None) -> RouteConfig:
    version = _positive_int(raw.get("version", 1), 1, "version")
    if version != 1:
        raise ConfigError(f"unsupported routes version {version}; expected 1")

    backends_raw = raw.get("backends")
    if backends_raw is None:
        backends_raw = {
            "deepseek": {"type": "deepseek"},
            "anysearch": {"type": "anysearch"},
        }
    if not isinstance(backends_raw, dict) or not backends_raw:
        raise ConfigError("backends must be a non-empty mapping")

    base_dir = config_path.parent if config_path else Path.cwd()
    backends: dict[str, BackendConfig] = {}
    for raw_name, raw_backend in backends_raw.items():
        name = str(raw_name).strip()
        if not name:
            raise ConfigError("backend names must not be blank")
        if raw_backend is None:
            raw_backend = {}
        if not isinstance(raw_backend, dict):
            raise ConfigError(f"backends.{name} must be a mapping")
        backends[name] = _parse_backend(name, raw_backend, base_dir)

    active = str(raw.get("active", "auto")).strip() or "auto"
    if active != "auto":
        active = normalize_backend_name(active)
        if active not in backends:
            raise ConfigError(
                f"active backend {active!r} is not defined in backends "
                f"({', '.join(sorted(backends))})"
            )

    fallback_raw = raw.get("fallback") or {}
    if not isinstance(fallback_raw, dict):
        raise ConfigError("fallback must be a mapping with search/extract lists")
    search_fallback = _parse_fallback(
        fallback_raw.get("search"), backends, "fallback.search"
    )
    extract_fallback = _parse_fallback(
        fallback_raw.get("extract"), backends, "fallback.extract"
    )

    on_empty = str(raw.get("on_empty", "fallback")).strip().lower()
    if on_empty not in ON_EMPTY_VALUES:
        raise ConfigError("on_empty must be 'fallback' or 'accept'")

    return RouteConfig(
        version=version,
        active=active,
        search_fallback=search_fallback,
        extract_fallback=extract_fallback,
        on_empty=on_empty,
        search_total_timeout=_positive_float(
            raw.get("search_total_timeout"), 45.0, "search_total_timeout"
        ),
        extract_total_timeout=_positive_float(
            raw.get("extract_total_timeout"), 100.0, "extract_total_timeout"
        ),
        cooldown_seconds=_nonnegative_float(
            raw.get("cooldown_seconds"), 60.0, "cooldown_seconds"
        ),
        backends=backends,
    )


def _parse_backend(
    name: str, raw: dict[str, Any], base_dir: Path
) -> BackendConfig:
    raw_type = raw.get("type")
    backend_type = str(raw_type).strip().lower() if raw_type else name
    if backend_type not in KNOWN_TYPES:
        raise ConfigError(
            f"backends.{name}.type must be one of {sorted(KNOWN_TYPES)}; "
            "add an explicit type for custom names"
        )

    timeout = _positive_float(raw.get("timeout"), 30.0, f"backends.{name}.timeout")

    if backend_type != "script":
        return BackendConfig(name=name, type=backend_type, timeout=timeout)

    mode = str(raw.get("mode", "canonical")).strip().lower()
    if mode not in SCRIPT_MODES:
        raise ConfigError(
            f"backends.{name}.mode must be 'canonical' or 'argv'"
        )

    command_raw = raw.get("command")
    if isinstance(command_raw, str):
        raise ConfigError(
            f"backends.{name}.command must be a list, not a shell string"
        )
    if not isinstance(command_raw, (list, tuple)) or not command_raw:
        raise ConfigError(f"backends.{name}.command must be a non-empty list")
    command = tuple(os.path.expanduser(str(part)) for part in command_raw)
    if not command[0].strip():
        raise ConfigError(f"backends.{name}.command[0] must not be blank")

    output = str(raw.get("output", "json")).strip().lower()
    if output != "json":
        raise ConfigError(f"backends.{name}.output must be 'json'")

    results_path = str(raw.get("results_path") or "results").strip()
    _validate_dotted_path(results_path, f"backends.{name}.results_path")

    fields_raw = raw.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise ConfigError(f"backends.{name}.fields must be a mapping")
    fields = {str(key): str(value) for key, value in fields_raw.items()}
    for canonical in ("title", "url", "description"):
        fields.setdefault(canonical, canonical)
    for key, value in fields.items():
        _validate_dotted_path(value, f"backends.{name}.fields.{key}")

    cwd_raw = raw.get("cwd")
    cwd: str | None = None
    if cwd_raw:
        cwd_path = Path(os.path.expanduser(str(cwd_raw)))
        if not cwd_path.is_absolute():
            cwd_path = base_dir / cwd_path
        cwd = str(cwd_path)

    max_output_bytes = _positive_int(
        raw.get("max_output_bytes"), 1_000_000, f"backends.{name}.max_output_bytes"
    )
    supports_extract = _as_bool(raw.get("supports_extract"), False)
    if supports_extract:
        logger.warning(
            "search-router: backends.%s.supports_extract is ignored in v1 "
            "(scripts are search-only)",
            name,
        )

    return BackendConfig(
        name=name,
        type=backend_type,
        timeout=timeout,
        mode=mode,
        command=command,
        output=output,
        results_path=results_path,
        fields=fields,
        cwd=cwd,
        max_output_bytes=max_output_bytes,
        supports_extract=False,
    )


def _parse_fallback(
    value: Any, backends: dict[str, BackendConfig], field_name: str
) -> tuple[str, ...]:
    names = _as_list(value, field_name)
    if not names:
        if "deepseek" in backends and "anysearch" in backends:
            names = ["deepseek", "anysearch"]
        else:
            names = list(backends)

    result: list[str] = []
    for name in names:
        normalized = normalize_backend_name(name)
        if normalized not in backends:
            raise ConfigError(
                f"{field_name}: backend {name!r} is not defined in backends"
            )
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _as_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        parts = re.split(r"[,\s]+", text)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        raise ConfigError(f"{field_name} must be a list or a comma-separated string")
    return [part.strip().strip('"').strip("'") for part in parts if part.strip()]


def _positive_float(value: Any, default: float, field_name: str) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a number") from exc
    if number <= 0:
        raise ConfigError(f"{field_name} must be > 0")
    return number


def _nonnegative_float(value: Any, default: float, field_name: str) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be a number") from exc
    if number < 0:
        raise ConfigError(f"{field_name} must be >= 0")
    return number


def _positive_int(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc
    if number <= 0:
        raise ConfigError(f"{field_name} must be > 0")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _validate_dotted_path(path: str, field_name: str) -> None:
    if not path or any(not part for part in path.split(".")):
        raise ConfigError(f"{field_name} must be a non-empty dotted path")


class ConfigStore:
    """Caches the parsed config and reloads it when the file changes.

    A malformed edit keeps the last good config, so a typo does not take web
    search down mid-session.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_config_path()
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._cached: RouteConfig | None = None
        self._key: tuple[int, int] | None = None
        self._missing = False

    def get(self) -> RouteConfig:
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                if self._cached is not None:
                    return self._cached
                config = default_config()
                self._cached = config
                self._missing = True
                self._key = None
                self.last_error = None
                return config
            except OSError as exc:
                if self._cached is not None:
                    self.last_error = f"cannot stat config: {exc}"
                    return self._cached
                raise ConfigError(f"cannot stat {self.path}: {exc}") from exc

            key = (stat.st_mtime_ns, stat.st_size)
            if self._cached is not None and key == self._key and not self._missing:
                return self._cached

            try:
                config = load_config(self.path)
            except ConfigError as exc:
                self.last_error = str(exc)
                if self._cached is not None:
                    logger.warning(
                        "search-router: %s; keeping last good config", exc
                    )
                    return self._cached
                raise

            self._cached = config
            self._key = key
            self._missing = False
            self.last_error = None
            return config
