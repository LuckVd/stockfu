# 前端拆分迁移任务清单(Vue 3 + Vite + Naive UI)

> 本文档是 `web/index.html`(1700+ 行单文件)向 Vue 3 工程化前端迁移的**完整任务清单**，按 5 期推进，每期可独立验收、独立上线预览。进度用 checkbox 跟踪。
>
> 维护规则：每完成一项把 `[ ]` 改为 `[x]`；计划外的任务追加到对应期「补充任务」段。

---

## 0. 目标与原则

**为什么**：`index.html` 已 1700+ 行，HTML/CSS/JS 全揉一个文件，无组件复用、无类型、无构建；AI 弹窗等新功能还要加迷你图/指标解析/四段布局，单文件不可持续。

**目标**：迁到 **Vue 3 + Vite + TypeScript + Naive UI + Pinia** 工程化前端；后端 FastAPI 不变，继续托管 build 产物；**保持本地优先、单端口部署**（生产 `:8787` 同源）。

**技术栈**：
- 框架 Vue 3（`<script setup>` + Composition API）
- 构建 Vite 5
- 语言 TypeScript
- UI 库 Naive UI（国人维护、主题定制强、适合 A 股配色）
- 状态 Pinia
- 图表/导出：自绘 SVG（仪表盘/迷你折线）+ html2canvas（分享多图，沿用）
- 包管理 pnpm（已装 11.7）

**铁律（贯穿所有期）**：
1. **数据诚实**：不编造。规格要的数据后端没有就如实标「样本不足/暂无」，或后端补接口，绝不前端造假（项目宪法）。
2. **A 股红涨绿跌**：看多/BUY=红 `#E5484D`，看空/SELL=绿 `#16A34A`，持有/HOLD=棕 `#8B7355`。与持仓页/首页一致。
3. **字号 ≥12px**，明亮扁平无衬线，标题禁用「慢慢变富」（沿用旧设计偏好）。
4. **本地优先**：不引入需要外网运行时依赖（除现有 CDN 字体/html2canvas，迁移后改 npm 安装）。
5. **渐进不中断**：Phase 1–4 旧 `index.html` 仍是默认（全功能可用），新版在 `:5173` 独立开发；Phase 5 才切默认。

---

## 1. 架构与部署

### 目录结构
```
/opt/projects/stockfu/
  main.py                    # 后端入口(不变)
  stockfu/                   # 后端包(不变)
  web/index.html             # 旧前端(Phase 5 下线)
  docs/FRONTEND_MIGRATION.md # 本文档
  frontend/                  # ★ 新前端工程
    package.json  vite.config.ts  tsconfig.json  index.html
    src/
      main.ts                # 挂 Pinia + NConfigProvider
      App.vue                # 根布局
      api/
        client.ts            # fetch 封装 + baseURL + JSON 容错
        modules.ts           # 各端点(indices/portfolio/watchlist/sectors/config/ai/trade/csv/share)
      stores/                # pinia: theme / portfolio / watchlist / market / sentiment / ui
      composables/           # useToast / usePoll / useGauge(仪表盘)/ useIndex
      components/
        layout/  (AppHeader, ThemePicker, HelpModal)
        dashboard/ (MarketMood, Summary, HoldingsTable, WatchlistTable, FundFlow)
        trade/   (TradePanel)
        settings/ (SettingsModal + 4 个 tab 子组件)
        ai/      (AiReportModal, AiButton)
        share/   (ShareCard)
        csv/     (CsvModal)
      styles/ (tokens.ts, base.css)
    dist/                    # pnpm build 产物(被 FastAPI 托管)
```

### 数据流
- **开发**：`pnpm dev`(:5173)→ 前端 `fetch(baseURL + 路径)`，`baseURL=DEV?'http://127.0.0.1:8787':''`，跨域靠现有 `CORSMiddleware(allow_origins=["*"])`。
- **生产**：`pnpm build` → `frontend/dist` → FastAPI(:8787)托管静态文件，**同源免跨域**，`baseURL=''`。

