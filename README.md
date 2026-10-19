# Hermes Search Router

**English** | [简体中文](README.zh-CN.md)

One Hermes `web_search` / `web_extract` backend that routes to **DeepSeek**, **AnySearch**,
or **local scripts**, with manual switching and ordered failure fallback.

The model still calls the built-in `web_search` / `web_extract` tools. Routing is invisible:
no prompt changes, no skill changes, no cron changes.

```
Hermes web_search / web_extract
            │  WebSearchProvider (external seam)
            ▼
   search-router provider
   ├─ routes.yaml      active / fallback / backends
   ├─ Router            manual switch + fallback + cooldown
   ├─ DeepSeekBackend   POST /v1/tools/search|fetch
   ├─ AnySearchBackend  POST /v1/search|extract
   └─ ScriptBackend     canonical JSON | argv + field mapping
```

## Quick start

Install directly from GitHub, the same way as other Hermes plugins:

```bash
# 1. Install and enable the plugin
hermes plugins install ruqinhu/hermes-search-router --enable

# 2. Route Hermes' web tools through the router
hermes config set web.search_backend search-router
hermes config set web.extract_backend search-router
hermes config set web.backend search-router

# 3. DeepSeek key: AnySearch works anonymously, DeepSeek needs a key.
#    If DEEPSEEK_API_KEY is already in ~/.hermes/.env, skip this.
grep -q '^DEEPSEEK_API_KEY=' ~/.hermes/.env || \
  echo 'DEEPSEEK_API_KEY=sk-...' >> ~/.hermes/.env

# 4. Restart/reload Hermes once, then verify in a new session
hermes plugins list | grep -i search-router
hermes tools
```

No `routes.yaml` is required for the default **DeepSeek + AnySearch** setup: the router
uses sensible defaults when the file is missing. Add one only when you want local scripts
or a different order.

> `hermes plugins install` may show a security-scan prompt because this plugin can execute
> **admin-configured** local scripts. The scripts are listed in `routes.yaml`; the model
> cannot supply commands. Review the prompt, then allow the install.

Optional self-check after install:

```bash
PLUGIN_DIR=$(find ~/.hermes -maxdepth 5 -type d \
  \( -name 'search-router' -o -name 'web-search-router' \) 2>/dev/null | head -1)
python3 "$PLUGIN_DIR/scripts/selfcheck.py"
# live calls (may cost quota):
python3 "$PLUGIN_DIR/scripts/smoke.py"
```

## Why a provider plugin

Hermes has one intended seam for replacing the web search backend:
`kind: backend` + `ctx.register_web_search_provider()` + `web.search_backend` /
`web.extract_backend`. This plugin uses that seam. It does not override the `web_search`
tool, does not add a second tool, and does not depend on Hermes internals beyond the
public `WebSearchProvider` ABC.

AnySearch remains available as a separate plugin for emergency direct rollback.

## Route configuration

Default path: `~/.hermes/plugin-data/search-router/routes.yaml`
(`HERMES_HOME` respected; `SEARCH_ROUTER_CONFIG` overrides for tests/tools).

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
```

If the file is missing, the router uses DeepSeek + AnySearch, `active: auto`, and
`on_empty: fallback`. See [`examples/routes.yaml`](examples/routes.yaml) for a full example.

### Manual switching

```yaml
active: auto             # use the fallback lists
active: deepseek         # try DeepSeek first, then the fallback list
active: anysearch        # try AnySearch first
active: script:local_news
```

`script:` is optional; a plain backend name works too.

### Fallback semantics

- A backend either advertises `search`, `extract`, or both.
- Candidates are filtered by capability, availability, and cooldown.
- First success with at least one hit wins.
- Empty success: `on_empty: fallback` tries the next backend; `accept` returns empty.
- Failure: record the reason, set a cooldown, try the next backend.
- Candidate timeout = `min(backend.timeout, remaining total budget)`.
- All failed: `{"success": false, "error": "search-router: all backends failed: ..."}`.
- For extract, the first backend that returns at least one content-bearing entry wins.
  Partial success is returned as-is; all-error results try the next backend.

## Local scripts

### Canonical mode (recommended)

The script receives JSON on stdin and writes JSON on stdout:

```json
{"capability": "search", "query": "...", "limit": 5}
```

```json
{"results": [{"title": "...", "url": "...", "description": "..."}]}
```

A bare JSON list is also accepted. Exit 0 = success; non-zero = failure with stderr.

See [`examples/canonical_search.py`](examples/canonical_search.py) for a working template.

### Argv mode (existing scripts)

```yaml
byted:
  type: script
  mode: argv
  command:
    ["python3",
     "/root/.hermes/skills/search/byted-web-search/scripts/web_search.py",
     "{query}", "--count", "{limit}"]
  output: json
  results_path: results
  fields: {title: title, url: url, description: snippet}
  timeout: 30
