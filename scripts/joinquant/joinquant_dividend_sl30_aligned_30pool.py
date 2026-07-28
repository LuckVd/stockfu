# -*- coding: utf-8 -*-
"""
聚宽(JoinQuant)策略回测脚本 —— 策略3 `dividend_cross_section#sl30` 交叉验证
============================================================================
【30 股对齐版 —— 与本地 run-20260728-211215 逐项同口径】

目的:用聚宽**独立回测引擎**重跑本策略,与本地 run-211215(红利横截面#sl30,
2021-01-04→2026-07-27,初始 100 万,qfq 口径,基准沪深300)对账。
本地基线:策略 -15.79% / 年化 -3.16% / 回撤 28.13% / 夏普 -0.32 /
基准(沪深300) -10.73% / 超额 -5.06% / 胜率 44.4% / 1149 笔 / 止损 27 笔。
**对账只看量级与排序,不要求逐位相等**(聚宽数据/撮合≠本地 baostock+自研引擎)。

⚠️ 为什么不是用旧的 research/joinquant_dividend_cross_section_sl30.py:
   那个脚本因子口径**未对齐本地**(低波=当前20日std横截面绝对排名、价值=-(PE+PB)横截面水平、
   稠密 percentile 加权、无 debounce、无组合刹车),跑出 -26.25% 是"另一套策略",不是交叉验证。
   本脚本是 c8ed150 对齐版(788池)的 30 股移植 + 补齐本地后来启用的组合刹车。

使用:聚宽官网 → 我的策略 → 新建策略 → 粘贴本文件全部内容 →
      设置初始资金 1,000,000、起止 2021-01-04 / 2026-07-27、频率"每天"→ 回测。
      30 股远小于 788,**聚宽免费版不会超时**(788 版必超时)。

---------------------------------------------------------------------------
【口径对齐清单 —— 跑之前逐项核对,这是交叉验证成立的唯一前提】
  1. 红利因子分母 = 不复权价(本地 close_raw)。本脚本 get_price(fq=None) 取真实价;
     禁止用前复权价当分母(名义现金÷qfq 会虚高 + 引入分红前视)。← 命门
  2. 低波/价值用前复权价算收益与 PE(get_price(fq='pre'))。
  3. 三因子权重 1.0/0.8/0.6;阈值:红利 high=5%/cap=20%、低波 30/70 分位、价值 20/80 分位。
     低波/价值都是【自身历史时序分位】(对齐本地算子),不是横截面绝对水平。
  4. 仓位连续映射:score≤-3 清仓 / |score|<3 死区维持 / score≥3 → 0.05×min(score/8,1);
     cap_and_rank 总仓≤95%、单股≤5%。对齐本地三段语义:减仓/清仓【直接放行不竞争】、
     增仓按 score 降序竞争额度、**超额尾部【维持现仓不清仓】**。
  5. 止损:个股亏≥30% 清仓(#sl30 变体)。本地 T+1(次日开盘成交),本脚本当日 open 近似。
  6. universe:本地 run-211215 用 cn_large_pool_v1(当前=30 股,base_size 30);本脚本用同一批
     固定 30 票(=run-211215 codes 并集),**逐票一致,消除票池变量**。
  7. 成本(对齐本地 engine):佣金万3双边 / 印花税卖出分段(2023-08-28 前千一、后万五)/ 滑点10bps。
  8. debounce(对齐本地 PositionManager,降换手):|目标-现仓|<1% 不下单、增仓冷却1天、
     部分减仓冷却1天、edge 0.5%。
  9. 组合回撤刹车(对齐本地 engine DEFAULT_PORTFOLIO_BRAKE=0.10):equity 较回测峰值回撤≥10%
     → 所有目标权重×0.5(含维持现仓的持仓);恢复到峰值 -10% 线内即释放。
     ← 本地 run-211215 此项 active(回撤28%必触发),是本地回撤28% vs 旧脚本44% 的直接原因,必须复刻。

【jqboson 本地引擎与聚宽云端文档的差异(已全部踩平,详见函数内注释,勿再改口径)】
  - get_price 多只股票返回【长格式】DataFrame(time/code/close),非横截面表 → _normalize_panel 归一。
  - 红利:jqboson 无 finance.STK_DIVIDEND;用 STK_XR_XD.bonus_ratio_rmb(每10股派息RMB,÷10÷不复权价)
    + a_xr_date(除权除息日),由 _resolve_dividend_source() hasattr 运行时解析(等价本地 TTM÷close_raw)。
  - PE 历史分位:jqboson/聚宽 valuation 表只能逐日查,本地用日频 PE(~1250点),本脚本按月采样(61点)
    近似 —— 这是 jqboson 不可避的局限;月采样对分位排序影响很小,阈值/映射与本地完全一致。
"""

