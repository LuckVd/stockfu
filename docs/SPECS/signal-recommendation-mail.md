# 策略信号扫描与推荐邮件

## 目标

系统在 A 股收盘后，以信号日有效的沪深 300（000300）与中证 500（000905）成分并集为股票池，刷新所需行情，运行用户动态选择的策略，并把每只股票、每个策略的当日结果按 0–100 分独立记录。用户可逐股决定是否在邮件中接收因子分，以及是否额外调用 LLM 分析。

本能力不读取实际持仓，不计算目标仓位，不执行交易。它只给出当日评分、因子证据和 LLM 独立意见。

## 评分口径

- 每个策略独立评分，不把多策略压成一个最终信号。
- `50` 为中性；策略原始总分达到该策略 `score_full` 时映射为 `100`，达到 `-score_full` 时映射为 `0`；中间线性映射并截断到 `[0, 100]`。
- 数据库同时保留 `score`、`raw_score`、`confidence` 和因子明细，便于审计与将来调整映射。
- 旧的 `strong_buy / buy / hold / sell / strong_sell` 只保留为兼容字段，不作为主要展示粒度。
- LLM 单独返回 0–100 分、摘要、理由和风险，不覆盖因子策略评分。

## 后续大 TODO：评分刻度与仓位刻度解耦（暂不实施）

当前实现复用了策略 `position.score_full` 作为邮件 0–100 映射刻度。该字段原本表示“原始总分达到多少时目标仓位达到 `max_w`”，不是策略原始总分的理论上限；多因子加权后原始分范围通常明显大于该值，因而可能造成大量 0/100 边界饱和。

后续需要：

1. 为信号展示增加独立、稳定的 `signal_score_scale`，不再复用 `position.score_full`；
2. 统一核对各算子的分数合约（尤其是反转/动量等未封顶算子），明确理论范围；
3. 重新校准各策略的仓位 `score_full`，评估不同因子数量和分数范围下的仓位公平性；
4. 对受影响策略重新进行 full 回测，再比较收益、回撤、换手和策略排名。

在该 TODO 完成前，`raw_score` 是回测与排名的审计依据；邮件中的 0/100 只表示映射后触及边界，不应解读为绝对强买/强卖。

## 开关与默认值

- 全局因子扫描开关：默认开启。开启时对全部指数成分计算并落库。
- 全局 LLM 开关：默认关闭。
- 推荐邮件开关：默认关闭，且仍要求通用 SMTP 配置完整。
- 逐股 `factor_mail_enabled`：默认关闭；只控制是否发送，不能阻止全量因子落库。
- 逐股 `llm_enabled`：默认关闭；只对开启项调用 LLM 并发送结果。
- 支持搜索、单票修改和批量开启/关闭。未创建订阅记录的股票等价于两个开关都关闭。

## 数据模型

- `signal_scan_run`：一次扫描批次、信号日、指数快照、策略清单、处理计数、状态和错误。
- `factor_signal`：批次、股票、策略配置指纹、归一化刻度、0–100 分、原始分、置信度、兼容信号及完整因子审计 JSON；批次内 `(asset_code, strategy_id)` 唯一。
- `llm_signal_analysis`：批次、股票、模型、0–100 分、摘要、理由、风险、输入策略分、状态、耗时和错误；批次内每只股票唯一。
- `stock_signal_subscription`：股票的因子邮件与 LLM 开关。

这些属于不可由缓存替代的业务与审计记录，存放在主库。`operator_cache.db` 继续只保存可再生的单算子缓存。

## 每日流水线

1. 按信号日读取两个指数各自不晚于该日的最近完整快照，取得当日 800 只左右成分。
2. `stockfu.scheduler.jobs.fetch_universe_quotes()` 只抓该日当前有效的沪深 300 + 中证 500 成分：按 `security_master` 排除未上市、退市和非活动状态，并跳过已有完整当日行情，支持中断后续跑。增量刷新这些股票的当日行情，并复用库内已有历史；窗口不足时由算子明确记录数据不足，不以自选股 `asset` 表作为指数扫描范围或静默补成有效分。
   动态策略依赖沪深300基准时同步刷新 `sh000300`；包含红利算子时在每日 90 秒预算内轮转刷新 50 只股票的近两年公司行为，避免每天对 800 只重复发起慢请求。
3. 对全部成员运行启用策略，写算子缓存并将策略级评分写入 `factor_signal`。
4. 读取逐股订阅；只有全局 LLM 开启且该股 `llm_enabled=true` 时，调用一次 OpenAI-compatible Chat Completions，并写 `llm_signal_analysis`。
5. 生成推荐专用 HTML 卡片：每只订阅股票逐策略展示分数和主要因子，LLM 独立成块。
6. 复用现有 Playwright 元素截图、内嵌 Web 与 SMTP MIME 发送框架。每日发送当日分数，不要求分数或标签发生切换。

任一股票或 LLM 调用失败只记录失败，不阻断其他股票；股票池、行情或全部策略失效时阻止发送，避免把不完整结果包装成日报。
停牌股允许沿用最近行情；但超过 10 天未更新的股票多于全池 2%（且至少 8 只）时，判定为数据源整体异常并拒绝本次评分。

## LLM 接口

沿用现有 OpenAI-compatible `POST {base_url}/v1/chat/completions` 客户端。Base URL、API Key、模型名均为运行时配置；不硬编码供应商。DeepSeek V4 Flash 只需填写其兼容地址与实际模型标识。提示词要求结构化 JSON，LLM 分数和文本与因子结果分表存储。

