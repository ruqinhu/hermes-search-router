# Search Router 还能优化什么

日期：2026-10-19
范围：`hermes-search-router` 对照 DeepSeek Harness（DSH）插件与同类开源搜索插件
状态：P0 已实现，通过两轮 review + 服务器验证；P1/P2/P3 待做
结论：架构方向正确；剩余优化空间主要在**报告搜索质量、运维/可观测、配额韧性**，
不在“再换一个搜索后端”。

## 1. 已验证的架构结论

DSH 的 `ctx.web` seam 和 Hermes 的 `WebSearchProvider` seam 是同一个形状：
宿主只选**一个** provider id，失败就是失败，没有内建优先级链。DSH 的
`dsh-web-search-plus` 也是“注册一个 provider，在内部跑链”：

```text
web_search tool
  └─ ctx.web (pinned to web-search-plus)
       ├─ Tavily
       └─ DeepSeek native
```

这和我们的 `search-router` 完全一致：一个 provider，内部 DeepSeek → AnySearch →
script。**不需要推翻架构**。

来源：`D:\codeai\demo-dsh-bundle\README.md`（DSH 扩展点总览）；
<https://github.com/polan-prologue/dsh-web-search-plus>（DSH 内部链、fallbackOnEmpty、
多 key 轮换、settings/status）。

## 2. DSH 插件值得借鉴的点

| 模式 | DSH 做法 | 我们的现状 | 判断 |
|---|---|---|---|
| 单 provider 内部链 | `web-search-plus` 注册一个 provider，内部按优先级调用 | 已做 | 保持一致 |
| 失败/空结果回退 | 每个节点记录原因，`fallbackOnEmpty` 可配 | 已做（`on_empty`） | 已覆盖 |
| 多 key 轮换 | 最多 20 个 key slot，429/额度耗尽冷却后换 key | 单 key | **缺口** |
| 设置页/状态探针 | Plugins 页卡片、status、Try one search | `routes.yaml` + scripts | **缺口** |
| 密钥域 | key 只走 credential 域，列表不回显 | `.env` / 环境变量 | 基本满足 |
| 插件打包 | bundle patch + `dsh plugin add` 自动接 profile | `hermes plugins install` + manual fallback | 可用，安装布局有歧义 |
| 客户端 UI | Cordis client Slot/RPC | Hermes provider 无 UI 扩展 | 不照搬 |

DSH 的 `loommii/dsh-web-search-plus` 还说明：官方 provider 的设置项可以单独做
第三方设置页，通过 settings seam 覆盖，不 monkey-patch。对应到 Hermes，就是
`plugin.yaml config_schema` / 设置页；0.20.5 是否支持要先验证，不能直接照搬。

## 3. 同类开源项目的可借鉴点

