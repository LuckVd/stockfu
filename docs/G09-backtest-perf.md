# G09 · 回测性能优化（operator meta 缓存 + 冗余索引清理 + WAL）

> 把热缓存天级回测从「分钟级」降下来。方向已确认；完整设计（含末尾 5 个待确认事项）待拍板后实施。

## 目标

三刀：

1. `_load_operator_meta` 进程级缓存——砍掉单次回测 ~420 万次 session 开闭；
2. 删 `operator_result` 4 个冗余单列索引 + VACUUM——缩小 2.9G 文件、缓解 page cache/swap 压力；
3. 开 WAL + `synchronous=NORMAL`——冷启动写缓存快一个量级、热跑读并发更友好。

**范围外（已否决，避免白做）**：`get_operator_result` 内存缓存。原因见「现状」末项。

## 现状（实证，2026-07-14）

- **慢跑性质 = 热缓存纯读**：`operator_result` 已有 **565 万行**，但 `updated_at` 显示 07-14 20:04 只写 **58 行**；565 万行是 **07-12 冷启动**建的。故本次慢是**读路径**瓶颈，非写锁。
- **meta 重复查**：`stockfu/ai/operators/runner.py:186-194` `_load_operator_meta` 每次 `session_scope()`+`s.get(Operator, id)`，**无缓存**。1 个 analyze 调 4 次（每算子 1 次）→ ~420 万次 session 开闭。`operator` 表仅 14 行、永远在页缓存，查询本身廉价，**贵在 session 开闭 ×N**。
- **冗余索引**：`operator_result` 既有唯一复合索引 `uq_op_result_code_date_op_fp(asset_code, as_of, operator_id, fingerprint)` 覆盖热路径；另有 4 个单列 `index(asset_code/as_of/operator_id/fingerprint)`，由 `stockfu/models.py:252-256` 的 `index=True` 自动生成。全库 `operator_result` 查询**仅 3 处**（`operator_cache.py:59`/`93`/`129-141`），**全被复合索引覆盖**（含 `count_operator_results` 走 `asset_code` 前缀）。4 个单列索引纯冗余，却让 565 万行 × 4 棵额外 B-tree **撑大文件 → page cache 放不下 → swap 满(1981/1987)**。
- **陷阱（已核实）**：`models.py:252-256` 这 4 列标了 `index=True`，故 SQLModel `create_all`（`init_db`/`_ensure_tables` 调）**会重建** → 仅 `DROP INDEX` 无效，**必须同时从模型去 `index=True`**。
- **无 WAL**：`stockfu/db.py:14` `create_engine` 未设任何 pragma；实测 `journal_mode=delete`、`synchronous=FULL`(=2)。冷启动写 565 万行每次 commit fsync + 全库锁 → **07-12 那次冷启动的主因**。
- **已否决（关键）**：`get_operator_result` 内存缓存。`stockfu/backtest/engine.py:375` `pool.submit(_analyze, c, as_of, ...)` 对每个 `(code, as_of)` **只提交一次** → 单次回测内**无重复 key** → 内存 LRU **0 命中**。复用是跨次回测走 DB 表，**已生效**（故攒了 565 万行）。`_make_cached_analyze`（`scheduler.py:64-78`）名不副实，**不缓存**，只注入策略+temp。

## 方案

三刀，**全部扩展现有文件，不新建模块**。

| 改动 | 文件 |
|---|---|
| `_load_operator_meta` 包 `functools.lru_cache` | `stockfu/ai/operators/runner.py:186-194` |
| `OperatorResult` 4 列去 `index=True` | `stockfu/models.py:252-256` |
| 幂等迁移 `DROP INDEX IF EXISTS` 4 个单列索引 | `stockfu/db.py:_migrate()` |
| `create_engine` 加 connect 事件监听设 `journal_mode=WAL`+`synchronous=NORMAL` | `stockfu/db.py:14-18` |
| 一次性 `VACUUM`（INTO 新文件原子替换） | 新 CLI `--vacuum`（`main.py`） |
| 文档同步（meta 缓存 + 索引策略 + WAL） | `docs/BACKTEST.md` / `docs/PROJECT_STATE.md` |