### 后端改动总览（全期）
- Phase 1：`api/server.py` 加 `dist` 托管分支 + catch-all（默认仍 `index.html`）。
- Phase 4：`api/routes.py` 加 `GET /quote/kline/{code}?days=30`。
- Phase 5：`api/server.py` 默认切 `dist`，删 `web/index.html`。
- **全程不动**：`routes.py` 现有 60+ 路由、不加 `/api` 前缀、业务逻辑。

---

## 2. 总进度表

| 期 | 主题 | 关键交付 | 状态 |
|---|---|---|---|
| 1 | 工程地基 | Vite+Vue3+TS+NaiveUI+Pinia 跑通；API 封装；7 套主题；Header+大盘指数卡；FastAPI 托管 dist | ✅ 完成(链路/build/托管/CORS 全通,待浏览器确认渲染) |
| 2 | 看板迁移 | 组合汇总/持仓表/自选表/资金流向 全功能 | 🟡 核心完成(pnpm build 通过;待浏览器逐项验收;T2.2/2.3/2.4/2.10/2.11 推迟) |
| 3 | 交互迁移 | 交易录入/设置弹窗(4 tab)/CSV 导入导出 | 🟡 核心完成(pnpm build 通过;待浏览器验收;T3.8 用 useMessage) |
| 4 | AI 报告弹窗 | 机构级四段重设计 + `/quote/kline` + 30 日迷你图 | 🟡 核心完成(全新设计;pnpm build 通过;待浏览器验收) |
| 5 | 分享 + 切换 | 分享卡片多图 + FastAPI 默认切 dist + 下线旧版 | ⬜ |

---

## Phase 1 — 工程地基（✅ 已完成）

> 验收：`pnpm dev`(:5173) 与 `pnpm build` 后 `:8787` 都能显示上证指数卡 + 恐贪热仪表盘 + 7 套主题切换；`dist` 不存在时 `:8787` 仍回落旧版。

### 1.1 脚手架与依赖
- [ ] **T1.1** `cd /opt/projects/stockfu && pnpm create vite frontend --template vue-ts`（非交互：已给项目名+模板）
- [ ] **T1.2** `cd frontend && pnpm add naive-ui pinia`（可选 `sass` 若用 scss；本期用 ts token 即可）
- [ ] **T1.3** 校验 `pnpm dev` 能起、`pnpm build` 出 `dist`
- [ ] **T1.4** `.gitignore`：忽略 `frontend/node_modules`、`frontend/dist`（项目根 .gitignore 补两行）
- [ ] **T1.5** `tsconfig.json` 加路径别名 `@/* → src/*`；`vite.config.ts` 同步 resolve.alias

### 1.2 基础设施
- [ ] **T1.6** `src/api/client.ts`：`baseURL = import.meta.env.DEV ? 'http://127.0.0.1:8787' : ''`；`doGet/doPost/doPut/doDel` 封装；**复用旧 `api()` 的 text→JSON 容错 + `{error}` 归一**（旧 `index.html` 737–743 行逻辑）
- [ ] **T1.7** `src/api/modules.ts`：按端点分组导出（`indices/portfolio/watchlist/sectors/config/ai/trade/csv/share`），本期只实现 `indices.quotes()`，其余期补
- [ ] **T1.8** `src/stores/theme.ts`：7 套（blue/amber/gold/darkgold/morandi/purple/coral）→ NaiveUI `GlobalThemeOverrides`；读 `localStorage['sf-theme']` 初始化（向后兼容旧版存的值）；`setTheme(t)` 写回；暴露 `isDark`
- [ ] **T1.9** `src/styles/tokens.ts`：A 股色常量（`UP=#E5484D / DN=#16A34A / NEU=#8B7355`）+ 4 顾问身份色（趋势蓝 `#2563EB`/逆向紫 `#8B5CF6`/风险橙 `#F97316`/估值绿 `#10B981`）

