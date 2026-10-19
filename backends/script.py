"""Local script backend.

Two modes:

- ``canonical``: JSON on stdin, JSON on stdout.
- ``argv``: ``{query}`` / ``{limit}`` placeholders plus a dotted ``results_path``
  and a field mapping.

The command is always a list and is executed without a shell. Output is read
incrementally and capped; timeout kills the whole process group, not just the
direct child.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
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


@dataclass
class _RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False


class ScriptBackend:
    capabilities = frozenset({CAP_SEARCH})
    _PLACEHOLDER = re.compile(r"\{(query|limit)\}")
    _BASE_ENV_KEYS = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USER",
    )

    def __init__(self, config: "BackendConfig", *, sleep=time.sleep) -> None:
        self.name = config.name
        self.timeout = float(config.timeout)
        self._mode = config.mode
        self._command = list(config.command)
        self._results_path = config.results_path
        self._fields = dict(config.fields)
        self._cwd = config.cwd
        self._max_output_bytes = int(config.max_output_bytes)
        self._env_passthrough = tuple(config.env_passthrough)
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
                path = Path(part).expanduser()
                if not path.is_absolute() and self._cwd:
                    path = Path(self._cwd) / path
                if not path.exists():
                    return False
        return True

    def _looks_like_script_path(self, part: str) -> bool:
        suffixes = {".py", ".sh", ".bash", ".js", ".rb", ".pl", ".ps1"}
        path = Path(part)
        if path.suffix.lower() not in suffixes:
            return False
        return path.is_absolute() or os.sep in part or self._cwd is not None

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
        run, error = self._run(self._command, payload, timeout)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if error:
            return SearchOutcome.failure(f"{self.name}: {error}")

        assert run is not None
        failure = self._run_failure(run, timeout)
        if failure:
            return SearchOutcome.failure(f"{self.name}: {failure}")

        try:
            data = self._parse_json(run.stdout.decode("utf-8", "replace"))
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
            run.returncode,
            elapsed_ms,
        )
        return SearchOutcome.success(hits, backend=self.name, count=len(hits))

    def _search_argv(
        self, query: str, limit: int, timeout: float
    ) -> SearchOutcome:
        args = [self._render_arg(part, query, limit) for part in self._command]
        started = time.monotonic()
        run, error = self._run(args, None, timeout)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if error:
            return SearchOutcome.failure(f"{self.name}: {error}")

        assert run is not None
        failure = self._run_failure(run, timeout)
        if failure:
            return SearchOutcome.failure(f"{self.name}: {failure}")

        try:
            data = self._parse_json(run.stdout.decode("utf-8", "replace"))
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
            run.returncode,
            elapsed_ms,
        )
        return SearchOutcome.success(hits, backend=self.name, count=len(hits))

    def _render_arg(self, part: str, query: str, limit: int) -> str:
        # Single-pass substitution: a query containing "{limit}" must not be
        # rewritten by the second replacement.
        return self._PLACEHOLDER.sub(
            lambda match: query if match.group(1) == "query" else str(limit),
            part,
        )

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
    ) -> tuple[_RunResult | None, str | None]:
        stdin = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
        kwargs: dict[str, Any] = {
            "stdin": stdin,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": self._cwd,
            "env": self._child_env(),
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(args, **kwargs)
        except OSError as exc:
            return None, f"script failed to start: {exc}"

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        readers = [
            threading.Thread(
                target=self._drain,
                args=(proc.stdout, stdout, overflow),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(proc.stderr, stderr, overflow),
                daemon=True,
            ),
        ]
        for thread in readers:
            thread.start()

        timed_out = False
        deadline = time.monotonic() + max(0.05, float(timeout))
        try:
            while True:
                if overflow.is_set():
                    self._kill_tree(proc)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._kill_tree(proc)
                    break
                try:
                    proc.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc, force=True)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            # Join readers before touching the BufferedReader. close() blocks on
            # the lock held by a reader stuck in read(), which would defeat the
            # timeout. If a reader is still alive, kill the process group and
            # leave that daemon reader alone rather than hanging the provider.
            for thread in readers:
                thread.join(timeout=0.5)
            if any(thread.is_alive() for thread in readers):
                self._kill_tree(proc, force=True)
                for thread in readers:
                    thread.join(timeout=0.5)
            for stream, thread in zip((proc.stdout, proc.stderr), readers):
                if not thread.is_alive():
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass

        return (
            _RunResult(
                returncode=proc.returncode,
                stdout=bytes(stdout),
                stderr=bytes(stderr),
                timed_out=timed_out,
                overflow=overflow.is_set(),
            ),
            None,
        )

    def _drain(
        self, stream: Any, sink: bytearray, overflow: threading.Event
    ) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                remaining = self._max_output_bytes - len(sink)
                if remaining <= 0:
                    overflow.set()
                    return
                if len(chunk) > remaining:
                    sink.extend(chunk[:remaining])
                    overflow.set()
                    return
                sink.extend(chunk)
        except (ValueError, OSError):
            return

    def _child_env(self) -> dict[str, str]:
        env = {
            name: os.environ[name]
            for name in self._BASE_ENV_KEYS
            if name in os.environ
        }
        for name in self._env_passthrough:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    def _kill_tree(
        self, proc: subprocess.Popen[bytes], *, force: bool = False
    ) -> None:
        if not force and proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except Exception:  # noqa: BLE001 - fall back to direct kill
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            return
        try:
            # start_new_session=True makes the direct child the process-group
            # leader, so this also reaches grandchildren that inherited pipes.
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001 - fall back to direct kill
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _run_failure(self, run: _RunResult, timeout: float) -> str | None:
        if run.timed_out:
            return f"script timed out after {timeout:g}s"
        if run.overflow:
            return (
                f"script output too large (exceeded {self._max_output_bytes} "
                "bytes on a stream)"
            )
        if run.returncode is None:
            return "script did not exit"
        if run.returncode != 0:
            return f"exit {run.returncode}: {self._output_excerpt(run)}"
        return None

    @staticmethod
    def _output_excerpt(run: _RunResult) -> str:
        text = (run.stderr or run.stdout).decode("utf-8", "replace").strip()
        return truncate(text or "no output")

    @staticmethod
    def _parse_json(stdout: str) -> Any:
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