**不动**：`get_operator_result` 路径（不加内存缓存）、算子计算逻辑、四层架构边界、fingerprint 契约、其他表索引、`quote_model_for` 现状。

## 验收标准

1. **meta 缓存**：同进程内 `_load_operator_meta(op_id)` 对每个算子只查 1 次 DB，后续命中缓存；回测功能不变（同输入同输出）。
2. **冗余索引清理**：`operator_result` 仅剩 PK + 唯一复合索引，4 个单列索引消失；`create_all`/全新 `--init-db` **不再重建**；3 个查询点 `EXPLAIN QUERY PLAN` 均走 `uq_op_result_code_date_op_fp`（或 PK），不走全表扫描。
3. **WAL**：DB 实测 `journal_mode=wal`、`synchronous=NORMAL`；产生 `data/stockfu.db-wal`/`-shm`；回测/API/scheduler 多进程访问读不互相阻塞。
4. **文件瘦身**：`VACUUM` 后 `data/stockfu.db` 体积显著下降（记录前后 MB）；可用内存/swap 压力缓解。
5. **防未来函数红线不破（硬约束）**：纯基础设施改动不改信号——同一区间同一策略，优化前后 metrics（`equity_curve`/`trades`/最终净值）逐项一致。
6. **实测加速**：同一回测命令（同区间同 codes）优化前后 wall-clock 对比，记录加速比。
7. **文档同步**：BACKTEST.md/PROJECT_STATE 反映 meta 缓存 + 索引策略 + WAL。

## 测试计划

- **单元·meta 缓存**：mock session，断言同 `op_id` 第二次不查 DB（lru_cache 命中）；不同 `op_id` 各查 1 次。
- **单元·索引迁移**：`_migrate()` 幂等（连跑两次不报错）；迁移后 `PRAGMA index_list(operator_result)` 仅 PK + 复合唯一；全新 `--init-db` 库也无 4 单列索引。
- **EXPLAIN·3 查询点**：`get_operator_result`/`save_operator_result`/`count_operator_results` 的 `EXPLAIN QUERY PLAN` 均用复合唯一索引或 PK，无全表扫描。
- **防未来回归·数值不变**：优化前 dump 一份回测 metrics；优化后同命令重跑，逐字段比对 `equity_curve`/`trades`（沿用 G02 prefix 一致性方法）。
- **集成·WAL 并发**：开 WAL 后，scheduler daemon 写入期间回测读不阻塞（不 `SQLITE_LOCKED` 崩溃）。
- **实测·加速比**：固定回测（如 `bollinger_monthly`，同区间同 codes）前后计时 + 文件体积前后对比。

## 实现步骤

1. `runner.py:186`：`_load_operator_meta` 上加 `@functools.lru_cache(maxsize=None)`（key=`operator_id`），`import functools`；函数体不变。
2. `models.py:252-256`：`OperatorResult` 的 `asset_code`/`as_of`/`operator_id`/`fingerprint` 去掉 `index=True`（保留 `UniqueConstraint` 复合）。
3. `db.py:_migrate()`：加幂等 `DROP INDEX IF EXISTS ix_operator_result_asset_code` / `_as_of` / `_operator_id` / `_fingerprint`。
4. `db.py:14-18`：`create_engine` 后挂 `event.listens_for(engine, "connect")`，每连接执行 `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`（WAL 为 DB 级持久，首连设一次即持久）。
5. 一次性 VACUUM：加 CLI `--vacuum`（`main.py`），`VACUUM INTO 'data/stockfu.db.vac'` 后原子替换（停 daemon/回测时跑）；**动手前先备份** `data/stockfu.db.bak.G09`。
6. 防未来回归：跑数值/prefix 一致性测试（步骤 5 前后各一次）。
7. 文档：`docs/BACKTEST.md` 加「性能：meta 缓存 + 索引策略 + WAL」段；`docs/PROJECT_STATE.md` 数据现状表补 `operator_result` 索引/WAL 备注。

## 风险

- VACUUM 期间锁库 → 必须在无 daemon/回测写入时跑（磁盘 24G 空闲，够 2.9G 临时空间）。
- WAL 持久变更产生 `-wal`/`-shm` 旁路文件 → 备份/搬迁需一并带走（文档标注）。

