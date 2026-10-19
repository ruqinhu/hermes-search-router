# Hermes Search Router — Design

Date: 2026-10-19
Status: approved by user (route A: self-built thin router)
Target: Hermes Agent 0.20.5 (`/opt/hermes-agent-0.20.5`), Python 3.11
Repo: `D:\codeai\hermes-search-router`

## 1. Problem

Hermes exposes one backend at a time through `web.search_backend` / `web.extract_backend`.
We need one backend that can use:

- DeepSeek Search/Fetch API
- AnySearch (anonymous or keyed)
- local scripts (canonical JSON or argv + field mapping)

with manual switching and failure fallback, without changing prompts, skills, cron jobs,
or the model-facing `web_search` / `web_extract` tools.

## 2. Prior art

- `robbyczgw-cla/hermes-web-search-plus` — multi-provider routing, fallback on failure/empty,
  optional native `wsp` backend. Mature, but its default provider set does not include
  DeepSeek or AnySearch, and it has no arbitrary local-script provider.
- `NousResearch/hermes-agent` PR #45399 (cascade) — priority chain, serial/hedge/hybrid,
  circuit breaker. Still an open PR with review issues; used as a design reference for
  error taxonomy and cooldown, not as a dependency.
- PRs #74410 / #36838 — core fallback chains, still open; not available on 0.20.5.
- `JasonSwindle/hermes-deepseek-search-fetch` — reference for DeepSeek field mapping and
  Fetch Markdown unescaping.

## 3. Decision

Use a single `WebSearchProvider` backend plugin named `search-router`.

- External seam: Hermes `WebSearchProvider` (`search`, `extract`).
- Internal seam: `Backend` protocol (`available`, `search`, `extract`, `capabilities`).
- Adapters: `DeepSeekBackend`, `AnySearchBackend`, `ScriptBackend`.
- The router owns routing, fallback, cooldown, normalization, and error envelopes.
- AnySearch remains an independent provider plugin for emergency direct rollback.

The model still calls `web_search` / `web_extract`; routing is invisible.

## 4. v1 scope

In scope:

- `active: auto | <backend> | script:<backend>`
- Ordered fallback for search and extract
- Failure fallback; optional empty-result fallback (`on_empty: fallback | accept`)
- Per-backend timeout, total deadline, process-local cooldown
- DeepSeek search + extract
- AnySearch search + extract
- Script search: canonical JSON and argv + field mapping
- Hot reload of `routes.yaml` with last-good-config protection
- Offline tests, live self-check, smoke script

Out of scope (YAGNI):

- LLM routing or synthesis
- Fan-out / merge / RRF
- Search cache
- Script extract
- Generic HTTP DSL
- Desktop settings UI
- Multi-key pools
- Automatic routing based on query classification

## 5. Configuration

Path: `~/.hermes/plugin-data/search-router/routes.yaml`
(`HERMES_HOME` respected; `SEARCH_ROUTER_CONFIG` overrides for tests/tools.)

```yaml
version: 1
active: auto

fallback:
  search:  [deepseek, anysearch]
  extract: [deepseek, anysearch]

on_empty: fallback          # fallback | accept
search_total_timeout: 45
extract_total_timeout: 100
cooldown_seconds: 60

backends:
  deepseek:
    type: deepseek
    timeout: 30
  anysearch:
    type: anysearch
    timeout: 30
  local_news:
    type: script
    mode: canonical
    command: ["python3", "/root/.hermes/scripts/local_news.py"]
    timeout: 20
  byted:
    type: script
    mode: argv
    command:
      ["python3", "/root/.hermes/skills/search/byted-web-search/scripts/web_search.py",
       "{query}", "--count", "{limit}"]
    output: json
    results_path: data.results
    fields: {title: title, url: url, description: snippet}
    timeout: 30
```

If the file is missing, defaults are DeepSeek + AnySearch, `active: auto`,
fallback `[deepseek, anysearch]`, `on_empty: fallback`.

Invalid YAML/schema: log, keep last good config; if none, return a clear error envelope.
Changes take effect on the next call (mtime + size check).

## 6. Routing semantics

`active: auto` → use the capability fallback list.

`active: deepseek` / `script:local_news` → try that backend first, then the fallback list.
`script:` is optional; a plain backend name also works.

For each candidate:

1. Skip if it does not advertise the capability.
2. Skip if `available()` is false.
3. Skip if in cooldown (process-local).
4. Call with `timeout = min(backend.timeout, remaining total budget)`.
5. Success with hits → normalize (dedupe by URL, cap limit, 1-based position) and return.
6. Success with zero hits → `on_empty=accept` returns empty; `fallback` tries next.
7. Failure → record reason, set cooldown, try next.

All fail → `{"success": false, "error": "search-router: all backends failed: ..."}`.

Extract: first backend that returns at least one content-bearing entry wins. Partial success
is returned as-is (no cross-backend retry for individual URLs in v1). All-error results try
the next backend. Missing requested URLs are filled with per-URL errors.

## 7. Backend contract

