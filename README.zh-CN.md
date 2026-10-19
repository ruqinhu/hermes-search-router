# Hermes Search Router

[English](README.md) | **简体中文**

一个 Hermes `web_search` / `web_extract` 后端：按配置路由到 **DeepSeek**、**AnySearch**
或**本地脚本**，支持手动切换和按顺序失败回退。

模型仍然调用内置的 `web_search` / `web_extract`；路由对模型不可见，不需要改提示词、
技能或 cron。

```
Hermes web_search / web_extract
            │  WebSearchProvider（外部 seam）
            ▼
   search-router provider
   ├─ routes.yaml      active / fallback / backends
   ├─ Router           手动切换 + 回退 + 冷却
   ├─ DeepSeekBackend   POST /v1/tools/search|fetch
   ├─ AnySearchBackend  POST /v1/search|extract
   └─ ScriptBackend     canonical JSON | argv + 字段映射
```

## 快速开始

直接从 GitHub 安装，和其他 Hermes 插件一样：

```bash
# 1. 安装并启用
hermes plugins install ruqinhu/hermes-search-router --enable

# 2. 把 Hermes 的 web 工具指向 router
hermes config set web.search_backend search-router
hermes config set web.extract_backend search-router
hermes config set web.backend search-router

# 3. DeepSeek 需要 Key；AnySearch 匿名即可用。
#    优先用 Hermes Key 存储：
hermes config set DEEPSEEK_API_KEY 'sk-...'
#    兜底方式（一定可用）：
# echo 'DEEPSEEK_API_KEY=sk-...' >> ~/.hermes/.env

# 4. 重启/重载 Hermes，然后在新会话里验证
hermes plugins list | grep -i search-router
hermes plugins show web-search-router 2>/dev/null || \
  hermes plugins show web/search-router
hermes tools
```

默认的 **DeepSeek + AnySearch** 不需要 `routes.yaml`：文件不存在时使用内置默认值。
只有要接本地脚本或调整顺序时才需要创建它。

> `hermes plugins install` 可能弹出安全扫描提示，因为这个插件可以执行**管理员配置**的
> 本地脚本。脚本只来自 `routes.yaml`，模型不能传入命令。确认内容后再允许安装。

安装布局可能因 Hermes 版本不同：GitHub 安装可能落在顶层 `web-search-router`，而手动
复制会落在 `plugins/web/search-router`（`web/search-router`）。provider 名始终是
`search-router`。如果 `hermes plugins list` 看不到它，用下面的手动安装。

安装后可做一次自检：

```bash
PLUGIN_DIR=$(find ~/.hermes -maxdepth 5 -type d \
  \( -name 'search-router' -o -name 'web-search-router' \) 2>/dev/null | head -1)
python3 "$PLUGIN_DIR/scripts/contract_check.py"   # 校验真实 Hermes ABC
python3 "$PLUGIN_DIR/scripts/selfcheck.py"        # 配置/环境/可用性
# 真实调用（会消耗额度）：
python3 "$PLUGIN_DIR/scripts/smoke.py"
```

## 为什么用 provider 插件

Hermes 替换 web 搜索后端的正式扩展点是：
`kind: backend` + `ctx.register_web_search_provider()` + `web.search_backend` /
`web.extract_backend`。本插件只使用这个扩展点：不覆盖 `web_search` 工具、不新增第二个
工具、也不依赖 `WebSearchProvider` ABC 之外的 Hermes 内部实现。

AnySearch 仍作为独立插件保留，用于紧急直连回退。

## 路由配置

默认路径：`~/.hermes/plugin-data/search-router/routes.yaml`
（支持 `HERMES_HOME`；测试/工具可用 `SEARCH_ROUTER_CONFIG` 覆盖）。

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
    # 可选：
    # cwd: /root/.hermes/scripts
    # max_output_bytes: 1048576
