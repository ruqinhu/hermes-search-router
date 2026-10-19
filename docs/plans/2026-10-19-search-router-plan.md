# Search Router — Implementation Plan

Date: 2026-10-19
Spec: `docs/specs/2026-10-19-search-router-design.md`

## Phase 0 — Repo skeleton

- `.gitignore`, `LICENSE` (MIT), `plugin.yaml`, `__init__.py`
- `docs/specs/`, `docs/plans/`
- `README.md` (install, config, rollback, security)

## Phase 1 — Config

- `config.py`: `BackendConfig`, `RouteConfig`, `load_config`, `ConfigStore`
- Defaults: DeepSeek + AnySearch, `active: auto`, fallback both
- Validate names, modes, commands, timeouts, dotted paths
- Hot reload by mtime/size; last-good config on parse errors
- Tests: `tests/test_config.py`

## Phase 2 — Backend contract and adapters

- `backends/base.py`: `Hit`, `SearchOutcome`, `Backend`, `RateLimiter`, validation helpers
- `backends/http.py`: JSON POST with timeout/retry/Retry-After
- `backends/deepseek.py`: search + extract, mapping, footer, unescape, error codes
- `backends/anysearch.py`: search + extract, optional key
- `backends/script.py`: canonical + argv, no shell, timeout, output cap
- `factory.py`: env lookup and backend construction
- Tests: `test_deepseek.py`, `test_anysearch.py`, `test_script.py`

## Phase 3 — Router provider

- `provider.py`: chain selection, active pin, fallback, cooldown, normalization
- Extract routing: first content-bearing backend; all-error fallback; fill missing URLs
- `is_available`, `supports_*`, `get_setup_schema`
- `__init__.py` lazy `register(ctx)`
- Tests: `test_router.py`, `test_contract.py`

## Phase 4 — Tooling and docs

- `examples/routes.yaml`, `examples/canonical_search.py`
- `scripts/selfcheck.py`, `scripts/smoke.py`
- README with copy-paste install/rollback commands
- Run `pytest -q`; fix all failures

## Phase 5 — Git and GitHub

- `git init`, commit
- Create `ruqinhu/hermes-search-router` via GitHub API (stored credential)
- `git remote add origin`; push `main`
- If the local proxy is down, stop here and report the exact blocker

## TDD order

For each module: write the failing test, implement the smallest behavior, run the
test, refactor. Keep adapter tests network-free via `httpx.MockTransport`.