# ============================ 参数(对齐本地 run-211215) ============================
HIGH_YIELD = 5.0          # 红利满分阈值 %
YIELD_CAP = 20.0          # 股息率封顶 %
W_DIV, W_LV, W_VAL = 1.0, 0.8, 0.6     # 三因子权重
LV_WINDOW = 20            # 低波:波动窗口(日);对齐本地 low_volatility.window
LV_HIST = 3 * 365 + 30    # 低波:历史 std 序列回溯(日);count=LV_HIST+LV_WINDOW = 3*365+20+30,
                           # 对齐本地 quote_series(hist_years*365 + window + 30)
VAL_YEARS = 5             # 价值:PE 时序分位回溯(年)
MAX_W = 0.05              # 单股上限 5 %
SCORE_FULL = 8.0          # 满仓刻度(score≥8 → 5%)
DEAD = 3.0                # 死区半宽(|score|<3 维持)
MAX_GROSS = 0.95          # 总仓位上限 95 %(留 5% 现金;对齐本地 sl30 产物 max_gross)
STOP_LOSS = 0.30          # 个股止损 30 %
BRAKE_DD = 0.10           # 组合回撤刹车:equity 较峰值回撤≥10% → 全局目标×0.5(对齐本地 DEFAULT_PORTFOLIO_BRAKE)
INIT_CASH = 1_000_000
# ---- debounce(对齐本地 engine PositionManager,降换手/过滤碎单)----
MIN_TRADE_W   = 0.01      # |目标-现仓|<1% 不下单(过滤碎单,本地 min_trade_weight)
EDGE          = 0.005     # |目标-上次成交目标|<0.5% 不下单(本地 edge_threshold 硬下限)
BUY_COOLDOWN  = 1         # 两次增仓间隔≥1 交易日(本地 buy_cool_down_days)
SELL_COOLDOWN = 1         # 两次部分减仓间隔≥1 交易日(本地 sell_cooldown_days;清仓不限)
# ---- 成本(对齐本地 engine:佣金万3双边 / 印花税卖出分段 / 滑点10bps)----
COMMISSION   = 0.0003     # 佣金 万3(双边;本地 COMMISSION_RATE)
STAMP_NEW    = 0.0005     # 印花税 2023-08-28 起 万五(仅卖出)
STAMP_OLD    = 0.001      # 印花税 2023-08-28 前 千一(仅卖出)
STAMP_SWITCH = '2023-08-28'  # 印花税分段日(字符串字典序比较,规避模块级 date 导入顺序)
SLIP         = 0.001      # 滑点 10 bps(本地 slip_bps=10)

from datetime import timedelta             # 聚宽策略环境可用标准库
import pandas as pd                        # concat 分批分红查询结果
from jqdata import *             # 引入 finance(分红表)+ 数据函数。finance 必须靠它引入。

# 本地 run-211215 的 30 股票池(codes 并集;cn_large_pool_v1 当前=30)。与本地逐票一致。
FIXED_UNIVERSE = [
    "000001", "000002", "000063", "000066", "000069", "000100", "000157", "000166",
    "000333", "000338", "000425", "000538", "000568", "000596", "000625", "000627",
    "000651", "000656", "000661", "000671", "000703", "000708", "000723", "000725",
    "000728", "000768", "000776", "000783", "000786", "000858",
]


