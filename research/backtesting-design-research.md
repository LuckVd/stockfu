# 主流量化回测系统设计调研

> 调研日期：2026-07-27
> 调研范围：Zipline、QuantConnect (LEAN)、Backtrader、vnpy、RQAlpha (RiceQuant)、聚宽 (JoinQuant)、Tushare

---

## 一、回测引擎核心架构

主流量化回测引擎的架构高度一致，分为 4 层：

```
数据层 → 事件循环/调度层 → 撮合/执行层 → 绩效分析层
```

### 1.1 各系统架构对比

| 系统 | 语言 | 架构特点 |
|------|------|----------|
| **Zipline** (Quantopian) | Python | 事件驱动，日频/分钟频，SQLite 存储调整因子，Cython 加速调整矩阵计算 |
| **QuantConnect (LEAN)** | C# | 事件驱动，多资产类，FactorFile 体系，DataNormalizationMode 控制数据调整 |
| **Backtrader** | Python | 策略-数据-经纪商三层分离，Broker 层负责现金/持仓处理，订单撮合在 Broker 内 |
| **vnpy** | Python | 模块化设计，C++ 核心撮合 + Python 策略层，支持 CTP 柜台对接 |
| **RQAlpha** (RiceQuant) | Python | Mod 插件体系，sys_simulation 负责撮合+事件源，设计受 Zipline 启发 |
| **聚宽 (JoinQuant)** | 闭源云端 | 云端执行，内置复权因子计算，点价成交撮合 |

### 1.2 事件循环核心流程

以 Zipline/LEAN 为代表的事件驱动引擎，每个 tick/minute/day 的循环：

```
for each trading day/minute:
  1. 获取当前时刻的行情快照 (OHLCV)
  2. 应用已保存的价格调整因子（split/dividend 调整）
  3. 检查并处理公司事件（分红、拆股、退市、改名）
  4. 推送调整后的数据给策略
  5. 策略产生信号 → 生成订单
  6. Broker 撮合订单（市价/限价）
  7. 更新持仓/现金
  8. 记录每日绩效
```

### 1.3 关键设计选择

| 维度 | Zipline | LEAN | Backtrader | RQAlpha |
|------|---------|------|------------|---------|
| 调整时机 | 数据加载时批量应用调整矩阵 | 数据加载时按 factor file 缩放 | 不调整价格，事件发生时补偿 | 加载时应用复权因子 |
| 撮合方式 | 盘中实时撮合 | 盘中实时撮合 | 收盘价/下一笔开盘价 | 点价撮合 |
| 多资产支持 | 股票+期货+期权 | 全资产类 | 股票+期货+期权 | 股票+期货 |
| 订单类型 | 市价/限价/止损 | 全类型 | 市价/限价/止损/目标 | 市价/限价 |

---

## 二、分红/拆股处理

### 2.1 三种处理路线

通过调研各主流系统源码和文档，有三种不同的技术路线：

#### 路线 A：价格调整法（Price Adjustment）

**代表系统**：Zipline、LEAN (Adjusted)、聚宽、RQAlpha

**原理**：用因子修正历史 K 线的价格（和成交量），使价格序列连续无跳变。调整在数据层完成，策略层看到的是调整后的"假价格"。

**Zipline 实现**（源码来源于 `zipline/data/adjustments.py` + `_adjustments.pyx`）：

Zipline 将 splits、dividends、mergers 存储在 SQLite 数据库的独立表中，作为 `Float64Multiply` 调整对象。

```python
# 数据结构
SQLITE_ADJUSTMENT_TABLENAMES = frozenset(['splits', 'dividends', 'mergers'])

# 拆分表结构
SQLITE_ADJUSTMENT_COLUMN_DTYPES = {
    'effective_date': int,
    'ratio': float64,    # split: new_shares / old_shares
    'sid': int,          # asset id
}
```

- **拆股**: 价格 × ratio（ratio > 1 表示拆分，价格变低），成交量 × 1/ratio
- **分红**: 价格 × (1 - cash_dividend / prev_close)
- **合并**: 价格 × ratio（收购换股）

调整的加载通过 Cython 加速的 `load_adjustments_from_sqlite()`，将调整映射到 `(bar_index, asset_index) → multiplier` 矩阵，在数据进入策略前批量应用。

#### 路线 B：事件补偿法（Event Compensation）

**代表系统**：Backtrader

**原理**：保持原始价格不变，公司事件发生时直接在 Broker 层修改现金和持仓。策略看到有缺口的价格线，但损益计算正确。

