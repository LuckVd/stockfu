---
name: backfill-universe-quotes
description: A股行情数据库落后于最新交易日时,补全 hs300+zz500 成分行情到指定日期。触发:quote_snapshot / index_quote_daily / etf_quote_daily 的 max(quote_date) 早于最近已收盘交易日,或用户要求"补数据 / 补行情 / 更新到今天 / 到 X 日"。
---

# 回补全成分行情到指定日期

把 hs300+zz500 时点成分(~800 只活跃大盘股)的 baostock 三复权行情补到目标交易日,写 `quote_snapshot`。与自选抓取(`run_scheduled_fetch` / `--fetch`)互补。

## 何时用

- 三张行情表 `quote_snapshot` / `index_quote_daily` / `etf_quote_daily` 的 `max(quote_date)` < 最近已收盘交易日
- 用户要求"补数据 / 补行情 / 把数据更新到今天 / 到 X 日"
- **区分口径**:自选 ~45 只用 `python3 main.py --fetch --date`;全成分 ~800 只用本 skill

## 流程

### 1. 确认缺口(三表 max date)

```python
import sqlite3
c = sqlite3.connect("data/stockfu.db")
for t in ["quote_snapshot","index_quote_daily","etf_quote_daily"]:
    print(t, c.execute(f"select max(quote_date) from {t}").fetchone()[0])
```

列出要补的交易日(跳过周末/节假日)。当日合法性:**北京时间 15:30 后**才算已收盘。

### 2. 逐日回补(主线程直连)

```python
from stockfu.scheduler.jobs import fetch_universe_quotes
for d in ["2026-08-05","2026-08-06","2026-08-07","2026-08-10"]:  # 缺口日
    r = fetch_universe_quotes(d)
    print(d, r)   # {'total':800,'ok':800,'fail':0,'elapsed_sec':...}
```

- 默认 `BAOSTOCK_PROXY_MODE=direct`(baostock 国内裸 TCP 直连最快最稳:~1.3s login、~0.28s/只)
- 800 只 ≈ 4 分钟/天;多天可后台跑:
  ```bash
  python3 -u > data/logs/fetch_universe.log 2>&1 <<'PY'
  from stockfu.scheduler.jobs import fetch_universe_quotes
  for d in [...]: print(d, fetch_universe_quotes(d), flush=True)
  PY
  ```

### 3. 验证入库

```python
import sqlite3
from datetime import date
from stockfu.services.index_universe import current_member_codes, HISTORICAL_INDEX_CODES
c = sqlite3.connect("data/stockfu.db")
members = set(current_member_codes(date(2026,8,10), HISTORICAL_INDEX_CODES))
for d in ["2026-08-05","2026-08-06","2026-08-07","2026-08-10"]:
    hit = c.execute("select count(distinct asset_code) from quote_snapshot where quote_date=? "
                    "and asset_code in (%s)" % ",".join("?"*len(members)),
                    [d, *members]).fetchone()[0]
    print(d, hit, "/", len(members))   # 应 800/800
```

三表 max 应到目标日;抽查一只三复权(`close / close_raw / close_qfq / close_hfq / pe / pb`)非空。

## 关键约束(踩过的坑)

- **主线程串行**:`fetch_universe_quotes` 故意主线程循环,绕开 `_upsert_quote` 的 `_call_timeout`。后者把抓取丢进双层子线程,baostock 全局 socket 登录态保不住,会陷入 login 循环(每只重 login、不入库)。
- **退市自动跳过**:成分来自 `current_member_codes`(hs300+zz500 时点成员,已过滤退市);即便混入,baostock 对退市/停牌当日返回 empty → `_fetch_today_via_baostock` 返回 False,静默跳过、不污染。
- **cap_date 防未来**:`upsert_quote_snapshot` 硬保证 `quote_date <= target`,回补历史日不会写入之后的 bar。
- **窗口语义**:每次 `fetch_universe_quotes(td)` 实际写 `[td-20天, td]`(每只拉近 20 天三复权,MERGE 不删已有)。**补到最新日期足够**;补长历史范围需循环跑每个历史交易日。
- **三复权口径**:raw(股息率分母/PE/PB)+ qfq(收益净值)+ hfq(对账),见 `docs/BACKTEST.md` §0。

## 相关入口

| 需求 | 用什么 |
|---|---|
| 自选 ~45 只(+指数+ETF+三层情绪,每日例行) | `python3 main.py --fetch --date YYYY-MM-DD` |
| **全成分 ~800 只行情** | **本 skill(`fetch_universe_quotes`)** |
| 全 A 长历史复权 K 线 | `python3 main.py --backfill-adj-prices` |
