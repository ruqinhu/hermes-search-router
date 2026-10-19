# 早/午/晚报搜索 playbook

这份文档来自服务器上早报、午报、盘前简报和即时报告的真实运行经验，目标是把
“报告里怎么搜、怎么并发、怎么降级、怎么防旧闻”固化到本项目里。

## 1. 先选对工具

| 场景 | 用什么 | 原因 |
|---|---|---|
| 单条新闻/事件/定性查询 | Hermes `web_search` | 走 `search-router`，DeepSeek 主、AnySearch 备，带 authority/date |
| ≥4 条互不相关的查询 | `scripts/batch_search.py` | 真并发（≤12 线程）、单条重试/失败隔离、JSON 结果 |
| 行情/公告/龙虎榜/涨停池/板块成分 | 服务器直连采集脚本 | 结构化数据直连 API 比通用搜索稳定，别塞进 router |
| 需要网页正文 | `web_extract` 或 `batch_search.py --extract-top N` | 先搜索定位，再抽正文 |

**关键结论**：工具层连续发多个 `web_search` 是串行的，不会真提速；报告里 ≥4 条
查询必须绕开工具层，用 `batch_search.py` 的线程池。

## 2. 真实实测数字（服务器）

- 早报 7 个独立采集源：串行 **65.3s → 6 线程 15.9s**（约 4x）；`--all` 10 源约 25.4s。
- 6 条搜索查询：串行 **14.5s → 并发 2.8s**（约 5.2x）。
- 并发上限 **12**；超过 12 会触发后端限流/连接被掐。
- 单任务超时参考：采集源 240s；搜索查询通常 1–3s，整批 30s 内应完成。
- 报告时间盒：早报 07:00、午报 12:40、盘前 08:50、手动即时报告 20 分钟。

## 3. batch_search.py

路径：`~/.hermes/plugins/web/search-router/scripts/batch_search.py`

```bash
# 默认：general profile，6 条/查询，6 线程
python3 batch_search.py "澜起科技 公告" "存储 涨价" "美股 隔夜 科技股"

# 新闻 profile：8 条/查询，3 天时效
python3 batch_search.py --profile news --workers 6 \
  "澜起科技 公告" "兆易创新 龙虎榜" "地产 政策 深圳"

# 从文件/stdin 读，落 JSON
python3 batch_search.py --file queries.txt --dedupe-urls --json --out /tmp/batch.json
printf 'q1\nq2\n' | python3 batch_search.py --stdin --workers 4

# 对每条查询前 1 条抽正文（最多 4000 字符）
python3 batch_search.py --extract-top 1 "澜起科技 公告"
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--profile` | `general` | `general` / `news` / `research` |
| `--workers` | 6 | 夹到 1–12 |
| `--count` | profile | 每条查询结果数，夹到 1–50 |
| `--retries` | 1 | 单条失败重试次数，夹到 0–3 |
| `--max-age-days` | profile | 超过则标 `stale`；`news`=3，`general`=7 |
| `--dedupe-urls` | 关 | 跨查询按归一化 URL 去重，保留首次出现 |
| `--min-authority` | 关 | 丢掉 authority 低于阈值的 hit（0–4） |
| `--require-authority` | 关 | 连没有 authority 的 hit 也丢掉 |
| `--sort` | `relevance` | `relevance` / `date` / `authority` |
| `--site` | 关 | 给每条 query 追加 `site:domain`，可重复 |
| `--lang` | 关 | 追加 `lang:zh` 等 |
| `--after` | 关 | 追加 `after:YYYY-MM-DD` |
| `--extract-top` | profile | 每条查询前 N 条抽正文；`research`=2 |
| `--extract-max-chars` | 4000 | 正文截断 |
| `--json` / `--out` | — | JSON 输出/落盘 |
| `--fail-on-error` | 关 | 任一查询失败则退出码 1 |

输出结构：

```json
{
  "ok": true,
  "summary": {
    "queries": 3, "workers": 6, "succeeded": 3, "failed": 0,
    "wall_ms": 4200, "search_wall_ms": 3800, "spread_ms": 12.4,
    "stale": 1, "date_unknown": 2, "duplicates_removed": 0,
    "filtered_authority": 0, "min_authority": null, "sort": "relevance"
  },
  "results": {
    "澜起科技 公告": {
      "ok": true, "backend": "deepseek", "tries": 1, "elapsed_ms": 900,
      "stale_count": 0,
      "hits": [
        {"title": "...", "url": "...", "date": "...", "date_source": "field",
         "age_days": 1.2, "stale": false, "date_unknown": false,
         "authority": 4, "backend": "deepseek"}
      ]
    }
  }
}
```

- `date_source` 为 `field`（搜索结果自带）、`url`（URL 日期兜底）或 `null`。
- URL 归一化去重会去掉 fragment、`utm_*`/`spm`/`scm`/`from` 等追踪参数；输出仍保留原 URL。