Backtrader 在 `broker.py` 和 `order.py` 中处理：
- Broker 层有 `cash_value` 跟踪
- 拆股时调用 `broker.split()`，调整持仓数量和均价
- 分红通过自定义数据源或 `cash_dividend` 方法处理现金入账

#### 路线 C：复权因子法（Adj Factor）

**代表系统**：聚宽、RiceQuant、Tushare

**原理**：由数据供应商统一计算每日的复权因子（adj_factor），用户直接使用。

```python
# Tushare 接口示例
pro = ts.pro_api()
df = pro.adj_factor(ts_code='000001.SZ')
# 返回: trade_date | adj_factor
```

#### 2.2 三种路线对比

| 维度 | 价格调整法 | 事件补偿法 | 复权因子法 |
|------|-----------|-----------|-----------|
| K线连续性 | ✅ 连续 | ❌ 有跳空缺口 | ✅ 连续 |
| 技术指标 | ✅ 可直接计算 | ❌ 需特殊处理跳空 | ✅ 可直接计算 |
| 数据真实性 | ❌ 非真实价格 | ✅ 全部真实 | ❌ 非真实价格 |
| 未来函数 | ❌ 有（前复权） | ✅ 无 | ❌ 有 |
| 成交量处理 | ❌ 需同时调整 | ✅ 真实 | ❌ 需调整 |
| 实现复杂度 | 中 | 高（事件处理逻辑复杂） | 低（数据源提供） |

---

## 三、退市处理

### 3.1 幸存者偏差（Survivorship Bias）

这是回测中最容易被忽略但影响最大的偏差之一。

**定义**：如果只用当前仍在交易的股票做历史回测，相当于排除了因为业绩差而退市/摘牌的股票，回测收益会虚高。

### 3.2 主流做法

#### QuantConnect (LEAN) —— 行业标杆的做法

源码文档显示 LEAN 的退市处理有三阶段：

```
1. DelistingType.WARNING（退市警告）
   - 退市前最后一交易日发送
   - 策略可在此日主动平仓（建议 MarketOnOpen 市价单）

2. DelistingType.DELISTED（退市执行）
   - 引擎自动以退市价（上日收盘价）强制平仓剩余持仓
   - 退市价对应现金进入组合现金账户

3. 退市后处理
   - 资产从 securities 主集合移除
   - securities.Total 属性仍然可访问退市资产
   - securities_changed 事件通知策略
```

关键设计原则 LEAN 采用：

```csharp
// LEAN 使用的 Delisting 对象
{
    type: DelistingType     // WARNING / DELISTED
    price: decimal          // 退市价
    // 退市警告时，stock 属性指示的是退市前的收盘价
    // 退市执行时，stock 属性指示退市时的价格
}
```

LEAN 还在退市处理时自动取消未成交的订单，生成 OrderEvent 状态为 CANCELED。

#### 学术标准（CRSP 数据库）

- 包含 1925 年至今所有曾交易股票的完整历史
- 退市股票会记录退市后市场交易数据（有些退市股会在 OTC 市场交易）
- 退市时按退市价清算后，现金归投资者所有

### 3.3 退市处理的三个层级

| 层级 | 做法 | 影响 |
|------|------|------|
| ❌ 不管退市 | 只保留退市前数据，忽略退市事件 | **严重幸存者偏差**，回测结果虚高 |
| ⚠️ 退市价归零 | 退市后价格变为 0 | 过于激进，忽略退市过程中仍有交易 |
| ✅ 退市强制平仓 | 退市时按当时收盘价平仓，现金归入组合 | 相对最准确 |

### 3.4 新上市股票处理

IPO 相对简单：
- 从上市第一天起提供数据
- 回测中策略只能交易**当前时刻已上市**的股票
- 建议使用**向后看的标的池**（backward-looking universe），即每期只选择当时实际存在的股票

---

## 四、股票拆分处理

### 4.1 Zipline 的做法

拆股在 Zipline 中通过 `Float64Multiply` 作用于价格和成交量两个矩阵：

```python
# 价格调整（价格 × ratio⁻¹∶ 拆股后价格变小）
Float64Multiply(from_row, to_row, from_col, to_col, multiplier)

# 成交量调整（成交量 × ratio∶ 拆股后成交量变多）
Float64Multiply(from_row, to_row, from_col, to_col, 1.0 / multiplier)
```

### 4.2 LEAN 的做法

拆股影响通过 factor file 的 split 列控制：

```
# factor file 示例（AAPL）
# date        split_factor  dividend_factor
2010-01-04   1.0           1.0
2014-06-09   0.5           1.0   # 7:1 拆股 ÷ 反向因子 0.5
2020-08-28   1.0           0.82  # 除息
```