def to_jq(code):
    """6 位裸代码 → 聚宽后缀代码(沪 .XSHG / 深 .XSHE / 北交所 .BJ)。"""
    c = str(code).strip().zfill(6)
    if c[0] == '6':                 # 60x/68x 沪市
        return c + '.XSHG'
    if c[0] in ('0', '3'):          # 00x/30x 深市
        return c + '.XSHE'
    return c + '.BJ'                # 8x/4x 北交所


# ===== 取价归一 =====
# ⚠️ 命门:聚宽 get_price 在【多只股票】时返回结构与单只【完全不同】(官方文档明示)。
# 本环境实测【多只 + panel=False】返回【长格式 DataFrame】,columns=['time','code','close']
# (每行一个 (日期,股票) 组合)—— 既非横截面表、也非官方示例的 Panel 式 dict-by-field。
# 原代码 `isinstance(px, dict)` 判失败 → 红利/低波因子【全 0】,只剩价值 → 选股反了。
# 修法:_normalize_panel 探测标识列(code/security)+ time 列筛行,兼容 Panel 式/横截面/dict 多形态。
def _normalize_panel(px, batch):
    """聚宽 get_price 多只标的返回 → {code: pd.Series(close, 时序升序)}。
    兼容多种形态(按优先级探测,首个出数据的形态即返回):
      1. Panel 式/dict 按字段: px['close'] → DataFrame(date×codes)   [官方文档示例形态]
      2. 长格式 DataFrame: columns 含 'code'/'security' + 'close'     [本环境实测形态 columns=time,code,close]
      3. 横截面 DataFrame: columns = codes / MultiIndex [('close',code)]
      4. dict 按标的: {code: DataFrame|Series}                         [分片形态]
    px 为空或均不可解析 → {}。"""
    out = {}
    if px is None:
        return out
    # 形态1: 按 'close' 字段索引 → DataFrame(date×codes)
    try:
        inner = px['close']
    except Exception:                                             # noqa: BLE001 探测式取值,失败即非此形态
        inner = None
    if inner is not None and hasattr(inner, 'columns'):
        cols = list(inner.columns)
        if cols and not isinstance(cols[0], tuple):
            for c in batch:
                if c in cols:
                    s = inner[c]
                    out[c] = s.dropna() if hasattr(s, 'dropna') else s
        if out:
            return out
    # 形态2: 长格式(标识列 code/security + time 列或 date 索引)—— 本环境实测形态
    if hasattr(px, 'columns'):
        cols = list(px.columns)
        idcol = 'code' if 'code' in cols else ('security' if 'security' in cols else None)
        if idcol and 'close' in cols:
            timecol = 'time' if 'time' in cols else None
            for c in batch:
                sub = px.loc[px[idcol] == c]
                if len(sub):
                    if timecol and timecol in list(sub.columns):
                        sub = sub.sort_values(timecol)            # 保证时序升序(供 rolling/iloc[-1])
                        out[c] = sub.set_index(timecol)['close'].dropna()
                    else:
                        out[c] = sub['close'].dropna()           # index 已是 date
            if out:
                return out
        # 形态3: 横截面(columns=codes)或 MultiIndex [('close', code)]
        if cols:
            if isinstance(cols[0], tuple):
                colset = set(cols)
                for c in batch:
                    if ('close', c) in colset:
                        out[c] = px[('close', c)].dropna()
            else:
                for c in batch:
                    if c in cols:
                        out[c] = px[c].dropna()
            if out:
                return out
    # 形态4: dict/Mapping 按标的 {code: ...}
    if hasattr(px, 'keys') and hasattr(px, '__getitem__'):
        for c in batch:
            if c in px:
                v = px[c]
                if hasattr(v, 'columns') and 'close' in list(v.columns):
                    out[c] = v['close'].dropna()
                elif hasattr(v, 'dropna'):
                    out[c] = v.dropna()
    return out