| 项目 | 值得借鉴 | 不适合直接照搬 |
|---|---|---|
| [hermes-web-search-plus](https://github.com/robbyczgw-cla/hermes-web-search-plus) | 自动按 query 类型路由；15+9 provider；fallback/empty 语义；`wsp` 原生 backend；status/setup/bench；provider SDK；cache/quality reports | provider 矩阵太大；默认 provider 不含 DeepSeek/AnySearch |
| [agent-web-search](https://github.com/JerryLiu369/agent-web-search) | 多 provider 并发、统一 `results` 结构、model-native grounding、research mode | 它是 tool override/多 provider 并发，不是后端 router；会引入 LLM 合成 |
| [hermes-federated-search](https://github.com/kumaxs/hermes-federated-search) | 一次查询并行 fan-out、合并去重、关键词/LLM 排序 | 只支持 GET custom backend；不支持 extract；成本翻倍 |
| [cascade PR #45399](https://github.com/NousResearch/hermes-agent/pull/45399) | serial/hedge/hybrid、按错误类型 circuit breaker、自动发现 provider | 未合并；hedge 会放大成本；review 仍有问题 |
| [Hermes provider docs](https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin) | 官方 ABC、response envelope、capability flags | — |

## 4. 我们现在的真实缺口

来自服务器 live 数据（2026-10-19）：

- 3 条查询/4 线程：1.12s，first-wave spread 2.14ms，3/3 成功。
- 10 条查询/6 线程：2.31s，spread 3.22ms，10/10 成功。
- `--extract-top 1`：正文 1318 字符。
- 但 10 条查询里 `stale=9`、`date_unknown` 仍有；`--dedupe-urls` 对同一篇
  新浪研报的两个不同 URL 没去掉（路径不同，不是完全相同 URL）。
- DeepSeek 返回 `authority` 0–4，但 batch 没有按权威度过滤/排序。
- 报告常见 query 需要 `site:` / `lang:` / `after:`，目前要手写进 query。
- 没有 `hermes search-router status/test/bench`；只能看 YAML 和日志。
- 单 key；DeepSeek 5 QPS、AnySearch 402/429 没有多 key 轮换。

## 5. 建议的优先级

### P0：报告搜索质量（低风险，直接收益）

**状态：已实现（v0.3.0），并已在服务器用 DeepSeek/AnySearch 实测。**

1. **URL 日期兜底**：DeepSeek 有 `date`，AnySearch 常常没有。对没有 `date` 的 hit，
   从 URL 提取 `/YYYY-MM-DD/`、`/YYYY-MM/DD/`、`/YYYY/MM/DD/`、`/a/YYYYMMDD...`、
   `/pYYYYMMDD...`、`/YYYYMMDD`，填 `date`、`date_source="url"`，再算
   `age_days/stale`。直接减少 `date_unknown` 和旧闻误判。
2. **URL 归一化去重**：去 fragment、去 `utm_*` / `spm` / `scm` / `from` 等追踪参数、
   统一 host/scheme（http/https、`www.` 前缀）、排序 query、去尾部 `/`，用归一化 key
   做跨 query 去重；输出保留原始 URL。能消掉同一 URL 的分享/追踪变体；不同路径的同文
   （例如新浪 `paper.php` 与 `vReport_Show` 两种详情页）不在 P0，需标题相似度，暂不做。
3. **权威度过滤/排序**：`--min-authority 2/3/4`（CLI 校验 0–4）；缺 `authority` 的 hit
   默认保留（AnySearch 没有），另给 `--require-authority` 才丢弃。
4. **查询操作符**：`--site example.com`（可重复，多个按 DeepSeek 文档用 `OR` 连接）、
   `--lang zh`、`--after 2026-10-17`，拼成 DeepSeek 支持的
   `site:... lang:zh after:...`；文档注明 AnySearch 回退可能忽略。
5. **`--sort relevance|date|authority`**：本地排序，只影响返回顺序，不增加请求；
   `authority` 对缺失值保持稳定顺序，不按日期重排。

### P1：运维/可观测（中等收益）

**状态：已实现（v0.4.0）。**

- `hermes search-router status`：active、fallback、每个 backend 可用性、env presence、
  routes.yaml 路径、`last_error`。
- `hermes search-router describe`：同一份信息的链详情。
- `hermes search-router test "<query>"`：跑一条并打印 backend/attempts/耗时/hits，
  可选 `--extract URL`。
- `hermes search-router bench`：DeepSeek vs AnySearch 同 query 耗时/结果数。
- 独立入口 `scripts/router_admin.py`。

### P2：配额韧性（按需）

- DeepSeek/AnySearch 多 key 池：`DEEPSEEK_API_KEYS` / `ANYSEARCH_API_KEYS`（逗号分隔），
  每个 key 独立冷却；429/402/401 换下一个；DSH 最多 20 slot，可参考。
- 可选 fan-out/hedge：单条关键 query 并行打 DS+AnySearch，合并去重；只作为
  `--fanout` 显式选项，不默认开启（成本/延迟）。
- 不建议现在做：每个 query 都 fan-out、LLM 合成、MCP 包装。

### P3：分发/集成（长期）

- GitHub Actions：push 跑 147 个离线测试 + `contract_check.py`。
- 安装脚本/setup 命令，解决 `hermes plugins install` 顶层/嵌套布局歧义。
- `plugin.yaml config_schema` / 设置页（先确认 Hermes 版本支持）。
- 把 `batch_search.py` 接进早/午报 cron 的 prompt（目前只更新了 skill 指引）。
- 服务器 Hermes 已升级到 0.21.5（v2026.9.24），记录见
  [`HERMES-UPGRADE-2026-10-19.md`](HERMES-UPGRADE-2026-10-19.md)。

## 6. 不建议照搬

- DSH 的 Cordis client UI / Slot / RPC：Hermes provider 插件没有同等的客户端扩展点。
- DSH 的 bundle patch：Hermes 用 `plugin.yaml` + `plugins.enabled`，概念不同。
- LLM answer synthesis / research mode：会把确定性 router 变成非确定性 agent。
- 每个 query 都多 provider fan-out：报告已经用 batch 做跨 query 并发，单 query
  fan-out 主要增加成本，收益不稳定。
- 大而全的 provider 矩阵：当前 DS/AnySearch/script 已覆盖需求。

## 7. 推荐下一步

先做 **P0**：URL 日期兜底 + URL 归一化去重 + `--min-authority` + `--site/--lang/--after`
+ `--sort`，并补测试。它直接解决 live 里看到的旧闻/重复/权威度问题，风险最低。
P1 的 status/test/bench 紧随其后；P2 等真正遇到配额瓶颈再做。