### 1.3 视觉基础（移植旧逻辑）
- [ ] **T1.10** `src/composables/useGauge.ts`：移植旧 `gauge()/band()/moodFace()/indexBar()/heatArrows()`（旧 `index.html` 751–809 行）为纯 TS 函数，返回 SVG 字符串或 props
- [ ] **T1.11** `src/components/layout/AppHeader.vue`：品牌 Stock**Fu** + 副标 + 时钟（`tick`）+ 占位按钮（主题/设置/刷新等，本期只接主题）
- [ ] **T1.12** `src/components/layout/ThemePicker.vue`：7 套主题网格弹窗（NModal + 色板），点选调 `themeStore.setTheme`，与旧版 UI 对齐
- [ ] **T1.13** `src/components/dashboard/MarketMood.vue`：拉 `indices.quotes()` → 上证主卡（点数+涨跌+恐贪热三仪表盘）+ 创业板/科创50 副卡（移植旧 `loadMarket`，812–839 行）
- [ ] **T1.14** `src/App.vue`：`<NConfigProvider :theme :theme-overrides>` 包根 + `AppHeader` + `MarketMood` + 占位主区
- [ ] **T1.15** `src/main.ts`：`createApp(App).use(createPinia()).mount('#app')`；NaiveUI 按需引入（或全局 `app.use(naive)`，本期简单用全局）

### 1.4 后端托管（兼容期）
- [ ] **T1.16** `stockfu/api/server.py`：
  - 新增 `_FRONTEND_DIST = BASE_DIR/'frontend'/'dist'`
  - `_index()`：dist 存在 → 返回 `dist/index.html`；否则回落 `web/index.html`（**默认逻辑不动**）
  - dist 存在时 `app.mount('/assets', StaticFiles(directory=dist/'assets'))`
  - catch-all `@app.get("/{full:path}")` SPA fallback（仅 dist 存在时返回 index.html；注意排在所有 API 路由之后）
- [ ] **T1.17** 兼容验证：删/重命名 dist → `:8787` 仍回旧版全功能

### 1.5 Phase 1 验收
- [ ] **T1.18** `pnpm dev` → `:5173` 显示上证指数卡（数据来自 `:8787`）
- [ ] **T1.19** 主题切换 7 套生效 + 刷新保持（localStorage）
- [ ] **T1.20** `pnpm build` → 重启 serve → `:8787` 看到 Vue 版指数卡
- [ ] **T1.21** dist 移除后 `:8787` 回落旧版

---

## Phase 2 — 看板迁移（🟡 核心完成，待浏览器逐项验收）

> 验收：看板（大盘/组合/持仓/自选/资金流向）在 Vue 版全功能可用，与旧版逐项对齐。

### 2.1 状态层
- [ ] **T2.1** `stores/portfolio.ts`：持仓列表 + 排序状态（`holdSort`）+ 汇总；`load()` 拉 `/portfolio`
- [ ] **T2.2** `stores/watchlist.ts`：自选 + 分页 + 排序（`watchSort/watchPage`，`WATCH_PS=15`）
- [ ] **T2.3** `stores/sentiment.ts`：资金流向板块数据
- [ ] **T2.4** `stores/market.ts`：指数行情（Phase 1 已用，提炼成 store）

### 2.2 组件
- [ ] **T2.5** `dashboard/Summary.vue`：组合汇总 5 格（市值/成本/盈亏/整体股息率/年红利 + 截至）（旧 842–857）
- [ ] **T2.6** `dashboard/HoldingsTable.vue`：15 列排序（`sortBy` 通用，null 排末尾）+ 行展开（`/indices/stock/{code}` 个股指数详情）+ 删除（`DELETE /holding/{code}`）+ AI 按钮占位（Phase 4 接）
- [ ] **T2.7** `dashboard/WatchlistTable.vue`：分页 + 排序 + 取消追踪（`DELETE /watch/{code}`）+ 持仓 tag
- [ ] **T2.8** `dashboard/FundFlow.vue`：全市场净额卡 + 板块横向柱状（top20 流入 + bot20 流出）（旧 1037–1057）
- [ ] **T2.9** `dashboard/MainTabs.vue`：持仓/自选/资金流向 切换 + 刷新按钮（旋转动画）