_PX_STEP = 20        # get_price 批量取价分批步长(长格式 date×code×close;步长兼顾性能与返回上限)


def _last_close(codes, ref_date, fq):
    """→ {code: 末日收盘 float}。count=2 避免单日退化;分批(每批150)。"""
    out = {}
    if not codes:
        return out
    for i in range(0, len(codes), 150):
        batch = codes[i:i + 150]
        px = get_price(batch, end_date=ref_date, count=2, frequency='daily',
                       fields=['close'], fq=fq, panel=False)
        for c, s in _normalize_panel(px, batch).items():
            if len(s):
                v = float(s.iloc[-1])
                if v == v and v > 0:                              # v==v 排除 NaN
                    out[c] = v
    return out


def _close_panel(codes, ref_date, count, fq):
    """→ {code: pd.Series(收盘, index=date)}。count>1 返回稳定 DataFrame;分批(每批 _PX_STEP 只)。"""
    out = {}
    if not codes:
        return out
    for i in range(0, len(codes), _PX_STEP):
        batch = codes[i:i + _PX_STEP]
        px = get_price(batch, end_date=ref_date, count=count, frequency='daily',
                       fields=['close'], fq=fq, panel=False)
        for c, s in _normalize_panel(px, batch).items():
            if len(s):
                out[c] = s
    return out


# ============================ 因子(口径逐行对齐本地) ============================
_DIV_SRC = None        # 缓存:(tbl, field, date_col, mode) 或 (None,None,None,None);None=未探测


def _resolve_dividend_source():
    """jqboson 红利数据通道解析(一次性、缓存)。jqboson 与聚宽云端文档不符:无 STK_DIVIDEND 表;
    有 STK_XR_XD,但无每股现金字段,用 bonus_ratio_rmb(每10股派息 RMB)。
    ⚠️ dir(finance) 不列 __getattr__ 动态表 → 必须 hasattr 逐候选枚举。
    返回 (table_obj, field, date_col, mode),mode:
      'cash'=每股现金(÷价)、'bonus10'=每10股派息RMB(÷10÷价)、'ratio'=已是收益率(直接用)。
    本环境命中:STK_XR_XD / bonus_ratio_rmb / a_xr_date / bonus10。"""
    global _DIV_SRC
    if _DIV_SRC is not None:
        return _DIV_SRC
    cands = ['STK_DIVIDEND', 'STK_XR_XD', 'STK_DIVIDEND_BASE', 'STK_HSR',
             'STK_DIVIDEND_SEND', 'STK_CAPITAL_CHANGE', 'STK_DIVIDEND_PU']
    present = [c for c in cands if hasattr(finance, c)]
    cash_cands = ('a_cash_div_tax', 'cash_div_tax', 'a_cash_div', 'cash_div', 'a_cash_div_pretax')
    bonus_cands = ('bonus_ratio_rmb', 'at_bonus_ratio_rmb', 'bonus_ratio_hkd', 'bonus_ratio_usd')
    ratio_cands = ('dividend_ratio', 'dividend_yield')
    date_cands = ('a_xr_date', 'a_bonus_date', 'ex_date', 'exdate',
                  'dividend_arrival_date', 'b_dividend_arrival_date', 'report_date')
    for tname in present:
        tbl = getattr(finance, tname)
        date_col = next((d for d in date_cands if hasattr(tbl, d)), None)
        cash_fld = next((f for f in cash_cands if hasattr(tbl, f)), None)
        bonus_fld = next((f for f in bonus_cands if hasattr(tbl, f)), None)
        ratio_fld = next((f for f in ratio_cands if hasattr(tbl, f)), None)
        if cash_fld and date_col:                            # 优先:每股现金股利(口径同本地)
            _DIV_SRC = (tbl, cash_fld, date_col, 'cash')
        elif bonus_fld and date_col:                         # 次选:每10股派息RMB(÷10÷价)
            _DIV_SRC = (tbl, bonus_fld, date_col, 'bonus10')
        elif ratio_fld and date_col:                         # 退化:dividend_ratio 当收益率
            _DIV_SRC = (tbl, ratio_fld, date_col, 'ratio')
        if _DIV_SRC is not None:
            return _DIV_SRC
    _DIV_SRC = (None, None, None, None)                      # 无可用表 → factor_dividend 优雅退化
    return _DIV_SRC


