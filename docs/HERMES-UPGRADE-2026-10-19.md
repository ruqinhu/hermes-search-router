# Hermes 0.21.5 upgrade record (2026-10-19)

## Target

- Source: `NousResearch/hermes-agent` tag `v2026.9.24`
- Version: **0.21.5**
- Python requirement: `>=3.11,<3.14`; server runs 3.11.6
- Previous: 0.20.5 (2026.8.19), editable at `/opt/hermes-agent-0.20.5`

## Backup

Created before any package change:

- `/root/backups/hermes-full-20261019-1822.tar.gz` (386M)
  sha256 `7565cc98e6dceba6c17ab42322284d87206dd2dbfaed12691febe5a984b6d56b`
- `/root/backups/hermes-mini-20261019-1822.tar.gz` (146M)
  sha256 `6288687b672fc5b8b7c58143249ac78a48ad80001cb3437ca271a2043b634a17`
- `/root/hermes-pip-freeze-0.20.5.txt`
- Full staged tree: `/root/backups/hermes-self-20261019-1822/`

## Staging validation

- Source tarball uploaded to `/root/hermes-0.21.5-src.tar.gz`, extracted to
  `/opt/hermes-agent-0.21.5`
- Isolated venv: `/opt/hermes-0.21.5-venv`
- `/opt/hermes-0.21.5-venv/bin/hermes --version` → 0.21.5
- `contract_check.py` → `failures: []`
- `hermes plugins doctor` → OK
- `hermes search-router status/test` → OK, live DeepSeek result
- No live gateway touched during staging

## Live upgrade

```bash
systemctl stop hermes-gateway
env -u VIRTUAL_ENV -u UV_PYTHON \
  /root/.local/bin/uv pip install --python /usr/bin/python3 \
  -e /opt/hermes-agent-0.21.5
systemctl start --no-block hermes-gateway
```

Installed package changes: `hermes-agent 0.20.5 → 0.21.5`,
`nemo-relay 0.7.3 → 0.8.4`, plus `firecrawl-anydoc`, `pillow-heif`,
`snowballstemmer`. Gateway active at 18:27:43 CST.

## Verification

- `hermes --version` → 0.21.5 (2026.9.24), Python 3.11.6
- `hermes plugins show web/search-router` → v0.4.0 enabled
- `hermes plugins doctor` → OK
- `contract_check.py` → `failures: []`
- `web_search_tool("澜起科技 公告", 2)` → `success: true`, backend `search-router`,
  DeepSeek result with `authority: 4`
- `web_extract_tool(["https://www.cnstock.com/commonDetail/804104"])` → content returned
- `hermes search-router test` → success, backend `deepseek`
- Gateway log: `registered web provider: search-router`
- Lark websocket reconnect at 18:28:19; the 18:27:40 error was the old process
  closing during restart, not a post-upgrade failure.

## Rollback

Package only:

```bash
systemctl stop hermes-gateway
env -u VIRTUAL_ENV -u UV_PYTHON \
  /root/.local/bin/uv pip install --python /usr/bin/python3 \
  -e /opt/hermes-agent-0.20.5
systemctl start --no-block hermes-gateway
```

Data (only if the upgrade damaged `~/.hermes`):

```bash
systemctl stop hermes-gateway
# inspect /root/backups/hermes-full-20261019-1822.tar.gz contents first
systemctl start --no-block hermes-gateway
```

The old editable source `/opt/hermes-agent-0.20.5` and the full backup are kept.