### 2.3 行为
- [ ] **T2.10** `composables/usePoll.ts`：移植 `pollStockReady`（追踪后每 8s 轮询，fear 落库即刷新）
- [ ] **T2.11** `ensureStock`：`POST /stock/{code}/ensure` 触发后台补 K 线 + 自动刷新
- [ ] **T2.12** 全量加载 `loadAll`（Promise.all 并行）+ 顶部刷新按钮
- [ ] **T2.13** 与旧版逐项对比（数值/排序/展开/分页），差异归零

---

## Phase 3 — 交互迁移（🟡 核心完成，待浏览器验收）

> 验收：交易/设置/CSV 在 Vue 版可用。

- [x] **T3.1** `dashboard/TradePanel.vue`：买/卖/追踪 seg + 代码/股数/价格/日期表单 + 提交（`POST /trade`、`POST /watch/{code}`）+ 最近成交 + ensureStock/poll
- [x] **T3.2** `settings/SettingsModal.vue`：NModal + 4 tab（v-if 懒加载）+ 各 tab 就地「保存」+「测试」（偏离旧版底部全局保存）
- [x] **T3.3** `settings/ProxyTab.vue`：`/config/proxy` 读 + `PUT` + `POST /config/proxy/test`（三态）
- [x] **T3.4** `settings/ScheduleTab.vue`：抓取时间/重试间隔/重试次数 → `PUT /config/schedule`
- [x] **T3.5** `settings/MailTab.vue`：邮箱预设下拉（QQ/163/Gmail/通用）+ 字段 + `POST /config/mail/test`（多态）
- [x] **T3.6** `settings/LlmTab.vue`：base_url/api_key/model + `POST /config/llm/test`（api_key 留空=不改；test 先存再测）
- [x] **T3.7** `csv/CsvModal.vue`：导入/导出（持仓/自选）+ 模板下载（`/csv/template/{scope}`）+ 类型分段 + 进度结果 + 导入后刷 store
- [x] **T3.8** 用 NaiveUI `useMessage`/`useDialog`（App 已包 provider，不单建 useToast）
- [x] **T3.9** `help/HelpModal.vue`：情绪指数说明（旧 676–688）

---

## Phase 4 — AI 报告弹窗（🟡 核心完成，全新设计，待浏览器验收）

> 验收：点持仓行「分析」→ 机构级四段报告弹窗（Header/Summary 四栏/4 分析师卡含 30 日迷你图/Bottom 综合结论+操作建议）。设计语言：Bloomberg+Apple+Linear，大量留白、圆角、浅阴影、A 股红涨绿跌。详见既定方案（自适应 1500×90vh）。

### 4.1 后端
- [x] **T4.1** `api/routes.py` 加 `GET /quote/kline/{code}?days=30`：**路由内直接查 `QuoteSnapshot` 拼 `points:[{date,close}]`**（不用 `quote_series`——它只返 float 无 date，且被 6 工具依赖）；同时 `_set_ai_done`/`get_ai_result` 增存/返 `signal`，AiButton done 态零请求上色。