- `results` 按输入查询顺序返回，不按完成顺序。
- 单条失败只影响该 query，`ok=false` + `error`，其余照常。
- `spread_ms` 是**第一波**线程启动时间极差，`< 20ms` 说明第一波是真并发；查询数大于
  workers 时，后续波次必然排队，`spread_ms` 不会包含它们。

## 4. 时效与旧闻

报告最容易犯的错：把旧闻当今日。`batch_search.py` 会为每条 hit 补：

- `date`：DeepSeek 的发布时间；AnySearch 没有时，从 URL 日期兜底。
- `date_source`：`field`（搜索结果自带）、`url`（URL 兜底）或 `null`。
- `age_days`：相对当前 UTC 的天数。
- `stale`：超过 `--max-age-days`。
- `date_unknown`：字段和 URL 都拿不到日期时为 true。

建议：

- 新闻/公告类 `--profile news`（3 天）；如果只写“今日”，把 `--max-age-days 1`。
- 标题戏剧性（暴跌/暴涨/万亿/史诗级）时，先看 `date` / `stale` / `date_source`，再写入“今日”。
- URL 日期兜底覆盖：新浪 `/YYYY-MM-DD/`、上海证券报 `/YYYY-MM/DD/`、`/YYYY/MM/DD/`、
  东财 `/a/YYYYMMDD...`、中财 `/pYYYYMMDD...`；`date_unknown=true` 不代表假，只代表
  不能证明日期。
- `--dedupe-urls` 用归一化 URL（去 fragment、`utm_*`/`spm`/`scm`/`from` 等追踪参数）
  跨查询去重，能消掉同一篇研报的不同分享链接。

## 5. 源选择与降级

- **DeepSeek Search**：中文新闻/政策/事件质量好，带 `authority`、`rerank_score`、`date`；
  默认主源。
- **AnySearch**：DeepSeek 失败/空结果时自动回退；匿名可用，402/429 时不要硬撞。
- **直连采集**：财联社电报、东财公告、龙虎榜、同花顺快讯、新浪行情/收评，结构化数据
  优先直连；通用搜索只做补充。
- 百度/东财股吧/B站等反爬：**检测到拦截就直接换源**，不要反复换 UA/URL 重试。
- 旧脚本 `concurrent_search.py` 仍是 AnySearch-only；需要 DeepSeek 质量时用
  `batch_search.py`。
- 权威度：DeepSeek `authority` 0–4，`>=3` 为权威、`4` 为极权威。`--min-authority 3`
  过滤低权威源；`--require-authority` 连缺字段的也丢。AnySearch 没有 authority，
  默认保留。
- 查询操作符：`--site cnstock.com`（可重复，多个用 OR）、`--lang zh`、
  `--after 2026-10-17` 会拼成 DeepSeek 支持的 `site:... lang:zh after:...`；
  AnySearch 回退可能按字面处理，报告里以 DeepSeek 结果为准。
- `--sort authority|date` 只调整本地返回顺序，不会重新请求更多结果。

## 6. 报告集成示例

早报（07:00）新闻面：

```bash
python3 ~/.hermes/scripts/collect_market_all.py --workers 6 --ann-days 1 --out /tmp/mkt.json
python3 ~/.hermes/plugins/web/search-router/scripts/batch_search.py \
  --profile news --workers 6 --dedupe-urls --json --out /tmp/news.json \
  "澜起科技 公告" "兆易创新 龙虎榜" "存储 涨价" "美股 隔夜 科技股"
```

只想要权威源、并按权威度排序：

```bash
python3 ~/.hermes/plugins/web/search-router/scripts/batch_search.py \
  --profile news --workers 4 --min-authority 3 --sort authority \
  --site cnstock.com --site stcn.com --after 2026-10-15 --json \
  "澜起科技 公告" "存储 涨价"
```

> `--site` / `--lang` / `--after` 拼的是 DeepSeek 查询语法；AnySearch 回退可能按字面
> 处理，报告里以 DeepSeek 结果为准。`--min-authority` 只过滤已知 authority 的 hit；
> 缺字段的 hit 默认保留，`--require-authority` 才丢弃。

午报（12:40）热点补充：

```bash
python3 ~/.hermes/plugins/web/search-router/scripts/batch_search.py \
  --profile news --workers 4 --count 6 --max-age-days 1 \
  "上午 涨停 板块" "午后 政策 催化" "持仓 异动"
```

盘前（08:50）外围：

```bash
python3 ~/.hermes/plugins/web/search-router/scripts/batch_search.py \
  --profile news --workers 4 \
  "美股 隔夜 指数" "费城半导体 指数" "A50 期货" "隔夜 大宗商品"
```

## 7. 边界

- 不把结构化采集塞进 router：router 负责搜索，采集脚本负责行情/公告/龙虎榜。
- 不在同一轮里连续发多个 `web_search` 期待并发；用 batch 脚本。
- 不超过 12 线程；不要改大常量。
- 单条失败重试 1 次即可；反爬/402/429 应换源或降级，不要无限重试。
- 模型“思考+组织语言”的耗时不计入搜索耗时；报告只报脚本/工具真实耗时。
