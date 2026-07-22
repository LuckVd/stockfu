# 实现方案：策略参数变体（一等）+ 回测指标持久化

> **交接文档（2026-07-22）**。新会话从此开始执行。冷启动入口：本文档 + `docs/PROJECT_STATE.md` §0。
> 设计已逐行验证（引擎执行热路径、seed 展开点、持久化链路）。计划原文见 `.claude/plans/`。

---

## 0. 背景 / 动机

当前框架把 `strategy_id` 当 PK，与一份 yaml/config **1:1 绑死**：调一个参数（如 `dividend_cross_section` 止损 8%→30%）只能覆盖原策略，两套参数无法共存。已验证 30% 止损明显优于 8%（年化 7.99→10.65、夏普 0.63→0.78、止损损失 60.6万→8.4万、回本 356d→9d），用户要「都保留」并希望框架原生支持「同类、不同参数」共存。

同时，回本天数 / 买入个数 / 止损成交 / 止损损失 / 胜率这些对比指标目前每次都要从 `.json.gz` 重算；且止损损失因成交单丢失 `signal`（`engine._exec` 硬写 `signal=None`）只能靠 D+1~D+3 窗口估算。要把它们在引擎里算好并持久化，并新增 4 个水下深度分布指标。

**目标产物**：一个 base yaml 可声明 `variants:`，seed 展开成多条共存策略（复合 id），catalog/run_id/产物互不覆盖；回测引擎原生产出全部对比指标并自动写入 `.json.gz` + `.meta.json`。

## 0.1 当前状态（执行前）

- **main 工作树**：干净（`dividend_cross_section.yaml` 已还原为 8% base，无 risk 块）。本会话提交的文档变更见末尾 commit。
- **目标工作区**：`/opt/pro/stockfu-backtest`（分支 `feature/backtest`，已含回测性能预载 `facd00d`/`61c5a99`/`bae713c`/`7f16d9e`/`647d4d4`）。**切入此 worktree，新开分支 `feature/strategy-variants`** 隔离本工作。
- 不同 worktree 用不同 DB 路径（AGENTS.md 约定），避免 SQLite 写锁竞争。
- 基线产物已备份：`/tmp/bt-backup-sl08/`（8%）与 `data/backtest/upd-dividend_cross_section-2026-07-21.json.gz`（30%，本会话实验跑）。

## 0.2 新会话启动步骤
1. `cd /opt/pro/stockfu-backtest`；`git fetch && git checkout feature/backtest && git pull`。
2. 本文档在 main：`cat /opt/pro/stockfu/docs/STRATEGY_VARIANTS_PLAN.md`（同机绝对路径，跨 worktree 可读）；或 `git merge main -- docs/STRATEGY_VARIANTS_PLAN.md` 拉入本 worktree。
3. `git checkout -b feature/strategy-variants`。
4. 按 Workstream A → B 顺序实现，每步对照下方 file:line。

---

## 核心设计决定

**把变体编码进 `strategy_id` 字符串**（`base#key`，分隔符 `#`，已验证：SQLite TEXT PK / POSIX 文件名 / JSON key / dict key / bash 词中 `#` 均安全）。这样 `_CATALOG_BY_ID`、`run_id`、PK、meta 自动消歧。**新增的真正逻辑只有 seed 的展开器 + recommend 改读 DB config**。

**指标持久化零改动**：往 `metrics` dict 加 key 即自动进 `.json.gz` + `.meta.json`（`scheduler.py:49,154`，`_write_meta` 整 dict 落盘）；全仓库消费方均 `.get()`，加 key 不破坏任何东西。

---

## Workstream A — 策略变体