### 4.2 前端
- [x] **T4.2** `ai/AiButton.vue`：四态(分析/loading/done/err) + done 信号竖条「查看」+ pending 轮询 + 固定宽 64px 不抖 + POST 超时 race
- [x] **T4.3** `ai/AiReportModal.vue`：NModal **不用 preset** + 固定字面量浅色(深色主题下仍白)，`width:min(96vw,1540px)`，上下四段
- [x] **T4.4** Header：股票名 32px Bold + 代码 mono + 「AI 分析报告」24px + X + 「↻ 重新分析」
- [x] **T4.5** Summary 四栏：最终意见(52px) / 综合评分(total_score + 偏多偏空中性胶囊) / 风险栏(risk_vetoed 红橙⚠一票否决 else 摘要) / 分析信息
- [x] **T4.6** 4 分析师卡：圆形身份图标(趋势折线/逆向节点/风险盾牌/估值饼图,身份色) + 顾问名 + 方向 + Opinion Badge + 评分胶囊
- [x] **T4.7** 核心观点：浅灰小卡 #FAFAFA + reasoning(14px/1.7)
- [x] **T4.8** 关键指标：`useAiMetric` toolToMetric(tool 名定指标 + result 关键词归类,失败回退);估值顾问 tools 空走 context.pe/pb/股息率
- [x] **T4.9** 30 日迷你图：`useMiniSpark` 渐变面积(返回 areaPath/linePath/gradId 模板渲染 svg,避开 v-html);modal 取一次 `/quote/kline` 缓存,4 卡共用,各身份色
- [x] **T4.10** Bottom 左：综合结论(narrative 按句号分两段,15px/1.8,绿竖线)
- [x] **T4.11** Bottom 右：操作建议(final_signal 派生仓位/周期) + 大字 signal + 核心风险 bullet
- [x] **T4.12** AiReportModal Header「↻ 重新分析」按钮(POST /ai 重跑 + 刷新)

### 4.3 信号→仓位/周期派生表（确定性规则，非编造）
| signal | 仓位 | 周期 |
|---|---|---|
| strong_buy | 80–100% | 中期加仓 |
| buy | 50–80% | 中期持有 |
| hold | 30–50% | 短中期观望 |
| sell | 10–30% | 短期减仓 |
| strong_sell | 0–10% | 短期规避/清仓 |

---

## Phase 5 — 分享 + 切换

> 验收：分享卡片单图/多图 9:16 导出可用；FastAPI 默认托管 dist；旧 `index.html` 下线。

- [ ] **T5.1** `pnpm add html2canvas`（替换 CDN 引用）
- [ ] **T5.2** `share/ShareCard.vue`：单图（与现渲染一致）+ 多图 9:16（probe 测行高→分页，旧 1392–1440）
- [ ] **T5.3** 导出：`html2canvas` 单图下载 + 多图逐页下载 + 全部下载
- [ ] **T5.4** 邮件定时回归：`/share` 端点 + `services/mail.py` 多图依赖仍正常（无头渲染走 `:8787`）
- [ ] **T5.5** `server.py`：默认托管切 `frontend/dist`（dist 不存在报错而非回落）；移除 `web/index.html` 托管分支
- [ ] **T5.6** 删 `web/index.html`（git 保留历史）
- [ ] **T5.7** 更新 `README.md`：前端开发说明（`cd frontend && pnpm dev`；`pnpm build` 后由 `--serve` 托管）
- [ ] **T5.8** 全功能回归测试（持仓/交易/设置/CSV/分享/AI/邮件/主题），与旧版逐项对齐

---

## 3. 风险与回退

| 风险 | 应对 |
|---|---|
| 旧版用户正在用，迁移期中断 | 渐进策略：Phase 1–4 旧版默认可用，新版 `:5173`/dist 预览；Phase 5 才切 |
| NaiveUI 主题与旧 CSS 变量色值漂移 | `styles/tokens.ts` 单一色源；旧版色值作为映射基准；每期对齐对比 |
| `/share` 多图被邮件无头渲染依赖 | Phase 5 必须回归 `--test-mail`；渲染走 `:8787` 同源 dist |
| 工具结果解析脆弱 | `toolToMetric` 失败回退原文摘要，绝不显示空白/造假 |
| dist 托管与 API 路由冲突 | catch-all 排在 `include_router` 之后，且仅 dist 存在时生效 |

**回退**：任一期出问题，停止切换默认托管（保持 `server.py` 回落 `web/index.html`），新版在 `:5173` 继续调试，不影响生产可用版本。

