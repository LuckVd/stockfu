# Project Constraints

- Prefer extending existing modules over adding parallel abstractions（参考 `docs/PROJECT_STATE.md` 第 2 节 5 层架构）。
- Add project-specific testing or release gates here when needed.

## StockFu 工程硬约束

- **运行环境**：Python 用 `python3`（`python` 不存在）；系统 Python PEP668，pip 装 包加 `--break-system-packages`。
- **代理分流**：港美股（yfinance）走 mihomo:7890（`source /opt/clash/proxy.sh`）；国内源（akshare/efinance）走 no_proxy 直连。混用会导致取数失败。
- **防未来函数（回测红线）**：所有取数带 `<= as_of` 上界；信号用 T-1 数据、T+1 开盘执行。`build_context`/`ma_alignment` 必须支持 `as_of`，实盘不传 = 取最新。修复前 bollinger 曾虚高 +39.62%，堵漏后真实 -4.14%。
- **单文件存储**：全部数据在 `data/stockfu.db`（SQLite 单文件，搬迁 = 拷贝，勿引入第二个库）；回测产物落 `data/backtest/`（gitignore 运行时产物，勿入库）。
- **四层量化架构边界**：算子→策略→rebalancer→执行，不跳层；active 策略/选股器走 `app_config.active_strategy_id`/`active_rebalancer_id` 路由，不硬编码。算子签名变更必须更新 fingerprint（`operator_result` 缓存按 fingerprint 全局复用）。
- **数据源坑（取数时对照）**：efinance `get_quote_history` 必须传 `beg` 才拉长历史；A股分红"派息"列是每 10 股需 `/10`；港股符号 4 位补零（`0700.HK`）。完整清单见 `docs/PROJECT_STATE.md` 第 7 节。
- **文档与代码不一致先问**：发现工作流文档/PROJECT_STATE 与代码不符，停下问用户，不擅自改文档。

## 多实例并行协作红线

- 每个并行目标一个独立分支（`goal/<id>`）与 worktree（`.claude/worktrees/<id>/`），物理隔离避免冲突。
- 执行者（便宜模型）不得修改 `roadmap.md` 状态、不得自行合并到主线、不得 commit/push 到主线；完工只置 `stage: ready_for_review` + `merge_status: pending_review`。
- 合并与 roadmap 回写由编排者（贵模型）在主线完成，且必须经用户确认。
- `goals/INDEX.md` 是派生投影，由 `/ai-status`/编排者重渲染，不要手工长编辑。
- 提交纪律依赖三层：用户确认 gate（主闸）、执行者红线约定、hook 兜底破坏性 git 操作（见 `.claude/settings.json`）。