def factor_dividend(codes, ref_date):
    """红利 score:近365天现金分红收益率(封顶 20%)。分红表/字段/除息日列由 _resolve_dividend_source()
    运行时探测(jqboson:STK_XR_XD,字段 bonus_ratio_rmb、日期 a_xr_date)。模式:
      cash=每股现金÷价×100;bonus10=每10股RMB÷10÷价×100;ratio=dividend_ratio 当收益率(单位自适应)。
    ≥5%→+20;1~5% 线性;<1%→0。"""
    scores = {}
    if not codes:
        return scores
    closes = _last_close(codes, ref_date, fq=None)           # 分母不复权
    if not closes:
        return scores
    tbl, fld, date_col, mode = _resolve_dividend_source()
    if tbl is None:                                          # 无分红表 → 优雅退化
        return scores
    year_ago = ref_date - timedelta(days=365)
    closes_list = list(closes)
    _frames = []
    for i in range(0, len(closes_list), 200):               # 分批查:.in_(大池) 超限/超时
        try:
            flt = [tbl.code.in_(closes_list[i:i + 200])]
            if date_col is not None:                         # 除息日窗口(近365天)
                flt.append(getattr(tbl, date_col) >= year_ago)
                flt.append(getattr(tbl, date_col) <= ref_date)
            _d = finance.run_query(query(tbl).filter(*flt))
            if _d is not None and len(_d):
                _frames.append(_d)
        except Exception:                                    # noqa: BLE001 字段/列名探测错 → 静默跳过该批
            pass
    df = pd.concat(_frames, ignore_index=True) if _frames else None
    ttm = {}                                                 # {code: 近365天聚合值}
    if df is not None and len(df) and fld in df.columns:
        for c, grp in df.groupby('code'):
            v = grp[fld].sum()
            if v and v > 0:
                ttm[c] = float(v)
    # ratio 模式单位自适应:若值域像小数(<0.5,如 0.05=5%)→×100 转百分数
    if mode == 'ratio' and ttm and max(ttm.values()) < 0.5:
        ttm = {c: v * 100 for c, v in ttm.items()}
    for jqc, close in closes.items():
        v = ttm.get(jqc, 0.0)
        if mode == 'cash':                                   # 每股现金 ÷ 不复权价 ×100
            y = min(v / close * 100, YIELD_CAP) if v > 0 else 0.0
        elif mode == 'bonus10':                              # 每10股RMB ÷10 ÷价 ×100
            y = min(v / 10 / close * 100, YIELD_CAP) if v > 0 else 0.0
        else:                                                # ratio:已是收益率 %
            y = min(v, YIELD_CAP) if v > 0 else 0.0
        if y >= HIGH_YIELD:
            scores[jqc] = 20.0
        elif y >= 1.0:
            scores[jqc] = min(20 * (y - 1) / (HIGH_YIELD - 1), 20.0)
        else:
            scores[jqc] = 0.0
    return scores


def factor_low_vol(codes, ref_date):
    """低波 score:近 LV_WINDOW 日收益 std 在近 LV_HIST 滚动 std 序列的时序分位。
    <30 分位→正分 20×(1-pct/30);>70→负分;否则 0。对齐本地 low_volatility 算子。"""
    scores = {}
    panel = _close_panel(codes, ref_date, LV_HIST + LV_WINDOW, fq='pre')
    for c in codes:
        s = panel.get(c)
        if s is None or len(s) < LV_WINDOW + 1:
            continue
        rets = (s / s.shift(1) - 1).dropna()
        stds = rets.rolling(LV_WINDOW).std().dropna()
        if len(stds) < 10:
            continue
        cur = stds.iloc[-1]
        # 平均秩分位(对齐本地 factors.percentile:below + equal/2)
        n = len(stds)
        below = int((stds < cur).sum())
        equal = int((stds == cur).sum())
        pct = (below + equal / 2) / n * 100
        if pct < 30:
            scores[c] = 20 * (1 - pct / 30)
        elif pct > 70:
            scores[c] = -20 * (1 - (100 - pct) / 30)
        else:
            scores[c] = 0.0
    return scores


