"""Local script backend.

Two modes:

- ``canonical``: JSON on stdin, JSON on stdout.
- ``argv``: ``{query}`` / ``{limit}`` placeholders plus a dotted ``results_path``
  and a field mapping.

The command is always a list and is executed without a shell.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .base import (
    CAP_SEARCH,
    Hit,
    SearchOutcome,
    clamp_limit,
    extract_entry,
    truncate,
    validate_query,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import BackendConfig

logger = logging.getLogger(__name__)


class ScriptBackend:
    capabilities = frozenset({CAP_SEARCH})

    def __init__(self, config: "BackendConfig", *, sleep=time.sleep) -> None:
        self.name = config.name
        self.timeout = float(config.timeout)
        self._mode = config.mode
        self._command = list(config.command)
        self._results_path = config.results_path
        self._fields = dict(config.fields)
        self._cwd = config.cwd
        self._max_output_bytes = int(config.max_output_bytes)
        self._sleep = sleep

    def available(self) -> bool:
        if not self._command or not self._command[0].strip():
            return False
        executable = self._command[0]
        if os.path.isabs(executable) or os.sep in executable:
            if not Path(executable).expanduser().exists():
                return False
        elif shutil.which(executable) is None:
            return False

        # When the executable is an interpreter, check an explicit script path
        # in the remaining arguments. Flags and output paths are ignored.
        for part in self._command[1:]:
            if "{" in part or "}" in part:
                continue
            if self._looks_like_script_path(part):
                if not Path(part).expanduser().exists():
                    return False
        return True

    @staticmethod
    def _looks_like_script_path(part: str) -> bool:
        suffixes = {".py", ".sh", ".bash", ".js", ".rb", ".pl", ".ps1"}
        path = Path(part)
        if path.suffix.lower() not in suffixes:
            return False
        return path.is_absolute() or os.sep in part

    # -- search ---------------------------------------------------------

    def search(
        self, query: str, limit: int, timeout: float | None = None
    ) -> SearchOutcome:
        clean, error = validate_query(query)
        if error:
            return SearchOutcome.failure(f"{self.name}: {error}")
        count = clamp_limit(limit, minimum=1, maximum=50, default=5)
        request_timeout = max(0.1, min(self.timeout, float(timeout or self.timeout)))

        if self._mode == "canonical":
            return self._search_canonical(clean, count, request_timeout)
        return self._search_argv(clean, count, request_timeout)

    def _search_canonical(
        self, query: str, limit: int, timeout: float
    ) -> SearchOutcome:
        payload = json.dumps(
            {"capability": "search", "query": query, "limit": limit},
            ensure_ascii=False,
        )
        started = time.monotonic()
        proc, error = self._run(self._command, payload, timeout)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if error:
            return SearchOutcome.failure(f"{self.name}: {error}")

        assert proc is not None
        if proc.returncode != 0:
            return SearchOutcome.failure(
                f"{self.name}: exit {proc.returncode}: {self._output_excerpt(proc)}"
            )
        too_large = self._too_large(proc.stdout)
        if too_large:
            return SearchOutcome.failure(f"{self.name}: {too_large}")

        try:
            data = self._parse_json(proc.stdout)
        except ValueError as exc:
            return SearchOutcome.failure(f"{self.name}: {exc}")

        if isinstance(data, dict):
            items = data.get("results")
        elif isinstance(data, list):
            items = data
        else:
            items = None
        if not isinstance(items, list):
            return SearchOutcome.failure(
                f"{self.name}: canonical output must contain a results list"
            )

        hits = self._map_items(items, limit)
        logger.info(
            "%s search: query_chars=%d limit=%d hits=%d exit=%s elapsed_ms=%.0f",
            self.name,
            len(query),
            limit,
            len(hits),
            proc.returncode,
            elapsed_ms,
        )
        return SearchOutcome.success(hits, backend=self.name, count=len(hits))

    def _search_argv(
        self, query: str, limit: int, timeout: float
    ) -> SearchOutcome:
        args = [
            part.replace("{query}", query).replace("{limit}", str(limit))
            for part in self._command
        ]
        started = time.monotonic()
        proc, error = self._run(args, None, timeout)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if error:
            return SearchOutcome.failure(f"{self.name}: {error}")

        assert proc is not None
        if proc.returncode != 0:
            return SearchOutcome.failure(
                f"{self.name}: exit {proc.returncode}: {self._output_excerpt(proc)}"
            )
        too_large = self._too_large(proc.stdout)
        if too_large:
            return SearchOutcome.failure(f"{self.name}: {too_large}")

        try:
            data = self._parse_json(proc.stdout)
        except ValueError as exc:
            return SearchOutcome.failure(f"{self.name}: {exc}")

        items = self._dig(data, self._results_path)
        if not isinstance(items, list):
            return SearchOutcome.failure(
                f"{self.name}: results_path {self._results_path!r} did not yield a list"
            )

        hits = self._map_items(items, limit)
        logger.info(
            "%s search: query_chars=%d limit=%d hits=%d exit=%s elapsed_ms=%.0f",
            self.name,
            len(query),
            limit,
            len(hits),
            proc.returncode,
            elapsed_ms,
        )
        return SearchOutcome.success(hits, backend=self.name, count=len(hits))

    # -- extract --------------------------------------------------------

    def extract(
        self,
        urls: Sequence[str],
        timeout: float | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if isinstance(urls, str):
            urls = [urls]
        return [
            extract_entry(
                str(url),
                error=f"{self.name}: script backend supports search only",
                backend=self.name,
            )
            for url in urls
        ]

    # -- process --------------------------------------------------------

    def _run(
        self, args: list[str], stdin_text: str | None, timeout: float
    ) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
        kwargs = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "cwd": self._cwd,
            "shell": False,
            "check": False,
        }
        if stdin_text is None:
            # Never let an argv-mode script inherit the gateway's stdin.
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_text
        try:
            proc = subprocess.run(args, **kwargs)
        except subprocess.TimeoutExpired:
            return None, f"script timed out after {timeout:g}s"
        except OSError as exc:
            return None, f"script failed to start: {exc}"
        return proc, None

    def _too_large(self, stdout: str | None) -> str | None:
        size = len((stdout or "").encode("utf-8"))
        if size > self._max_output_bytes:
            return (
                f"script output too large ({size} > {self._max_output_bytes} bytes)"
            )
        return None

    @staticmethod
    def _output_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
        text = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        return truncate(text or "no output")

    @staticmethod
    def _parse_json(stdout: str | None) -> Any:
        text = (stdout or "").strip().lstrip("\ufeff")
        if not text:
            raise ValueError("script produced no stdout")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"script stdout is not JSON: {truncate(text, 160)}"
            ) from exc

    def _map_items(self, items: list[Any], limit: int) -> list[Hit]:
        hits: list[Hit] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            url = self._field(item, "url")
            if not url or url in seen:
                continue
            seen.add(url)
            title = self._field(item, "title") or url
            description = self._field(item, "description")
            if not description:
                description = self._first_value(item, ("snippet", "content"))
            hits.append(Hit(title=title, url=url, description=description))
            if len(hits) >= limit:
                break
        return hits

    def _field(self, item: dict[str, Any], name: str) -> str:
        path = self._fields.get(name, name)
        value = self._dig(item, path)
        return str(value).strip() if value is not None else ""

    @staticmethod
    def _first_value(item: dict[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            value = item.get(key)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _dig(value: Any, path: str) -> Any:
        current = value
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else None
            else:
                return None
            if current is None:
                return None
        return current