## V2 十策略评分邮件（2026-08 新增）

V1 上述管线基于 `ai.operators` + `services.evaluator` + `score_full` 线性映射。V2 评分架构（`stockfu/scoring` + `stockfu/strategy`）上线后，另起一条**并行管线**把 V2 十策略当日评分送进同类邮件。两条管线独立，不共享策略目录、不共享数据表。

### 与 V1 的关键差异：评分刻度

- **V2 策略分天生是 0–100**：原始值经 profile 映射成 0–100 因子分（`scoring.mappings.combine_hybrid`），再按 alpha 权重加权聚合成策略分（`strategy.alpha.AlphaAggregator`），契约注明「直接用于展示/选股，禁止再映射」。因此**不复用、也不需要 V1 的 `score_full` 线性映射**——V1 上文「后续大 TODO：评分刻度与仓位刻度解耦」在 V2 不存在。
- **中性点 50 一致**：因子缺失向 50 收缩；ECDF 分位 50 即中位；绝对锚点按 50 设计。
- **粒度/分布差异显式暴露（粒度方案①）**：10 个 profile 的映射基不同——`fifty_two_week_high / rsi / trend_linearity` 是纯绝对锚点（`absolute_weight=1.0`），其余（价值/红利/动量/低波/低β/反转）是「绝对锚点 + 历史 ECDF 分位」混合。各策略分的横截面分布因此不同（实测低波 p50≈23、动量 p50≈57）。**V2 不做跨策略再映射**，而是把每策略的校准统计（P05/中位/P95/饱和率/可交易占比）放进邮件图例，让读者按列读、知悉分布差异，而非误读横向绝对值。

### 单日评分入口（设计 A2）

V2 回测引擎（`backtest.v2_engine`）只有 `run_v2_backtest` 循环入口，没有单日评分 API。邮件只需当日分，故新增 `stockfu/services/v2_signal.py::V2SignalScorer.score(as_of)`：

- **复用引擎原语**：`HistoryState` / `FactorScorer` / `AlphaAggregator` / raw_computers / `_preload_market_range` / `_backtest_series_ctx`，**不跑交易/账户/风控/组合**。
- **预热**：评分读 `cutoff < as_of` 的历史，故先回放 `history_origin`→as_of 把 HistoryState 喂满。非采样日只推进 `history.cutoff`，全量 raw 计算只发生在月末采样日 + 目标日——5 年预热对全宇宙约 192s。
- **性能要点**：必须 `with _backtest_series_ctx(sctx, div_index)` 挂内存供给器，否则 `earnings_yield`/`book_to_price` 经 `valuation.pe_pb_at` 每次开 session 查库（~108ms/次）。
- **数据末日截断**：as_of 超过库行情末日（交易日历会预埋未来日）时截断到 `max(sctx.dates)`，与回测引擎一致；展示/主题一律用 `report.as_of`（真实评分日）。

### 渲染与发信

- `stockfu/services/signal_mail_v2.py`：`build_v2_signal_mail_html` 组装长表（图例=粒度①校准 + 策略列 + 榜单股票），`render_v2_signal_images` 用 Playwright 进程内 `set_content` 截图（无 web 路由依赖，自包含），`run_v2_signal_mail_job` 串联评分→出图→复用 `services.mail.send_card_email`。
- **推荐榜单规则（邮件与自选股荐股共用，2026-08-15 起；自选混排 2026-09-02 起）**：榜单 = 综合均分前 N（默认 30）∪ 每策略各自前 5 ∪ **全部自选股**，去重后按综合均分降序；入选理由（"综合前30"/"价值前5"/"自选"等中文标签）记录在每行 `inclusion`，邮件中展示在股票名下（自选标签用徽标底色突出）。自选股评分由独立一轮自选池评分（`cn_watchlist_stock_v1`，与 `--v2-watchlist-recommend` 同语义）供给：已在榜单的自选股保留宇宙池评分、只补「自选」标签（同一代码榜单内单一评分口径）；未上榜的整行插入。自选链失败只降级回纯宇宙榜单，不阻断每日邮件。榜单构建见 `v2_recommend._build_recommend_list` 与 `v2_recommend.merge_watchlist_into_list`。
- **策略名统一中文**：正式五套在报告/邮件/控制台均用中文（价值/高股息/多因子/质量增强/盈利动量进攻），映射见 `v2_signal.ALPHA_CN_NAMES` 与 `signal_mail_v2.V2_ALPHA_BRIEFS`。
- CLI：`main.py --v2-signal-mail`（`--no-send` 仅出图、`--top-n`、`--as-of`）。
- V2 自选股荐股入口：`python main.py --v2-watchlist-recommend --as-of YYYY-MM-DD`。它只取 `Asset.is_watch=1` 且 `asset_type=stock`，先校验目标日行情覆盖，再用调优后五套策略评分（价值/高股息/多因子/质量增强/盈利动量进攻，`RECOMMENDATION_ALPHA_IDS`）；ETF/基金不混入股票池，完整 JSON 落在 `data/reports/recommend/`（含 `rows` 全量 + `recommend_list` 榜单）。

### 持久化（待办）

V1 落 `signal_scan_run`/`factor_signal` 供 web/API 回看；**V2 暂不持久化**，每次内存一次性评分。若需 web 回看或定时调度（接入 `--schedule`），后续可加 V2 版扫描批次表与 `_run_v2_signals` 调度钩子。