def factor_value(codes, ref_date):
    """价值 score:当前 PE 在近 VAL_YEARS 年时序分位。
    <20 分位→正分;>80→负分;否则 0。对齐本地 value 算子(阈值/映射一致)。
    注:本地用日频 PE 算分位;jqboson valuation 只能逐日查,这里按月采样(61点)近似 ——
    jqboson 不可避的局限(日频不可行),分位排序影响很小,勿为性能改。"""
    scores = {}
    # 当前 PE
    cur = get_fundamentals(query(valuation.code, valuation.pe_ratio).filter(
        valuation.code.in_(codes)), date=ref_date)
    cur = {r['code']: r['pe_ratio'] for r in cur.to_dict('records')
           if r['pe_ratio'] and r['pe_ratio'] > 0}
    # 历史月采样 PE 序列(每个 code 一条;近 5 年每月一点,足够做时序分位)
    hist = {c: [] for c in cur}
    start = ref_date - timedelta(days=VAL_YEARS * 365)
    months = [start + timedelta(days=30 * i) for i in range(0, VAL_YEARS * 12 + 1)]
    for d in months:
        df = get_fundamentals(query(valuation.code, valuation.pe_ratio).filter(
            valuation.code.in_(list(cur.keys()))), date=d)
        for r in df.to_dict('records'):
            if r['pe_ratio'] and r['pe_ratio'] > 0 and r['code'] in hist:
                hist[r['code']].append(float(r['pe_ratio']))
    for c, pe in cur.items():
        h = sorted(hist.get(c, []))
        if len(h) < 10:
            continue
        # 平均秩分位(对齐本地 valuation._percentile_sorted:below + equal/2)
        below = sum(1 for x in h if x < pe)
        equal = sum(1 for x in h if x == pe)
        pct = (below + equal / 2) / len(h) * 100
        if pct < 20:
            scores[c] = 20 * (1 - pct / 20)
        elif pct > 80:
            scores[c] = -20 * (1 - (100 - pct) / 20)
        else:
            scores[c] = 0.0
    return scores


# ============================ 引擎 ============================
def build_universe(ref_date):
    # 30 股固定票池(= 本地 run-211215 codes 并集)。剔 ST/停牌由 place_orders 的成交现实层兜底。
    return [to_jq(c) for c in FIXED_UNIVERSE]


def switch_cost(context):
    """印花税按日期分段(对齐本地 stamp_duty_rate):2023-08-28 前千一 / 后万五,仅卖出。
    每天 before_open 调一次(幂等)。佣金同步设万3(本地 COMMISSION_RATE)。"""
    tax = STAMP_NEW if context.current_dt.date().strftime('%Y-%m-%d') >= STAMP_SWITCH else STAMP_OLD
    set_order_cost(OrderCost(open_tax=0, close_tax=tax,
                             open_commission=COMMISSION, close_commission=COMMISSION,
                             close_today_commission=0, min_commission=5), type='stock')


