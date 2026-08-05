# 数据源限流与安全频率参考

> 最后更新：2026-08-05
>
> **重要**：以下频率均为项目实际运行中迭代出的经验安全值，非服务商官方文档阈值。
> 同花顺和 BaoStock 均未公开 rate limit 文档。

---

## 一、同花顺 (10jqka.com.cn)

| 端点 | 用途 | 安全间隔 | 代码位置 |
|------|------|---------|----------|
| `d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js` | 行业板块日K线历史回补（当日通常仅到 T-1） | **0.3s** / 行业 / 年 | `stockfu/data/akshare_source.py:get_sector_kline_period` |
| `d.10jqka.com.cn/v4/line/bk_{code}/01/today.js` | 行业板块当日实时/收盘 K 线（年度归档的当天补充） | **0.3s** / 行业 | `stockfu/data/akshare_source.py:get_sector_kline_period` |
| `data.10jqka.com.cn/funds/hyzjl/` (via akshare) | 当日全行业资金流快照 | **0.3s** / 行业 | `stockfu/services/backfill.py:415` |
| `q.10jqka.com.cn/thshy/` (via akshare) | 行业分类清单 | **缓存 10 分钟** | `stockfu/data/akshare_source.py:624` |

### 反爬机制

- **JS 动态令牌**：akshare 使用 `py_mini_racer` 执行混淆后的 `ths.js` 生成 `Cookie: v=` 和 `hexin-v:` 头。
- **资金流端点** (`data.10jqka.com.cn`) 强制要求 JS 令牌，已通过 akshare 封装处理。
- **K线端点** (`d.10jqka.com.cn`) 目前不强制校验 JS 令牌，但项目 `get_sector_kline_period` 裸请求未带令牌。若同花顺升级反爬，此端点会首先失效。
- **当日 K 线语义（当前实现）**：年度文件是 T+1 归档，不能用于判断当天是否有行业日线；`get_sector_kline_period` 会在请求范围包含本机当天时合并 `today.js`，以其 `1/7/8/9/11/13/19` 字段写入当日 OHLCV/成交额。历史日期不读取 `today.js`，避免错标数据。
- **韧性（当前实现）**：`get_sector_kline_period` 加失败重试(初试+2次,退避 0.6/1.2s)+ WARNING log(catalog 缺失 / HTTP 非200 / 异常 / 正文解析失败 各自打点),端点故障不再静默 `return []`;`backfill_sector_pulse_history`(`backfill.py:330`) 连续 15 次无效请求中止回补 + 结束打印失败汇总。

### 频率特点

- 容忍度较高，0.3s 间隔从未触发过明显限流。
- 响应快，约 70ms，远优于东财。
- 无已知 TLS 指纹层面的封禁。

---

## 二、东方财富 (East Money)

| 端点 | 用途 | 安全间隔 | 代码位置 |
|------|------|---------|----------|
| `stock_sector_fund_flow_hist` (via akshare) | 行业历史主力资金流 | **1.0-1.2s** / 行业 | `stockfu/services/backfill.py:300` |
| `limit_up_at` | 涨停/连板数据按天回补 | **2.0s** / 天，连续失败 10 次中止 | `stockfu/services/backfill.py:69,99` |
| ETF qfq 日线 (via akshare) | 东财 ETF 前复权 | 重试退避 **0.8×(n+1)s**，最多 3 次 | `stockfu/data/akshare_source.py:251` |
| `push2.eastmoney.com` / `push2his` | K线/资金流历史 | **已完全封死** | — |

### 反爬机制

- **TLS 指纹 (JA3) 封禁**：`push2`/`push2his` 基于 TLS 指纹拦截非浏览器请求，curl/requests/curl_cffi 全部无效。
- 替代方案：实时数据用 `push2delay.eastmoney.com` 或 `/webguest/` 前缀；历史 K 线只能通过真实浏览器。
- 显著比同花顺更激进，需更大间隔 + 重试 + 断点续传。

### 频率特点

- 非常敏感，是最不可靠的数据源。
- `backfill_limit_up` 有专门保护：连续失败 10 次自动中止（疑似限流）。

---

## 三、BaoStock

| 端点 | 用途 | 安全间隔 | 代码位置 |
|------|------|---------|----------|
| 三复权日线回补 | 每只票 raw/hfq/qfq | **0.15s** / 只票 | `stockfu/scheduler/backfill_adj_prices.py:145` |
| 指数成分快照 | 沪深300/中证500 成分历史 | **0.3s** / 日期 | `stockfu/services/index_universe.py:410` + `docs/baostock-api/` |
| 换代理冷却 | 代理池切换后休眠 | **0.3s** | `stockfu/data/baostock_proxy.py:89` |
| 登录重试 | 重登后等待 | **1.0s** | `stockfu/services/index_universe.py:342` |

### 特点

- 免费、结构化数据，主力分红源。
- `baostock` 官方序列化设计（同一 IP 同一时间只能一个 session），配合代理池实现串行单 IP。
- 项目通过免费代理池 (`BaostockProxySession`) 实现失败自动换 IP + 冷却。
- 换代理冷却 0.3s，rebootstrap 冷却 ≥60s。

---

## 四、腾讯 (Tencent)

| 端点 | 用途 | 安全间隔 | 代码位置 |
|------|------|---------|----------|
| `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | ETF qfq 分段拉取 | **0.15s** / 分段 (~2年/段) | `stockfu/data/akshare_source.py:333` |
| 同上 | 个股前复权日K | **无显式 sleep** | `stockfu/data/tencent_source.py` |

### 特点

- 代码注释："不走东财，不受东财限流/反爬影响"。
- 独立于东财/同花顺，作为 A 股日 K 的第二 fallback（第一 efinance）。
- 限制最宽松，目前未遇到限流问题。
- ETF 分段拉取每段最多 800 根 K 线，qkqday 字段会自动回落 day。

---

## 五、efinance

| 端点 | 用途 | 安全间隔 | 代码位置 |
|------|------|---------|----------|
| `efinance` 库 | A 股实时行情 + 日 K | **无显式 sleep** | `stockfu/data/efinance_source.py` |

### 特点

- A 股实时行情首选源，免费无 token。
- `get_realtime_quotes` 支持批量，一次请求覆盖多只票。
- 日 K 强制前复权 `fqt=1`。
- 无已知限流问题。

---

## 总结速查

```
同花顺 K线    → 0.3s       宽松,裸HTTP可用;失败重试2次+WARNING,回补连续15次空中止
同花顺 资金流  → 0.3s       走akshare，已有JS令牌保护
东财   资金流  → 1.0-1.2s   敏感，易限流
东财   涨停    → 2.0s       非常敏感，连续失败10次自动中止
东财   push2   → 封死       TLS指纹，无法使用
BaoStock       → 0.15-0.3s  免费代理池 + 换IP冷却
腾讯           → 0.15s      最宽松，无已知限流
efinance       → 无限制     目前无问题
```