### A1. `stockfu/ai/operators/seed.py` — 变体展开器（核心）
- `_STRATEGIES`(:35)：不变，仍是 **base** stem 清单。
- 新增 `_deep_merge(base, override)`（嵌套 dict 递归；list/标量整体替换）与 `_expand_variants(base_id, text)`：载入 base yaml 一次；若含 `variants:`，对每条 `override:` 深合并进 deepcopy 的 cfg、剥掉 `variants` 键、`yaml.safe_dump(allow_unicode=True, sort_keys=False)` 重序列化，产出 `[(base_id, base_name, text, False)] + [("{base}#{key}", vname, vtext, True)]`。base 行保留原文（注释不丢），变体行合成。
- `seed_operators_and_strategies` 循环(:81-87)：改成
  ```python
  for sid in _STRATEGIES:
      name, text = _load_strategy_yaml(sid)
      for vsid, vname, vtext, derived in _expand_variants(sid, text):
          _upsert_strategy(s, vsid, vname, vtext, derived=derived)
          n += 1
  ```
- `_upsert_strategy`(:185-192)：加 `derived: bool = False`。**变体行(derived=True)每次 seed 强制重同步** name+config（消除「改 yaml 不 reseed 不生效」的坑）；base 行保留现有 insert-if-not-exists（用户热改生效）。
  ```python
  def _upsert_strategy(s, sid, name, yaml_text, *, derived: bool = False) -> None:
      existing = s.get(Strategy, sid)
      if existing:
          if derived:
              existing.name = name; existing.config = yaml_text
          return
      s.add(Strategy(strategy_id=sid, name=name, config=yaml_text))
  ```

### A2. `stockfu/ai/operators/runner.py`
- `compile_strategy`(:327)：加可选 `strategy_id: str = ""` 形参，构造时 `CompiledStrategy(strategy_id=strategy_id, ...)`。`CompiledStrategy.strategy_id`(:69) 无需改 schema；`get_active_strategy`(:366) 仍权威覆盖。

### A3. `stockfu/backtest/full_cycle_update.py`
- `FULL_CYCLE_CATALOG`(:76-79 后) 加一条：
  ```python
  StrategyRunSpec("dividend_cross_section#sl30", "cap_and_rank", dict(_CS),
      universe="all", strict=True, min_amount=MIN_AMOUNT, tier="hot"),
  ```
- `_CATALOG_BY_ID`(:125) / `run_one` run_id(:270 `upd-{strategy_id}-{end}`) / `resolve_specs` 未知 id 报错(:156)：strategy_id 是复合串即自动消歧，无需改逻辑。`print_catalog` 宽度(:453,457) `{:36}`→`{:40}`。

### A4. `stockfu/services/recommend.py`
- 删 `_load_yaml`(:91-95，1:1 文件名查找，复合 id 无文件)。`pick_strategy`(:384-385) 改读 DB：
  ```python
  from stockfu.models import Strategy
  with session_scope() as s:
      row = s.get(Strategy, spec.strategy_id)
      if row is None: raise ValueError(f"策略 {spec.strategy_id} 不在 DB(先 --init-db/seed)")
      yaml_text = row.config
  cs = compile_strategy(yaml_text, strategy_id=spec.strategy_id)
  ```
- `_CATALOG_BY_ID`(:54) 已 import 自 full_cycle_update，保持单一真源（可选 `from stockfu.backtest.full_cycle_update import _CATALOG_BY_ID` 去重）。

### A5. `stockfu/models.py`
- `Strategy`(:278) PK 保持复合 `strategy_id`，**不加列**（base 可 `strategy_id.split("#")[0]` 取回；无分组查询需求；避免迁移）。YAGNI。

### A6. `main.py`
- `run_backtest`(:437)：`set_app_config` 前校验 `s.get(Strategy, strategy)` 存在，否则 `SystemExit` 给出可用清单（避免静默回落 pure_factor）。`--list-strategies`(:790) 自动打印变体 id。

---

## Workstream B — 新指标（引擎算好，自动持久化）