---

## 4. 补充任务 / 备注

（执行中发现的计划外任务追加于此，注明归属期）

### Phase 2 偏离记录（2026-07-04）

**已做**：T2.1(portfolio store) / T2.5(Summary) / T2.6(HoldingsTable,AI占位) / T2.7(WatchlistTable,只读) / T2.8(FundFlow) / T2.9(MainTabs) / T2.12(loadAll，拆为 App 初始 fetch + MainTabs 懒加载/刷新)。`pnpm build` 通过（vue-tsc + dist），数据链路 curl 通过。

**推迟 / 偏离**：
- **T2.2 watchlist store / T2.3 sentiment store** → 改为组件局部 ref（WatchlistTable/FundFlow 内）。原因：数据+排序+分页完全在单组件内，无跨组件复用；watchlist 留待 Phase 3 交易面板「追踪」时按需提炼成 store。
- **T2.4 market store** → 不做。MarketMood 保持局部 ref，避免重构引发 Phase 1 回归。
- **T2.10 usePoll / T2.11 ensureStock** → 推迟 Phase 3（Phase 2 无触发入口，「追踪」按钮在 Phase 3 交易面板）。
- **T2.6 AI 按钮** → `disabled` 占位（title="AI 分析 (Phase 4)"），逻辑 Phase 4 接。
- **T2.7 添加自选** → 随 Phase 3 交易面板「追踪」seg 做；本期 WatchlistTable 只读（查看 + 取消追踪）。

**新增文件**：`api/types.ts`（TS 类型集中）、`composables/useFormat.ts`（nf/pct/signed/curSym）、`composables/useSort.ts`（sortBy/nextSort/STRING_COLS）。
**修改**：`useGauge.ts` 补 `indexBar`/`heatArrows`；`client.ts` 的 doPost/doPut/doDel 加泛型 `<T>`；`modules.ts` 补 portfolio/watchlist/sectors/holding/watch 五组；`App.vue` 嵌套 NMessageProvider/NDialogProvider + 挂 Summary/MainTabs；全局 `style.css` 追加看板类（持仓表用 `.ht-tbl` 前缀避免全局 table 误伤、hover/expanded 改 `var(--surface-2/3)` 适配深色主题、资金流柱用 `.bar-in/.bar-out` 避开仪表盘 `.bar` 冲突）。
**后端零改动**：复用现有 `/portfolio /watchlist /sectors/flow /indices/stock/{code} /holding/{code} /watch/{code}`。

### Phase 3 偏离记录（2026-07-04）

**已做**：T3.1-T3.9 全部（T3.8 用现成 useMessage/useDialog）。`pnpm build` 通过；config 端点 curl 验证字段吻合（/config/proxy /config/llm /config/schedule）。待浏览器逐项验收。

**补做 Phase 2 推迟项**：T2.2 `stores/watchlist.ts`（WatchlistTable 改读 store，让 TradePanel 追踪 / CsvModal 导入自选后能刷新）；T2.10 `composables/usePoll.ts` + T2.11 ensureStock（追踪/买卖后后台补数据 + 每 8s 轮询 fear 落库即刷新 portfolio+watchlist）。

**偏离 / 决策**：
- **设置 tab 保存**：每个 tab 就地「保存」+「测试」（偏离旧版底部全局保存），状态内聚；tab 用 `v-if` 懒加载（首次切到才 GET，避免开弹窗就发 4 请求）。
- **LlmTab.test**：先 `await save()` 再 test（否则测旧 base_url）。
- **TradePanel**：常驻 MainTabs 右侧双列 grid（`1fr 360px`，对齐旧版），移动端 `@media(max-width:900px)` 叠列；非弹窗。
- **CSV 导出**：绕开 doGet（doGet 会 JSON 解析），用 `fetch(BASE_URL+url)`→Blob+`a.click()`；模板用 `<a download>` 避免 `window.location` 整页跳转；导入用 FormData（`doPost` json=false，浏览器自动加 multipart boundary）。
- **MailTab**：preset 用前端内置默认（QQ/163/Gmail 标准 host/port），days 简化为 mon-fri/每天（去掉 custom）。
- **NModal**：preset=card，`:body-style` 控制内边距，避免与全局 `.set-group/.lab` 双重 padding。