```

文件不存在时，默认使用 DeepSeek + AnySearch、`active: auto`、`on_empty: fallback`。
完整示例见 [`examples/routes.yaml`](examples/routes.yaml)。

命名：GitHub 仓库是 `hermes-search-router`，manifest 是 `web-search-router`，Hermes
provider 名是 `search-router`；手动嵌套安装目录是 `~/.hermes/plugins/web/search-router/`
（启用名 `web/search-router`）。

### 手动切换

```yaml
active: auto             # 按 fallback 列表
active: deepseek         # 先试 DeepSeek，再按 fallback
active: anysearch        # 先试 AnySearch
active: script:local_news
```

`script:` 前缀可省略，直接写后端名也可以。

### 回退语义

- 后端声明自己支持 `search`、`extract` 或两者。
- 候选按能力、可用性、冷却状态过滤。
- 第一个返回至少一条结果的后端获胜。
- 空结果：`on_empty: fallback` 继续下一个；`accept` 直接返回空。
- 失败：记录原因、设置冷却、继续下一个。
- 单个后端的超时 = `min(backend.timeout, 剩余总预算)`。
- 全部失败：`{"success": false, "error": "search-router: all backends failed: ..."}`。
- extract 由第一个返回至少一条有正文的 entry 的后端获胜；部分成功原样返回；全部
  entry 都失败才换下一个后端。

## 本地脚本

### canonical 模式（推荐）

脚本从 stdin 读 JSON，从 stdout 写 JSON：

```json
{"capability": "search", "query": "...", "limit": 5}
```

```json
{"results": [{"title": "...", "url": "...", "description": "..."}]}
```

也接受裸 JSON list。退出码 0 = 成功；非 0 = 失败，stderr 作为错误摘要。

可运行模板见 [`examples/canonical_search.py`](examples/canonical_search.py)。

### argv 模式（兼容已有脚本）

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
  # 只把这些具名变量传给子进程（另加 PATH/HOME/LANG/TMPDIR）。
  env_passthrough: [WEB_SEARCH_API_KEY]
  timeout: 30
```

`{query}` / `{limit}` 只替换 argv 元素，不经过 shell。`results_path` 是点号路径
（如 `data.results`）；`fields` 把 `title` / `url` / `description` 映射到源字段
（也支持点号路径）。

### 脚本安全

- 命令只来自 `routes.yaml`（管理员维护），不来自模型。
- `subprocess` 用 list 调用，`shell=False`。
- query 是一个 argv 元素或 JSON stdin；`; rm -rf` 不会被解释。
- 配置加载时会拒绝 shell/eval 参数（`bash -c`、`python -c`、`node -e` 等）；
  请用脚本文件 + argv 占位符。
- 超时会杀掉整个进程树；stdout/stderr 各有大小上限（默认各 1 MB）。
- 脚本默认只拿到最小环境（`PATH`、`HOME`、`LANG`、临时目录）。需要额外变量时用
  `env_passthrough: [VAR_NAME]` 显式放行；脚本**默认不会**继承 gateway 的 API Key。
- 脚本由管理员配置；只运行你信任的脚本。
- v1 脚本只支持 search；extract 会返回每条 URL 的 "search only" 错误。

## 自检与冒烟

dry run：检查配置、环境变量是否存在、后端可用性、可执行文件路径。不联网。

```bash
python3 scripts/selfcheck.py
python3 scripts/selfcheck.py --config /root/.hermes/plugin-data/search-router/routes.yaml
python3 scripts/selfcheck.py --json
```

live：对每个可用后端做一次真实搜索，可选做一次 extract。会消耗额度。

```bash
python3 scripts/smoke.py
python3 scripts/smoke.py --query "DeepSeek V4 发布" --limit 3
python3 scripts/smoke.py --extract https://example.com/
```

DeepSeek Search/Fetch 在 **2026-11-01 00:00（北京时间）前免费**，之后按成功请求计费。
DeepSeek 每个 endpoint、每个账号限制 5 QPS；adapter 在进程内做 0.21 秒最小间隔。

## 报告批量搜索

早/午/晚报经常要一次查多条独立信息。工具层连续发 `web_search` 是串行的，**≥4 条**
请用 batch CLI：

```bash
PLUGIN_DIR=$(find ~/.hermes -maxdepth 5 -type d \
  \( -name 'search-router' -o -name 'web-search-router' \) 2>/dev/null | head -1)

python3 "$PLUGIN_DIR/scripts/batch_search.py" \
  --profile news --workers 6 --dedupe-urls --json --out /tmp/report.json \
  "澜起科技 公告" "存储 涨价" "美股 隔夜 科技股"
```

- 最多 **12** 条并发；每条查询重试 1 次；失败隔离。
- `results` 按输入顺序按 query 返回，带 `wall_ms`、`spread_ms`（第一波启动极差）、
  单条耗时、`backend`、`age_days`、`stale`、`date_unknown`。
- profile：`general`（6 条/7 天）、`news`（8 条/3 天）、
  `research`（5 条/30 天，前 2 条抽正文）。
- `--extract-top N` 为每条查询前 N 条追加 `content`。
- 行情/公告/龙虎榜等结构化数据仍走直连采集；batch 只负责新闻/定性查询。