### B1. `stockfu/backtest/engine.py` `_metrics()`(:557-613) — 扩展峰值循环(:577-582)
在同一遍 `eq`/`peak` 循环里累加水下直方图、记录最大回撤的峰/谷 index；循环后算回本。guard 在 `if eq and initial>0`(:572) 内，空 `eq` 给 `None`。
- `max_drawdown_recovery_days`（谷底→净值收回前高的交易日数；未回本=`None`）+ `max_drawdown_recovered`(bool)。
- `underwater_pct_gt0 / _ge10 / _ge20 / _ge30`：权益低于运行峰值 0/10/20/30% 的交易日占比%（drawdown=(peak-v)/peak）。
```python
peak, max_dd = eq[0], 0.0
last_peak_idx = 0; max_dd_peak_idx = 0; max_dd_trough_idx = 0
u0=u10=u20=u30=0
for i, v in enumerate(eq):
    if v > peak: peak = v; last_peak_idx = i
    if peak > 0:
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd; max_dd_peak_idx = last_peak_idx; max_dd_trough_idx = i
        ddp = dd * 100
        if ddp > 0:  u0 += 1
        if ddp >= 10: u10 += 1
        if ddp >= 20: u20 += 1
        if ddp >= 30: u30 += 1
out["max_drawdown"] = round(max_dd * 100, 2)
peak_val = eq[max_dd_peak_idx]
rec_idx = next((j for j in range(max_dd_trough_idx, len(eq)) if eq[j] >= peak_val), None)
out["max_drawdown_recovered"] = rec_idx is not None
out["max_drawdown_recovery_days"] = (rec_idx - max_dd_trough_idx) if rec_idx is not None else None
n = len(eq) or 1
out["underwater_pct_gt0"]  = round(u0/n*100, 1)
out["underwater_pct_ge10"] = round(u10/n*100, 1)
out["underwater_pct_ge20"] = round(u20/n*100, 1)
out["underwater_pct_ge30"] = round(u30/n*100, 1)
```

### B2. `engine.py` Stage B(:1099-1136 后) — 成交类指标
```python
metrics["distinct_stocks_bought"] = len({t["code"] for t in filled if t.get("kind") in ("buy","add")})
_sl = [t for t in filled if t.get("signal") == "stop_loss"]
metrics["stop_loss_count"] = len(_sl)
metrics["stop_loss_realized_loss"] = round(sum((t.get("pnl") or 0.0) for t in _sl), 2)  # 负数=亏损
```

### B3. `engine.py` 止损信号穿透（最脆，已逐行验证执行中性）
加一个与 `pending_target` **同生命周期**的 `pending_signal`（`pending_target` 在 :772 声明于日循环外、靠 :875 `=still_pending` 不重置而跨 D→D+1 存活；`_sig`(:949) 是每日重建——这正是信号丢失根因）。6 处编辑：
1. **:772** 声明 `pending_signal: dict[str, str | None] = {}`（挨着 `pending_target`）。
2. **:804** 声明 `still_signal: dict[str, str | None] = {}`（挨着 `still_pending`）。
3. **:807** 循环顶 `sig = pending_signal.get(code)`；**:812** 与 **:844** 两条 defer 路径都 `still_signal[code] = sig`。
4. **:852-858** `_exec`：开头 `sig = extra.pop("signal", None)`（避 kwarg 撞），改 `tr.update(..., signal=sig, reason="open_exec", ..., **extra)`（替换 `signal=None` 字面量）。
5. 调用点传 signal：**:862** `_exec(code, tw, px, source, signal=sig)`；**:873-874** `_exec(code, stw, spx, source, signal=sig, **({"cash_scaled": round(safety,4)} if constrained else {}))`。
6. **:875** `pending_signal = still_signal`；**:1054** `pending_signal[code] = _sig.get(code)`（与 `pending_target[code] = target` 并排）。
> **执行中性**：`signal` 纯属 trade dict 元数据；成交决策只走 `target_weight`→`resolve_action`(:815/:853)，未动。副产品：`universe_st_exit` 信号(:960) 也被正确穿透。

### B4. `stockfu/backtest/scheduler.py` — schema bump（推荐）
`persist["schema_version"] = 1`(:154) 与 meta 默认(:40) → `2`；`full_cycle_update.py:425` 同步→2。纯加字段、向后兼容；bump 让 §0.6 生成可据 `>=2` 判定止损/回本是「信号直传精确值」而非窗口估算。

### B5. `docs/BACKTEST.md` §8 指标表(:231-246)
追加行：`max_drawdown_recovery_days` / `max_drawdown_recovered` / `distinct_stocks_bought` / `stop_loss_count` / `stop_loss_realized_loss` / `underwater_pct_gt0|ge10|ge20|ge30`。