def initialize(context):
    set_benchmark('000300.XSHG')           # 沪深300(对齐本地 run-211215 BENCHMARK sh000300)
    set_option('use_real_price', True)     # 真实价(不复权)下单
    set_option('avoid_future_data', True)  # 防未来函数
    set_slippage(PriceRelatedSlippage(SLIP))    # 滑点 10 bps(对齐本地 slip_bps=10)
    context.target = {}                    # 每日目标权重 {code: weight}
    context.peak_equity = INIT_CASH        # 组合回撤刹车:回测内权益峰值(对齐本地 peak_equity 初值=initial_cash)
    # debounce 状态(对齐本地 PositionManager:冷却/碎单/edge)
    context.last_buy  = {}                 # {code: date} 上次增仓日
    context.last_sell = {}                 # {code: date} 上次部分减仓日
    context.last_exec = {}                 # {code: weight} 上次成交目标权重
    # 调仓节奏:本地日频(T+1 开盘执行)。before_open 算 target → open 下单。
    run_daily(switch_cost, time='before_open')   # 印花税按日期分段(先于 calc_target)
    run_daily(calc_target, time='before_open')   # 日频(对齐本地)
    run_daily(stop_loss, time='open')       # 止损必须每日检查
    run_daily(place_orders, time='open')    # 开盘按 target 下单


def calc_target(context):
    """盘前算目标仓位(基于昨日 ref_date,防未来):三因子→连续映射→cap_and_rank→组合刹车。

    对齐本地 cap_and_rank.adjust 的三段语义:
      ① 减仓/清仓(0≤desired≤cur 或 desired==0)【直接放行,不占竞争额度】(风险优先);
      ② 增仓/建仓(desired>cur)按 score 降序竞争 MAX_GROSS —— 超额尾部【维持现仓,不清仓】
        (本地 final=cur 不写 0;原聚宽脚本误把尾部置 0 清仓,是曲线发散主因之一);
      ③ 死区(desired=None)维持。
    组合刹车(对齐本地 engine 1421-1426):equity 较峰值回撤≥BRAKE_DD → 所有目标×0.5。"""
    ref = context.previous_date
    codes = build_universe(ref)
    if not codes:
        context.target = {}
        return
    s_div = factor_dividend(codes, ref)
    s_lv = factor_low_vol(codes, ref)
    s_val = factor_value(codes, ref)
    # 加权聚合
    total = {}
    for c in set(s_div) | set(s_lv) | set(s_val):
        total[c] = (W_DIV * s_div.get(c, 0.0) + W_LV * s_lv.get(c, 0.0)
                    + W_VAL * s_val.get(c, 0.0))
    # 连续映射 → desired(0/None/weight)
    desired = {}
    for c, sc in total.items():
        if sc <= -DEAD:
            desired[c] = 0.0
        elif sc < DEAD:
            desired[c] = None              # 死区维持
        else:
            desired[c] = round(MAX_W * min(sc / SCORE_FULL, 1.0), 4)

    # 现仓权重(对齐本地 cap_and_rank 的 cur 基线;before_open 读的是前日收盘持仓)
    pv = context.portfolio.total_value or 1.0
    cur_w = {}
    for c, pos in context.portfolio.positions.items():
        if pos.total_amount > 0:
            cur_w[c] = pos.total_amount * pos.price / pv

    target = {}
    # ① 基线总仓:清仓归 0 / 减仓降到 desired(直接放行)/ 其余维持现仓
    gross0 = 0.0
    for c, cw in cur_w.items():
        d = desired.get(c)
        if d is None:                      # 死区 / 未覆盖 → 维持现仓
            gross0 += cw
        elif d <= 0:                       # 清仓 → 归 0(不占额度)
            target[c] = 0.0
        elif d <= cw + 1e-9:               # 减仓 → 降到 desired(直接放行,不竞争)
            target[c] = d
            gross0 += d
        else:                              # 增仓 → 暂按现仓计基线,下方竞争
            gross0 += cw
    for c, d in desired.items():
        if d == 0.0 and c in cur_w:        # score≤-3 且【已持仓】→ 清仓
            target[c] = 0.0
    # ② 增仓/建仓按 score 降序竞争剩余额度;超额尾部【维持现仓,不清仓】
    buys = sorted([(total[c], c, desired[c]) for c in desired
                   if desired[c] is not None and desired[c] > cur_w.get(c, 0.0) + 1e-9],
                  key=lambda x: (-x[0], x[1]))
    used = gross0
    for _sc, c, w in buys:
        inc = w - cur_w.get(c, 0.0)
        if used + inc <= MAX_GROSS:
            target[c] = w
            used += inc
        # 额度满的尾部【维持现仓,不清仓】—— 不进 target → place_orders 不动 = 维持

    # ③ 组合回撤刹车(对齐本地 engine DEFAULT_PORTFOLIO_BRAKE):equity 较峰值回撤≥BRAKE_DD
    #   → 所有目标权重×0.5(含维持现仓的持仓);peak 只增不减,恢复到峰值 -BRAKE_DD 线内即释放。
    if BRAKE_DD > 0:
        eq = context.portfolio.total_value or 0.0
        if context.peak_equity <= 0:
            context.peak_equity = eq
        context.peak_equity = max(context.peak_equity, eq)
        if context.peak_equity > 0 and eq / context.peak_equity - 1 <= -BRAKE_DD:
            braked = {}
            for c, cw in cur_w.items():       # 维持现仓的持仓也砍半(本地 final 全集×0.5)
                braked[c] = cw * 0.5
            for c, w in target.items():       # 显式目标(买/减/清)同样砍半,清仓保持 0
                braked[c] = (w * 0.5) if w else 0.0
            target = braked
    context.target = target