LEAN 还特别处理了**未成交订单受拆股影响**：

> "If a split event occurs before your order is filled, the unfilled portion of the order is adjusted automatically, where its quantity is multiplied by the split factor and the limit/stop/trigger price (if any) is divided by the split factor."

如果拆股发生在订单未完全成交之前，未成交部分的订单数量和价格会自动调整。

### 4.3 A 股拆股的处理

A 股常见的是"送转"（送股+转增），公式并入复权因子计算：

```
除权参考价 = (prev_close + rights_price × rights_ratio) / 
              (1 + stock_dividend_ratio + capitalization_ratio + rights_ratio)

adj_factor_new = adj_factor_old × actual_close / 除权参考价
```

---

## 五、数据标准化模式（Data Normalization）

### 5.1 QuantConnect (LEAN) 的五种模式

这是目前业界最完善的数据标准化方案：

| 模式 | 值 | 名称 | 行为 |
|------|-----|------|------|
| `Raw` | 0 | 原始价 | 不做任何修正。分红现金入账，拆股调整持仓 |
| `Adjusted` | 1 | **向后调整**（前复权） | 拆股 + 分红都回退修正历史价格，今日价格 = 市场价 |
| `SplitAdjusted` | 2 | 仅拆股调整 | 拆股回退修正价格，分红以现金入账 |
| `TotalReturn` | 3 | 总收益调整 | 所有未来分红价值加入初始价格（后复权变体） |
| `ScaledRaw` | 7 | 缩放原始 | 仅用于 history 请求，按当前时刻之前的调整因子缩放 |

重要特性：

> "If you use ADJUSTED, SPLIT_ADJUSTED, or TOTAL_RETURN, we use the entire split and dividend history to adjust historical prices. This process ensures you get the same adjusted prices, regardless of the backtest end date."

使用 ADJUSTED/SPLIT_ADJUSTED/TOTAL_RETURN 时，系统使用完整的拆分和分红历史来调整价格，确保无论回测结束日期是哪天，调整后的价格都一致（**解决了前复权的"未来函数"问题**）。

### 5.2 中国市场常见做法

| 系统 | 默认方式 |
|------|----------|
| **聚宽 (JoinQuant)** | 前复权（默认） |
| **RiceQuant (RQAlpha)** | 前复权（默认） |
| **Tushare** | 提供 adj_factor 原始数据，用户自选 |
| **Baostock** | 提供前复权/后复权/不复权三种参数选择 |

---

## 六、复权方法详解

### 6.1 三种复权方法定义

#### 前复权（Forward-adjusted / Adjusted）

```python
adj_price[t] = raw_price[t] × (latest_adj_factor / adj_factor[t])
```

- **当前价是真实市场价**
- 历史价格等比缩放
- **有未来函数**：新除权事件会改写所有历史价格
- 最常用于回测，因为指标计算连续且最新价可交易

#### 后复权（Backward-adjusted / Total Return）

```python
adj_price[t] = raw_price[t] × (adj_factor[first] / adj_factor[t])
```

- 历史价格不变
- 当前价被放大（不是真实市场价）
- 适合看长期收益走势，**不能用于下单**
- 没有未来函数问题

#### 不复权（Raw）

- 完全真实，但 K 线有除权缺口
- 技术指标（均线等）会因缺口而失真

### 6.2 A 股复权因子计算公式

聚宽/Tushare 等平台使用的标准公式：

```
P_prev     = 除权除息日前一日收盘价
P_rights   = 配股价
C          = 每股现金红利（含税）
S          = 送股比例（送股数/总股本）
T          = 转增比例（转增数/总股本）
R          = 配股比例（配股数/总股本）

# 除权除息参考价（证监会标准公式）
除权参考价 = (P_prev - C + P_rights × R) / (1 + S + T + R)

# 当日复权因子更新
adj_factor[ex_date] = adj_factor[ex_date-1] × actual_close / 除权参考价
```

然后通过累积乘积得到每日因子序列：

```
adj_factor[t] = Π(每个事件日的调整因子)
```

### 6.3 各系统默认复权方式

| 系统 | 默认方式 | 说明 |
|------|----------|------|
| **LEAN (QuantConnect)** | Adjusted（前复权） | 拆股+分红都调整，今天价格=市场价 |
| **Zipline** | 向后调整 | 基于 Float64Multiply 矩阵运算 |
| **聚宽** | 前复权 | 使用 adj_factor |
| **RiceQuant (RQAlpha)** | 前复权 | 类似聚宽 |
| **Tushare** | 提供原始复权因子 | 用户自己选前/后/不复权 |
| **Baostock** | 三种方式可选 | 用户指定参数 |
| **Backtrader** | 不复权 | 保持原始数据，在 Broker 层处理事件 |