### B6. 汇总露出
- `full_cycle_update.py` summary pick-list(:302-312) 加：`max_drawdown_recovery_days`、`max_drawdown_recovered`、`distinct_stocks_bought`、`stop_loss_count`、`stop_loss_realized_loss`（+可选 `underwater_pct_ge20`）——1:1 映射 §0.6 对比表。
- `main.py` 单行 print(:477-481) 加：`| 回本 {recovery_days or '未回本'}d | 止损 {stop_loss_count}笔`。

---

## 首个具体用例：`stockfu/ai/strategies/dividend_cross_section.yaml`
base 已是 8%（无 risk 块）。末尾加：
```yaml
variants:
  - key: sl30
    name: 红利横截面(止损30%)
    override:
      risk:
        stop_loss: 0.30
```
seed → 两行：`dividend_cross_section`(8%) 与 `dividend_cross_section#sl30`(30%)，并存。

---

## 测试（沿用 `tests/` unittest 风格，无 DB 依赖优先）
- **`tests/test_metrics_recovery.py`**：合成 `equity_curve` 验 `_metrics` 新键——`[100,90,80,100]`→回撤20%/回本1d/recovered=True；`[100,80,90]`→未回本/None；水下分桶手算核对；断言旧 `max_drawdown` 不变（回归）。
- **`tests/test_seed_variants.py`**：`_expand_variants`/`_deep_merge`——base+override 深合并（`risk.stop_loss` 换叶、`risk.portfolio_brake` 保留）、变体 id `base#sl30`、变体 cfg 无 `variants` 键、list 整体替换。
- **`tests/test_variant_e2e.py`**（thin，skip-if-no-DB）：seed 出两行；`resolve_specs([...])` 返回 2 条；短区间 `--update-backtests` 产出两份独立产物、新指标齐全、sl30 的 `stop_loss_count` < 8% base。

## 风险 / 回滚
- **最高风险 `_exec` 形参**：开头 `pop("signal", None)` 使其免疫 kwarg 撞（与调用方是否传 signal 无关）。回滚=撤 6 处引擎编辑（无 schema 变更，原子）。
- **变体行每次 seed 重同步**：DB 里手改变体行会在下次 seed 丢失（设计如此，文档化）。
- **schema→2**：纯加字段，旧 `list_runs`/`load_run` 忽略未知键，无需迁移。
- **§0.6 数字会小幅变化**：止损/回本从窗口估算变信号直传精确值；重跑 `--update-backtests` 刷新。

## 验证序列
1. `python3 -m pytest tests/test_metrics_recovery.py tests/test_seed_variants.py -v`（单元，无 DB）。
2. `python3 main.py --init-db` → `SELECT strategy_id FROM strategy WHERE strategy_id LIKE 'dividend_cross_section%'` 见两行；变体 config 含 `stop_loss: 0.30`。
3. `python3 main.py --update-backtests --list` 打印两个 id。
4. `python3 main.py --update-backtests --strategies dividend_cross_section,dividend_cross_section#sl30 --start 2025-01-01 --end 2025-03-01` → 两份 `upd-dividend_cross_section*-2025-03-01.*`，独立文件，metrics 含全部新键。
5. 抽查：sl30 的 `stop_loss_count` 严格小于 8% base。
6. `python3 main.py --backtest 'dividend_cross_section#sl30' --codes all --start 2025-06-01 --end 2025-08-01` 不回落 pure_factor（A6 校验通过）。

## 关键文件
`stockfu/backtest/engine.py`(B1/B2/B3) · `stockfu/ai/operators/seed.py`(A1) · `stockfu/backtest/full_cycle_update.py`(A3/B6) · `stockfu/services/recommend.py`(A4) · `stockfu/ai/operators/runner.py`(A2) · `stockfu/ai/strategies/dividend_cross_section.yaml`(用例) · `stockfu/backtest/scheduler.py`(B4) · `docs/BACKTEST.md`(B5) · `main.py`(A6/B6) · `tests/`(新增 3 个)。