完整早/午/晚报 playbook、源选择、旧闻校验和时间盒见
[`docs/REPORT_SEARCH_PLAYBOOK.md`](docs/REPORT_SEARCH_PLAYBOOK.md)。

## 测试

离线；不联网、不需要 Key、不产生费用。

```bash
python3 -m pytest -q
```

- Adapter 测试用 `httpx.MockTransport`。
- Router 测试用 fake backend。
- Script 测试用临时 Python 脚本。
- Contract 测试锁定 search envelope 和 extract 裸 list 形状。

## 回滚

保留 AnySearch 插件。回退时**先改配置**：

```bash
hermes plugins list | grep -i anysearch          # 确认回退后端存在
hermes config set web.search_backend anysearch
hermes config set web.extract_backend anysearch
hermes config set web.backend anysearch
```

下一次调用生效；先跑一次搜索确认，再禁用 router：

```bash
hermes plugins disable web/search-router         # 手动嵌套安装
# 或：hermes plugins disable web-search-router   # GitHub 顶层安装
```

不要在 `web.*` 还指向 `search-router` 时禁用 router：否则所有 web 调用都会失败，直到
配置改回来。

## 手动安装（备选）

如果当前 Hermes 版本不支持从 GitHub 安装，或者安装后没有被发现，可以手动复制：

```bash
scp -r hermes-search-router root@HOST:/root/.hermes/plugins/web/search-router
ssh root@HOST
hermes plugins doctor /root/.hermes/plugins/web/search-router --ci
hermes plugins enable web/search-router --no-allow-tool-override
hermes plugins list | grep -i search-router
hermes config set web.search_backend search-router
hermes config set web.extract_backend search-router
hermes config set web.backend search-router
```

手动复制绕过安装时的安全扫描；真正的门槛是 `hermes plugins doctor`。这里用
`--no-allow-tool-override`，因为插件只注册 backend，不替换内置 `web_search` 工具。

`web.search_backend` / `web.extract_backend` 每次调用都会重新读取。`routes.yaml` 也按
mtime + size 热加载；插件加载后，切换或修改路由**不需要**重启 gateway。

如果 gateway 正在运行且插件刚启用，先确认是否 live reload；否则按正常 drain 流程重启
（默认 180 秒）。参考实例上必须用 `systemd-run` 包一层脚本触发重启，因为在 gateway
进程内直接重启会被拦截。

## 兼容性与限制

- 目标：Hermes Agent 0.20.5（Python 3.11）。只使用公开的 `WebSearchProvider` ABC，
  不改核心。
- `extract()` 返回裸 list。已对照 0.20.5 的 `tools/web_tools.py` dispatcher 验证：它
  直接遍历 list 并检查每条 `error`。ABC 文件顶部的注释仍写着 envelope，但方法契约是
  list。
- DeepSeek query 限制 128 个 Unicode 字符；控制字符会被拒绝。
- `DEEPSEEK_BASE_URL` / `ANYSEARCH_URL` 只允许官方 host（HTTPS、默认端口）。
  loopback 覆盖需要设置 `SEARCH_ROUTER_ALLOW_LOOPBACK=1`（仅测试/开发）。
- v1 不做 fan-out、merge/RRF、LLM 路由或结果缓存。
- v1 脚本不支持 extract。
- 冷却状态是进程内的；重启 Hermes 会清空。
- DeepSeek Key 必须来自 `platform.deepseek.com`，不是 Ark Key。401/402 表示 Key 或
  余额问题，不是路由 bug。
- AnySearch 每次请求最多 10 条；adapter 会夹到 10。

## 参考项目

- [`robbyczgw-cla/hermes-web-search-plus`](https://github.com/robbyczgw-cla/hermes-web-search-plus) —
  多 provider 路由、失败回退、原生 `wsp` 后端。本项目的回退语义和原生后端接入参考了它。
- [`NousResearch/hermes-agent` PR #45399](https://github.com/NousResearch/hermes-agent/pull/45399) —
  cascade provider，serial/hedge/hybrid 策略 + circuit breaker。错误分类和冷却的参考，
  不是依赖。
- [`JasonSwindle/hermes-deepseek-search-fetch`](https://github.com/JasonSwindle/hermes-deepseek-search-fetch) —
  DeepSeek Search/Fetch 字段映射和 Fetch Markdown 反转义参考。
- [`NousResearch/hermes-agent` PR #74410](https://github.com/NousResearch/hermes-agent/pull/74410) —
  核心 fallback chain 提案（`web.search_fallbacks` / `web.extract_fallbacks`）。

## License

MIT，见 [`LICENSE`](LICENSE)。
