# Change Log

> 每个目标完成后，由 `/ai-sync` 在本文件顶部追加一条记录。

记录格式（复制后填写）：

```
## YYYY-MM-DD

- Goal ID:
- Summary:
- Impact:
- Tests:
- Native review: 未运行则注明
- Commit Status: not committed
```

---

## 2026-07-14

- Goal ID: G02
- Summary: 激活回测基准——510300 ETF → 上证综指 `sh000001`(1990 起,覆盖任意区间);`_benchmark_curve` 直读 `IndexQuoteDaily`、按交集区间算 excess;`metrics` 恒定产出 `benchmark_return`/`excess`/`benchmark_window`(N/A→None+reason)。新增 `get_index_daily`(akshare 优先 + baostock 兜底)+ `run_scheduled_fetch` 每日追加 + `--backfill-benchmark` 全量回补;新增 `IndexQuoteDaily` 模型(映射已存表)。最小范围,不动个股 `quote_snapshot`/`quote_model_for`。
- Impact: `stockfu/backtest/engine.py`(`_benchmark_curve`/`_metrics`/`_get_quote_dict` docstring)、`stockfu/data/akshare_source.py`+`baostock_source.py`(指数日线多源)、`stockfu/models.py`(`IndexQuoteDaily`)、`stockfu/scheduler/jobs.py`(`update_index_benchmark`/接 fetch/回补)、`main.py`(CLI 输出+`--backfill-benchmark`);文档 BACKTEST/PROJECT_STATE/roadmap/CLAUDE。
- Tests: 实证——回测基准 5.89%/超额 -6.63%(N/A 消失);`update_index_benchmark` 追加 6 行 07-06→07-14(需求2 更新机制);A=`[06-01,07-01]` vs B=`[06-01,08-01]` 前缀逐字节一致(防未来函数);2025 回归数值不变。
- Native review: `/ai-check` 编排者评审通过;F1(baostock 兜底)+F2/F3 清理已修并实证;未单独跑 `/code-review`。
- Commit Status: committed `775e881`(main,未推送)