---

## 七、各系统源码关键引用

### 7.1 Zipline 调整系统

- **`zipline/data/adjustments.py`** — SQLiteAdjustmentReader/Writer，调整数据库的读写
  - 维护 `splits`、`dividends`、`mergers` 三张表
  - 支持的调整类型: splits、dividends、mergers
  - 支持 `price` 和 `volume` 两种调整类型
- **`zipline/data/_adjustments.pyx`** — Cython 加速的调整加载
  - `load_adjustments_from_sqlite()` 将调整映射为 `Float64Multiply` 矩阵操作
  - 按 `(bar_index, asset_index) → multiplier` 映射
- **`zipline/lib/adjustment.py`** — `Float64Multiply` 调整对象定义

### 7.2 QuantConnect (LEAN) 因子体系

- **`Common/DataNormalizationMode.cs`** — 定义 Raw/Adjusted/SplitAdjusted/TotalReturn/ScaledRaw
- **`Engine/DataFeeds/`** — 数据加载层，应用 factor file 调整
- **CORPORATE ACTIONS 文档** — Splits/Dividends/SymbolChanges/Delistings 四种事件
- 调整策略：分两阶段——数据层做 factor 缩放，事件层通知策略

### 7.3 Backtrader

- **`backtrader/broker.py`** — BrokerBase 定义 Broker 接口，包括 `split()` 方法
- **`backtrader/brokers/ibbroker.py`** — IB Broker 实现，处理实盘拆分事件
- 核心哲学：**不修改历史数据**，事件补偿在 Broker 层完成

### 7.4 RQAlpha (RiceQuant)

- **Mod 体系**：sys_simulation（撮合+事件源）、sys_analyser（绩效分析）、sys_transaction_cost（税费）
- 使用复权因子调整数据

---

## 八、最佳实践建议

结合调研结果，对一个回测系统的设计建议：

### 8.1 数据层设计

```
adj_factors 表:
  symbol | trade_date | split_ratio | cash_dividend | cum_adj_factor
  000001 | 2020-01-01 | 1.0         | 0.0           | 1.000
  000001 | 2020-06-30 | 1.5         | 0.0           | 0.667   # 10送5
  000001 | 2021-04-15 | 1.0         | 0.35          | 0.665   # 每股派0.35

delistings 表:
  symbol | delist_date  | delist_price | reason
  600666 | 2021-05-25   | 0.18         | 终止上市
```

### 8.2 推荐策略

1. **K 线数据**：使用前复权价格（保证连续性，便于技术指标计算）
2. **成交执行**：使用真实价格（不复权，确保下单量正确）
3. **分红处理**：按现金入账方式计入组合，不调整价格（类似 LEAN 的 SplitAdjusted）
4. **退市处理**：维护完整的退市清单，退市时按退市价强制平仓
5. **标的池**：每期使用当时实际存在的股票（向后看），避免幸存者偏差
6. **IPO 处理**：只有在上市日期 ≥ 回测当前日期时方可交易

### 8.3 常见错误清单

| # | 错误 | 后果 |
|---|------|------|
| 1 | 只用当前存续股票做回测 | **幸存者偏差**，回测收益虚高 2-5%/年 |
| 2 | 回测中使用尚未上市的数据 | 未来函数，策略在现实中无法执行 |
| 3 | 用后复权价格计算下单量 | 后复权价不是市场价，下单价格错误 |
| 4 | 用前复权价格计算持仓市值 | 前复权下"当前价"不对应真实成本，损益计算错误 |
| 5 | 忽略成交量调整 | 拆股后成交量放大，量价指标失真 |
| 6 | 忽略现金分红再投资 | 长期回测收益偏低（A 股分红再投资占总收益 20%+） |

---

## 九、参考来源

1. **QuantConnect LEAN 文档** — Corporate Actions: https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions
2. **QuantConnect LEAN 文档** — Data Normalization: https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/requesting-data
3. **Zipline 源码** — `zipline/data/adjustments.py` (SQLiteAdjustmentReader/Writer)
4. **Zipline 源码** — `zipline/data/_adjustments.pyx` (Cython 加速调整加载)
5. **Backtrader 源码** — `backtrader/broker.py` (BrokerBase, split)
6. **Tushare Pro 文档** — 复权因子 adj_factor 接口
7. **RQAlpha 文档** — Mod 插件体系的设计
8. **Investopedia** — Adjusted Closing Price & Backtesting concepts