## 待确认事项（默认已定，有异议请指出）

- **WAL 作用域**：默认**全局持久开**（对多进程并发更友好，读不阻塞写）；副作用 = 产生 `-wal`/`-shm` 文件，备份/搬迁需带上。是否接受全局？
- **索引删除安全**：已 grep 确认仅 3 查询点全被复合覆盖，默认删；落库前 `EXPLAIN` 复核 3 查询点。（基本无风险，记录备案）
- **meta 缓存失效**：默认 `lru_cache` 进程级，改 `operator` 表 prompt/version 后**进程内不自动失效**（fingerprint 含 version 跨次回测自动失效；重启清空）。改 prompt 是低频运维动作，默认不加手动清缓存 hook。是否够？
- **VACUUM 方式**：默认 `VACUUM INTO` 新文件原子替换（更安全）；备选直接 `VACUUM`（锁库期间阻断访问）。选哪个？
- **验收含实测加速比**：默认**要求**（前后 wall-clock + EXPLAIN + 文件体积三组实证），保证非空头优化。是否接受多这步测量？

---

## 实施记录（2026-07-15）

G09 已完成。核实工作树 + 活库后发现原方案三刀中**前两刀已在 P0+P1 同期改动里落地**（文档未同步），本次补齐剩余：

| 刀 | 状态 | 实证 |
|---|---|---|
| meta 进程级缓存 | ✅ | `_ensure_op_meta` 实例级缓存（每策略+temp 算一次）+ `_load_operator_meta` `@functools.lru_cache`（进程级，跨实例）。实测同进程每算子只查 1 次 DB（`cache_info` misses=1 hits=1） |
| 删 4 冗余单列索引 | ✅（早已落地） | `models.py` 去 `index=True` + `db._migrate` `DROP INDEX IF EXISTS`；活库 `PRAGMA index_list(operator_result)` 仅剩复合唯一 `sqlite_autoindex_operator_result_1`；EXPLAIN 两条热路径均走复合索引、无全表扫 |
| VACUUM | ⏸ 工具已交付，本次未跑 | 活库 `freelist=0`、1.13GB 已紧凑，VACUUM 0 回收；`--vacuum` CLI（`VACUUM INTO` 原子替换）留作 `cleanup_operator_results` 全表扫 DELETE 后的维护工具 |
| WAL | ✅ 本次新增 | `db.py` connect 监听器：`journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`；实测 wal/1/5000，产生 -wal/-shm |

**回归（防未来函数硬门槛）**：ARCHITECTURE_REVIEW §7 基准（macd_cross / 5 codes / 2025-06-01~08-01）优化前后签名 `c5008ea2b5b7` 逐字节一致，14 项 metrics 0 差异；与 §7 基准表逐值吻合。并发 smoke：写事务未 commit 期间另一连接 SELECT 0.4ms 不阻塞、smoke 回滚干净。

**加速比诚实结论**：meta 大头早被 `_ensure_op_meta` 解决（热缓存单跑 0.503s → 0.482s，噪声内）；WAL 收益在**并发**（scheduler 写 / 回测读不阻塞，已 smoke 证明）+ **冷启动写缓存**（省 fsync），**非热缓存单跑 wall-clock**。文件体积：freelist=0，无瘦身空间（原"2.9G 瘦身"前提已不成立）。

### 5 个待确认事项 · 结论

1. **WAL 作用域**：✅ 全局持久开。备份前 `PRAGMA wal_checkpoint(TRUNCATE)` 并回主库保单文件习惯（已写进 CLAUDE.md / BACKTEST.md §6 / PROJECT_STATE §9）。
2. **索引删除安全**：✅ grep + EXPLAIN 复核 3 查询点全走复合唯一索引。
3. **meta 缓存失效**：✅ `lru_cache` 进程级，改 prompt/version 重启清空；fingerprint 含 version 跨次回测自动失效。不加手动 hook。
4. **VACUUM 方式**：✅ `VACUUM INTO` 原子替换（scratch 库验证机制：45056B → 24576B，1000 行全保）。
5. **实测加速比**：✅ 已出（wall-clock + EXPLAIN + 文件体积三组），如实记录 WAL 对热缓存单跑无加速（收益在并发 + 冷写）。