**新增文件**：`stores/watchlist.ts`、`composables/usePoll.ts`、`components/dashboard/TradePanel.vue`、`components/settings/{SettingsModal,ProxyTab,ScheduleTab,MailTab,LlmTab}.vue`、`components/csv/CsvModal.vue`、`components/help/HelpModal.vue`。
**修改**：`client.ts` export BASE_URL；`modules.ts` 补 trade/stock/configApi(11 方法)/csvApi(3) + watchApi.add；`types.ts` 补 Trade/Watch/Ensure/Proxy/Schedule/Mail/Llm/CsvImport 类型；`MainTabs.vue` 改双列挂 TradePanel；`AppHeader.vue` 加 设置/导入/导出/帮助 4 按钮 + 挂 4 modal（`.btn` 从 scoped 提到全局）；`style.css` 追加 trade/main-grid/settings/csv/help 类 + 全局 `.btn/.btn.ghost`。
**后端零改动**：复用 `/trade /watch /stock/{code}/ensure /config/{proxy,schedule,mail,llm} /csv/{import,export,template}`。

### Phase 4 偏离记录（2026-07-04）

**已做**：T4.1-T4.12 全部（全新设计）。`pnpm build` 通过；后端重启后 `/quote/kline` 返回 30 点、`/ai/result` 含 signal，验证通过。待浏览器逐项验收视觉。

**后端改动（本期唯一）**：`routes.py` 加 `GET /quote/kline/{code}`（路由内查 QuoteSnapshot 拼 points，**不动 quote_series**）；`_set_ai_done`/`get_ai_result` 增 `signal` 字段。

**偏离 / 决策**：
- **全新设计非迁移**：不复用旧 `web/index.html` 的 `.ai-*`，按用户规格（Bloomberg+Apple+Linear+Notion）从零写；弹窗内全字面量浅色（不引用主题 `var`），深色主题下弹窗仍纯白。
- **NModal 不用 preset**：避免 card 主题色/边框污染；自定义 `.ai-shell` 接管。
- **字段名**：`aggregate.opinions[].score`（synthesis.py:50 核实，非 score_adjustment）。
- **toolToMetric**（旧版没有）：tool 名定指标名 + result 关键词归类（多头/空头、金叉/死叉、超买/超卖、放量/缩量…）+ 失败回退；估值顾问 tools 空走 context.pe/pb/股息率。
- **miniSpark**（旧版没有）：返回 `{areaPath,linePath,gradId}` 供模板渲染 svg（避开 v-html）；4 卡共享 closes，渐变 id 含 advisor 唯一。
- **AiButton**：固定宽 64px + signal 左侧 4px 竖条（不抖）；POST `Promise.race` 120s 超时；pending 自动 poll（useAiPoll，3 分钟超时）；loadAiResults 放 portfolio store 80ms 错峰。
- **操作建议**：final_signal → 仓位/周期确定性派生表（非编造）；核心风险从 risk 顾问 reasoning 拆句。
- **tokens 色板**：项目 `COLOR.UP=#dc2626` 与规格 `#E5484D` 不一致 → 弹窗内局部字面量，不套全局。

**新增文件**：`components/ai/{AiButton,AiAnalystCard,AiReportModal}.vue`、`composables/{useAiMetric,useMiniSpark,useAiPoll}.ts`。
**修改**：`api/types.ts`（AI/Kline 类型，score 非 score_adjustment）、`api/modules.ts`（aiApi/quoteApi）、`stores/portfolio.ts`（aiStates + loadAiResults）、`components/dashboard/HoldingsTable.vue`（disabled 占位换 AiButton + 挂 AiReportModal）。
