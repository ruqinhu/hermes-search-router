# 与 dsh-web-search-plus 的对齐复核

日期：2026-10-19
对照版本：`dsh-web-search-plus` v0.1.7（源码快照，`lib/chain.js`、`lib/tavily.js`、
`lib/schema.js`、`lib/index.js`、`lib/errors.js`）
本项目：`hermes-search-router` v0.3.1（含 attempts/backend 对齐）
结论：**架构已经同构；还差的是可观测/探针、多 key 韧性、provider 复用和设置面。**

## 1. 同构的部分

DSH 的 `ctx.web` 与 Hermes 的 `WebSearchProvider` 都是“seam 只选一个 provider”：

```text
web_search
  └─ seam (one provider id)
       └─ web-search-plus / search-router
            ├─ node 1
            └─ node 2
```

| 能力 | DSH v0.1.7 | 我们 v0.3.0 | 状态 |
|---|---|---|---|
| 单 provider 内部优先级链 | `ChainSearchProvider.searchWithReport` 按 `priority` 遍历 | `SearchRouterProvider.search` 按 active + fallback 遍历 | 已对齐 |
| 失败继续下一个 | 每个节点记录 reason 后 `continue` | 每个 backend 记录 attempts 后继续 | 已对齐 |
| 空结果策略 | `fallbackOnEmpty`，默认 **false** | `on_empty: fallback|accept`，默认 **fallback** | 语义有，默认不同 |
| 全部失败 | `WEB_SEARCH_CHAIN_EXHAUSTED` + 逐节点原因 | `search-router: all backends failed: ...` | 语义有，形状不同 |
| 调用方取消不回退 | `throwIfAborted` + abort 穿透 | 同步 provider，无 AbortSignal | 运行模型不同 |
| 可用性检查 | `available()` 廉价同步，链上至少一个可用 | `is_available()` 构建 backends 检查 | 已对齐 |
| 路由报告 | `searchWithReport` 返回 `{result, route, attempts[]}` | 本次新增 `data.backend` + `data.attempts[]` | 已对齐（batch 可读） |
| 链状态探针 | `describe()` 返回每个节点 available/detail | 只有 `selfcheck.py` 的 backend 可用性 | **缺口** |
| 多 key 轮换 | `KeyRing`，最多 20 槽，429/额度冷却，成功复位 | 单 key，按 backend 冷却 | **缺口** |
| 复用官方 provider | 从 registry 取 `deepseek-official` 实例 | 自己实现 DeepSeek/AnySearch adapter | **缺口/可优化** |
| 设置面 | schemastery volatile + 自绘设置页 | `routes.yaml` 热加载 | 不同形态，功能可用 |
| 错误码 | `WEB_SEARCH_CHAIN_EXHAUSTED` 等稳定 code | 字符串 error | Hermes 契约只要求 string |
| 测试 | 238 单测（含 live） | 140 离线测试 | 数量少但覆盖 P0 |

## 2. 本次已经对齐的点

### 2.1 `backend` + `attempts` 路由报告

`SearchRouterProvider.search` 现在在成功/空/失败结果里带：

```json
{
  "backend": "deepseek",
  "attempts": [
    {"backend": "deepseek", "ok": false, "error": "HTTP 429", "elapsed_ms": 120},
    {"backend": "anysearch", "ok": true, "hits": 8, "elapsed_ms": 530}
  ]
}
```

- 对应 DSH `searchWithReport` 的 `route` + `attempts`。
- `batch.py` 把 `attempts` 保留在每条 query 结果里，CLI 在多于一个节点时打印
  `attempts: deepseek(fail) -> anysearch(ok)`。
- 这是“出问题时能看出到底走了哪条链”的最小对齐，不影响 Hermes envelope。

### 2.2 报告质量的 P0

URL 日期兜底、归一化去重、authority、site/lang/after、sort 已完成并实测；
DSH 没有这些报告专用逻辑，这是我们比它更贴近早/午/晚报的部分。

## 3. 仍未对齐、值得做的

### P1：`status / describe / test`（最像 DSH 的下一步）

DSH 有 `describe()` 和 `searchWithReport`，设置页能展示“链上每个节点是否可用、
这次实际命中谁、每个节点耗时/失败原因”。我们对应物是：

- `hermes search-router status`：active、fallback、每个 backend 的 available、
  最近一次 attempts、routes 路径、cooldown（进程内可见部分）。
- `hermes search-router test "<query>"`：跑一次并打印 `attempts` 链。
- `hermes search-router bench`：DS vs AnySearch 同 query 的耗时/条数。
- 0.20.5 不支持 `ctx.register_cli_command` 时，用 `scripts/router_admin.py` 等价实现。

### P2：多 key 池 + provider 复用

- **多 key**：`DEEPSEEK_API_KEYS` / `ANYSEARCH_API_KEYS`（逗号或列表），每个 key
  独立 cooldown；429/402/401 换下一个；成功复位。参考 DSH `KeyRing`，但不必做
  20 槽 UI，先做 2–5 个。
- **provider 复用**：新增 `type: provider` backend，从
  `agent.web_search_registry` 取已注册 provider（如 `anysearch`），直接调用它的
  `search/extract`，对应 DSH 的 `getDeepSeek()` 复用官方实例。这样以后接 Tavily/Exa
  不需要再写 adapter。
- 这两项都是“架构对齐 + 降低维护”，不是报告质量的硬需求。

### P3：设置面 / CI / 安装

- DSH 用 schemastery + 自绘设置页；Hermes 0.20.5 的 `config_schema` 能力要先验证，
  不要直接照搬 Cordis UI。
- GitHub Actions 跑离线测试；安装脚本解决 `hermes plugins install` 顶层/嵌套布局歧义。

## 4. 不建议照搬

- **Cordis client UI / Slot / RPC**：Hermes provider 插件没有等价客户端扩展点。
- **schemastery / volatile 引用树**：Hermes 用 `routes.yaml` + mtime 热加载，已够用。
- **Session 事件**：DSH 明确不写自定义 session event；我们也不应该把路由 attempts
  写进会话历史，只放在 batch/status/日志里。
- **每个 query 都多 provider fan-out**：报告已经用 batch 做跨 query 并发；单 query
  fan-out 成本高、收益不稳定，只保留可选实验。

## 5. 结论

- **架构**：已对齐，不需要改。
- **可观测**：本次 `attempts/backend` 是第一步；P1 的 status/describe/test 是下一块。
- **韧性**：DSH 的多 key 是它最强、我们最缺的部分；如果 batch 并发继续放大，P2 值得做。
- **报告质量**：P0 已领先 DSH 的通用搜索链，继续按早/午/晚报真实数据迭代。