def stop_loss(context):
    """个股亏≥30% 立即清仓(每日,独立于调仓;清仓不限冷却)。在 place_orders 之前跑:
    清掉的票同步 debounce 状态并移出 target,避免 place_orders 再下一单。"""
    for c in list(context.portfolio.positions):
        p = context.portfolio.positions[c]
        if p.total_amount > 0 and p.avg_cost > 0:
            if p.price / p.avg_cost - 1 <= -STOP_LOSS:
                order_target_value(c, 0)
                context.last_exec[c] = 0.0          # 同步 debounce(已清,目标=0)
                context.target.pop(c, None)         # 已清,place_orders 不再处理


def place_orders(context):
    """按 context.target 用开盘价调仓 + debounce(对齐本地 PositionManager.should_act)。
    本环境无 order_target_percent → 用 order_target_value(目标市值=目标比例×总资产),等价。
    debounce:|目标-现仓|<MIN_TRADE_W / edge / 增仓冷却 / 部分减仓冷却 → 不下单(清仓不限冷却)。"""
    total = context.portfolio.total_value
    today = context.current_dt.date()
    for c, w in context.target.items():
        # 用 `in` 判断(聚宽 __contains__ 不触发"空Position"警告);只有已持仓才下标取 pos
        if c in context.portfolio.positions:
            pos = context.portfolio.positions[c]
            cur_w = pos.total_amount * pos.price / total if pos.total_amount > 0 else 0.0
        else:
            cur_w = 0.0
        # 碎单过滤:幅度太小不下单(本地 min_trade_weight;下限 0.005)
        if abs(w - cur_w) < MIN_TRADE_W:
            continue
        # edge:与上次成交目标差太小不下单(本地 edge_threshold)
        if abs(w - context.last_exec.get(c, cur_w)) < EDGE:
            continue
        if w > cur_w:                         # 增仓 / 建仓
            last = context.last_buy.get(c)
            if last is not None and (today - last).days < BUY_COOLDOWN:
                continue                      # 买入冷却未过(本地 buy_cool_down_days=1)
            order_target_value(c, w * total)
            context.last_buy[c] = today
            context.last_exec[c] = w
        elif w <= 0:                          # 清仓(不限冷却,如 cap_and_rank 放行的减仓到 0)
            order_target_value(c, 0)
            context.last_exec[c] = 0.0
        else:                                 # 部分减仓
            last = context.last_sell.get(c)
            if last is not None and (today - last).days < SELL_COOLDOWN:
                continue                      # 部分减仓冷却(本地 sell_cooldown_days=1)
            order_target_value(c, w * total)
            context.last_sell[c] = today
            context.last_exec[c] = w