```python
class Backend(Protocol):
    name: str
    capabilities: frozenset[str]   # {"search", "extract"}
    timeout: float
    def available(self) -> bool: ...
    def search(self, query: str, limit: int, timeout: float | None = None) -> SearchOutcome: ...
    def extract(self, urls: Sequence[str], timeout: float | None = None, **kwargs) -> list[dict]: ...
```

`SearchOutcome` carries `ok`, `hits`, `error`, `cooldown`, `meta`.

Search hits are normalized to:

```json
{"title": "...", "url": "...", "description": "...", "position": 1, "backend": "deepseek"}
```

Extra backend-specific fields (`date`, `authority`, `rerank_score`, `site_name`) are additive.
`extract()` returns a bare list, matching the bundled provider contract on 0.20.5:

```json
{"url": "...", "title": "...", "content": "...", "raw_content": "...", "metadata": {"sourceURL": "...", "backend": "..."}}
```

Failures add `"error"`; never raise.

## 8. Adapters

### DeepSeek

- `POST https://api.deepseek.com/v1/tools/search`
- Body `{query, count, need_content}`; count clamped 1–50.
- `description` = `summary` (fallback `snippet`), plus a compact footer
  `来源: ...｜发布: YYYY-MM-DD｜权威度: N/4` when fields exist. `authority=-1` is omitted.
- Deduplicate by URL; skip empty URLs.
- Query validation: 1–128 Unicode chars, non-blank. Longer → error, no truncation.
- `POST https://api.deepseek.com/v1/tools/fetch`, one URL per request.
- Unescape Markdown: `\` + ASCII punctuation → punctuation. Do not touch `\n`, `\d`, `C:\path`.
- Multi-URL: sequential, input order, 100s internal budget, per-request timeout 30s.
- 401/402 are account-level: stop remaining URLs, fill all with the same error.
- 403/404/413 are per-URL errors; continue.
- 408/429/5xx: one retry; honor `Retry-After` up to 5s; then error.

### AnySearch

- `POST /v1/search` body `{query, max_results}`; count clamped 1–10.
- `POST /v1/extract` body `{url}`.
- Optional bearer `ANYSEARCH_API_KEY`; anonymous works.
- Map `data.results[*].snippet`/`content` to description; `data.content` to extract.

### Script

Canonical mode:

- stdin: `{"capability":"search","query":"...","limit":5}`
- stdout: `{"results":[{title,url,description}]}` (bare list also accepted)
- exit 0 = success; non-zero = failure with truncated stderr.

Argv mode:

- `command` list with `{query}` and `{limit}` placeholders.
- stdout JSON; `results_path` dotted path (`data.results`); `fields` maps
  `title`/`url`/`description` to source fields (dotted paths allowed).
- exit 0 = success; non-zero = failure.

Security and limits:

- `subprocess` list, `shell=False`; query is one argv element or JSON stdin, never shell text.
- timeout kills the child; output capped (default 1 MB).
- scripts are admin-configured in `routes.yaml`; v1 is search-only (`supports_extract` ignored).

## 9. Error handling and limits

- No backend ever raises through the provider; all failures become envelopes/entries.
- Error text truncated to 300 chars; never log keys, Authorization headers, or full pages.
- Cooldown: process-local; set on failure. Default 60s; 429/5xx 60–120s; 401/402 3600s.
- DeepSeek rate limit: 5 QPS per endpoint, per process. AnySearch: no client-side limiter.
- Total deadlines: search 45s, extract 100s (configurable).

## 10. Hermes integration

`config.yaml`:

```yaml
web:
  backend: search-router
  search_backend: search-router
  extract_backend: search-router

plugins:
  enabled:
    - web/anysearch
    - web/search-router
```

- Plugin directory: `~/.hermes/plugins/web/search-router/`
- Provider name: `search-router`
- Manifest: `kind: backend`, `provides_web_providers: [search-router]`
- No `requires_env`; availability is computed from configured backends.
- Rollback: set the three `web.*` keys back to `anysearch`; no code change.
- Direct DeepSeek (without router) is out of v1; use `active: deepseek` or a future
  second registration of the same adapter.

## 11. Testing

- `pytest`, offline.
- Adapter tests use `httpx.MockTransport`; no network, no billing.
- Router tests use fake backends; no HTTP.
- Script tests use temporary Python scripts and `sys.executable`.
- Contract tests assert the Hermes search envelope and bare-list extract shape.
- `scripts/selfcheck.py` checks config, env names, executables, and optionally runs live
  calls with `--live`.
- `scripts/smoke.py` is a thin live wrapper.

## 12. Acceptance

1. `active: deepseek`, `active: anysearch`, and `active: script:<name>` all work; changes apply
   on the next call.
2. A failing first backend falls back to the next; the session does not crash.
3. Empty results obey `on_empty`.
4. Deduplication, limit clamping, and 1-based positions are correct.
5. Script query strings are never shell-interpreted; timeouts kill the process.
6. Search returns the Hermes envelope; extract returns a bare list.
7. Rollback to `anysearch` is a config-only operation.
8. No keys or page bodies in logs.