```

`{query}` and `{limit}` are substituted into argv elements, not into a shell string.
`results_path` is a dotted path (for example `data.results`); `fields` maps
`title` / `url` / `description` to source fields (dotted paths allowed).

### Script safety

- Commands come from `routes.yaml` (admin-owned), never from the model.
- `subprocess` is called with a list and `shell=False`.
- The query is one argv element or JSON stdin; `; rm -rf` is not interpreted.
- Timeout kills the child; stdout is capped (default 1 MB).
- Scripts inherit the gateway environment, including API keys. Only run scripts you trust.
- v1 scripts are search-only; `extract` returns a per-URL "search only" error.

## Self-check and smoke

Dry run: config, env presence, backend availability, executable paths. No network.

```bash
python3 scripts/selfcheck.py
python3 scripts/selfcheck.py --config /root/.hermes/plugin-data/search-router/routes.yaml
python3 scripts/selfcheck.py --json
```

Live: one search per available backend, optional extract. May consume quota.

```bash
python3 scripts/smoke.py
python3 scripts/smoke.py --query "DeepSeek V4 release" --limit 3
python3 scripts/smoke.py --extract https://example.com/
```

DeepSeek Search/Fetch is free until **2026-11-01 00:00 Beijing time**, then billed per
successful request. DeepSeek is limited to 5 QPS per endpoint and account; the adapter
uses a per-process 0.21s minimum interval.

## Tests

Offline; no network, no API key, no billing.

```bash
python3 -m pytest -q
```

- Adapters use `httpx.MockTransport`.
- Router tests use fake backends.
- Script tests use temporary Python scripts.
- Contract tests assert the search envelope and bare-list extract shape.

## Rollback

Keep the existing AnySearch plugin enabled. To go back:

```bash
hermes config set web.search_backend anysearch
hermes config set web.extract_backend anysearch
hermes config set web.backend anysearch
```

The change applies to the next call. To remove the router entirely:

```bash
hermes plugins disable web-search-router   # or: hermes plugins disable web/search-router
```

## Manual install (fallback)

If your Hermes version cannot install from GitHub, copy the directory manually:

```bash
scp -r hermes-search-router root@HOST:/root/.hermes/plugins/web/search-router
ssh root@HOST
hermes plugins enable web/search-router
hermes config set web.search_backend search-router
hermes config set web.extract_backend search-router
hermes config set web.backend search-router
```

`web.search_backend` / `web.extract_backend` are read per call. `routes.yaml` is also
re-read per call (mtime + size), so switching or editing routes does **not** need a
gateway restart after the plugin is loaded.

If a gateway is running and the plugin was just enabled, confirm the live reload; if not,
restart with the usual drain period (default 180s). On the reference host the restart must
be triggered from a script via `systemd-run`, because restarting the gateway from inside
the gateway process is blocked.

## Compatibility and limitations

- Target: Hermes Agent 0.20.5 (Python 3.11). Uses the public `WebSearchProvider` ABC;
  no core patches.
- `extract()` returns a bare list, matching the bundled provider behavior. If a future
  Hermes version requires `{"success": true, "data": [...]}`, wrap the return value.
- No fan-out, merge/RRF, LLM routing, or result cache in v1.
- No script extract in v1.
- Cooldowns are process-local; restarting Hermes clears them.
- DeepSeek key must come from `platform.deepseek.com`, not an Ark key. A 401/402 means
  key or balance, not a routing bug.
- AnySearch accepts 1–10 results per request; the adapter clamps to 10.

## Prior art

- [`robbyczgw-cla/hermes-web-search-plus`](https://github.com/robbyczgw-cla/hermes-web-search-plus) —
  multi-provider routing, fallback, native `wsp` backend. Design reference for fallback
  semantics and a proven native-backend integration.
- [`NousResearch/hermes-agent` PR #45399](https://github.com/NousResearch/hermes-agent/pull/45399) —
  cascade provider with serial/hedge/hybrid strategies and a circuit breaker. Design
  reference for error taxonomy and cooldowns; not a dependency.
- [`JasonSwindle/hermes-deepseek-search-fetch`](https://github.com/JasonSwindle/hermes-deepseek-search-fetch) —
  DeepSeek Search/Fetch mapping and Fetch Markdown unescaping.
- [`NousResearch/hermes-agent` PR #74410](https://github.com/NousResearch/hermes-agent/pull/74410) —
  core fallback-chain proposal (`web.search_fallbacks` / `web.extract_fallbacks`).

## License

MIT. See [`LICENSE`](LICENSE).
