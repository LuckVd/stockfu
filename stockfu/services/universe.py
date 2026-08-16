"""时点宇宙(point-in-time universe):A 股大盘候选池按日可投资集合。

设计目标(用户口径):
  - 基础池 ≈ quote_snapshot 中 ~800 只大盘候选(用户声明不再扩全 A)
  - 每日 U(t) 只用 ≤t 信息:list_date / delist / is_st / trade_status / 最短上市天数
  - 防名单污染(次新股上市前进截面)是红线;不做消息面/指数成分历史

用法:
  rules = UniverseRules()                     # 默认严谨
  base = resolve_base_codes("all")            # 或 watchlist / 显式列表
  ctx = UniverseContext.load(base, rules)     # 回测开始一次
  u = ctx.eligible_on(as_of, day_flags)       # 每个交易日
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import Asset, QuoteSnapshot, SecurityMaster

UNIVERSE_ID = "cn_large_pool_v1"


def board_of_code(code: str) -> str:
    """代码 → 板块(涨跌幅档)。688 科创 20%;300/301 创业板 20%;8/4 北交 30%;sw 行业指数无涨跌停;其余主板 10%。"""
    c = (code or "").strip()
    if c.startswith("sw"):
        return "index"
    if c.startswith("688"):
        return "star"
    if c.startswith(("300", "301")):
        return "chinext"
    if c.startswith(("8", "4")) and len(c) == 6:
        return "bse"
    return "main"


def limit_pct_for(board: str, is_st: bool = False) -> float:
    """涨跌停幅度 %。ST 统一按 5%(简化;实际 ST 主板 5%);指数(index)无涨跌停限制。"""
    if board == "index":
        return 999.0
    if is_st:
        return 5.0
    return {"star": 20.0, "chinext": 20.0, "bse": 30.0}.get(board or "main", 10.0)


@dataclass
class UniverseRules:
    """宇宙过滤规则(写入回测 meta,保证可审计)。"""
    universe_id: str = UNIVERSE_ID
    exclude_st: bool = True
    require_trading: bool = True          # trade_status!=0 或无行则剔除(新开仓)
    min_list_days: int = 60               # 上市(或首根K)后至少 N 个日历日
    use_list_date: bool = True            # 无 master 时退回 first_quote_date
    # 可选流动性(默认关:用户池已是大盘候选)
    min_amount_ma20: float | None = None
    # 非空时另要求当日属于指定历史指数成分；为空保持旧大盘候选池行为。
    index_codes: tuple[str, ...] = ()
    # 龙虎榜排雷(2026-08-15,lhb-precheck-2026.md):近 N 日有大额净卖事件
    # (单日 net_ratio < lhb_net_sell_threshold)的票不进截面。0=关闭。
    # PIT:榜单盘后披露,lhb_date<=as_of 可见、T+1 才下单,天然防未来。
    exclude_lhb_net_sell_days: int = 0
    lhb_net_sell_threshold: float = -2.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DayFlags:
    """单票单日状态(来自 quote_snapshot,仅 as_of 当日行)。"""
    is_st: bool = False
    trade_status: int = 1                 # 1 交易 / 0 停牌
    has_row: bool = False
    amount: float | None = None


@dataclass
class UniverseContext:
    """回测/诊断进程内缓存:主数据 + 首根 K,避免每日扫库。"""
    codes: list[str]
    rules: UniverseRules
    master: dict[str, SecurityMaster] = field(default_factory=dict)
    first_quote: dict[str, date] = field(default_factory=dict)
    memberships: dict[str, list[tuple[date, date | None]]] = field(default_factory=dict)
    # 龙虎榜排雷索引:code → 大额净卖日期列表(升序);规则关闭时为空 dict。
    lhb_net_sell: dict[str, list[date]] = field(default_factory=dict)

    @classmethod
    def load(cls, codes: list[str], rules: UniverseRules | None = None) -> "UniverseContext":
        rules = rules or UniverseRules()
        codes = sorted({c.strip() for c in codes if c and c.strip()})
        master: dict[str, SecurityMaster] = {}
        first_quote: dict[str, date] = {}
        if not codes:
            return cls(codes=[], rules=rules)
        with session_scope() as s:
            for row in s.exec(
                select(SecurityMaster).where(SecurityMaster.code.in_(codes))
            ).all():
                master[row.code] = row
            # 首根 K:防无 list_date 时的次新污染;严格 <= 任意 as_of 的下界
            # 按 quote_model_for 分表(sw/sh/sz 指数在 index_quote_daily,其余在 quote_snapshot)
            from sqlalchemy import func
            from stockfu.services.factors import quote_model_for
            by_table: dict[type, list[str]] = {}
            for code in codes:
                by_table.setdefault(quote_model_for(code), []).append(code)
            for model, cs in by_table.items():
                rows = s.exec(
                    select(model.asset_code, func.min(model.quote_date)).where(
                        model.asset_code.in_(cs)).group_by(model.asset_code)
                ).all()
                for code, d0 in rows:
                    if code and d0:
                        first_quote[code] = d0 if isinstance(d0, date) else date.fromisoformat(str(d0)[:10])
        memberships = {}
        if rules.index_codes:
            from stockfu.services.index_universe import memberships_for
            memberships = memberships_for(codes, rules.index_codes)
        lhb_net_sell: dict[str, list[date]] = {}
        if rules.exclude_lhb_net_sell_days > 0:
            from stockfu.models import LhbEvent
            lhb_net_sell = {}
            with session_scope() as s:
                for code, d in s.exec(
                    select(LhbEvent.asset_code, LhbEvent.lhb_date).where(
                        LhbEvent.asset_code.in_(codes),
                        LhbEvent.net_ratio < rules.lhb_net_sell_threshold,
                    ).order_by(LhbEvent.asset_code, LhbEvent.lhb_date)
                ).all():
                    lhb_net_sell.setdefault(code, []).append(
                        d if isinstance(d, date) else date.fromisoformat(str(d)[:10]))
        return cls(codes=codes, rules=rules, master=master, first_quote=first_quote,
                   memberships=memberships, lhb_net_sell=lhb_net_sell)

    def list_anchor(self, code: str) -> date | None:
        """可交易起点锚点:优先 list_date,否则 first_quote。"""
        m = self.master.get(code)
        if self.rules.use_list_date and m and m.list_date:
            return m.list_date
        return self.first_quote.get(code)

    def board(self, code: str) -> str:
        m = self.master.get(code)
        if m and m.board:
            return m.board
        return board_of_code(code)

    def eligible_on(self, as_of: date, day_flags: dict[str, DayFlags] | None = None) -> set[str]:
        """as_of 日可新开仓/可参与截面排名的集合。只用 ≤as_of 主数据 + 当日 flags。

        day_flags 必传(可为空 dict=当日全无行情)。传 None 直接 raise,防新 caller 静默零交易。
        """
        if day_flags is None:
            raise ValueError(
                "UniverseContext.eligible_on 需要 day_flags dict"
                "(可为空);勿省略——省略会导致宇宙静默为空"
            )
        rules = self.rules
        out: set[str] = set()
        for code in self.codes:
            if rules.index_codes:
                from stockfu.services.index_universe import member_on
                if not member_on(self.memberships.get(code, []), as_of):
                    continue
            m = self.master.get(code)
            if m and m.delist_date and as_of >= m.delist_date:
                continue
            anchor = self.list_anchor(code)
            if anchor is None:
                continue
            if as_of < anchor:
                continue
            if rules.min_list_days > 0 and (as_of - anchor).days < rules.min_list_days:
                continue
            fl = day_flags.get(code)
            if fl is not None:
                if rules.exclude_st and fl.is_st:
                    continue
                if rules.require_trading and fl.has_row and fl.trade_status == 0:
                    continue
                if rules.require_trading and not fl.has_row:
                    # 当日无行情行:不进截面(停牌/缺失);持仓侧另处理
                    continue
            else:
                # flags 字典无此 code:视为无行
                if rules.require_trading:
                    continue
            if (rules.min_amount_ma20 is not None and fl is not None
                    and fl.amount is not None and fl.amount < rules.min_amount_ma20):
                continue
            # 龙虎榜排雷:近 N 日(按自然日近似,覆盖 N*1.5 交易日)有大额净卖 → 剔除。
            if rules.exclude_lhb_net_sell_days > 0:
                ev_dates = self.lhb_net_sell.get(code)
                if ev_dates:
                    from bisect import bisect_right
                    i = bisect_right(ev_dates, as_of)
                    if i > 0 and (as_of - ev_dates[i - 1]).days <= rules.exclude_lhb_net_sell_days:
                        continue
            out.add(code)
        return out

    def summary(self, sizes: list[int] | None = None) -> dict:
        d = {
            "universe_id": self.rules.universe_id,
            "rules": self.rules.to_dict(),
            "base_size": len(self.codes),
            "master_coverage": len(self.master),
            "first_quote_coverage": len(self.first_quote),
            "membership_coverage": len(self.memberships) if self.rules.index_codes else None,
            "status_coverage": quote_status_coverage(self.codes),
        }
        if sizes:
            d["avg_size"] = round(sum(sizes) / len(sizes), 1)
            d["min_size"] = min(sizes)
            d["max_size"] = max(sizes)
        return d


def quote_status_coverage(codes: list[str] | None = None) -> dict:
    """quote_snapshot 上 is_st / trade_status 非空覆盖率(可观测过滤是否真实生效)。

    纯 SQL 聚合,不拉全表行。rate 低时 ST/停牌过滤接近 no-op → 应跑 --backfill-quote-status。
    """
    from sqlalchemy import text
    with session_scope() as s:
        if codes:
            # 参数化 IN:分批防 SQL 过长
            n = st_nn = ts_nn = st_pos = 0
            chunk = 400
            for i in range(0, len(codes), chunk):
                part = codes[i:i + chunk]
                ph = ",".join(f":c{j}" for j in range(len(part)))
                params = {f"c{j}": part[j] for j in range(len(part))}
                sql = text(
                    f"SELECT COUNT(*), "
                    f"SUM(CASE WHEN is_st IS NOT NULL THEN 1 ELSE 0 END), "
                    f"SUM(CASE WHEN trade_status IS NOT NULL THEN 1 ELSE 0 END), "
                    f"SUM(CASE WHEN is_st = 1 THEN 1 ELSE 0 END) "
                    f"FROM quote_snapshot WHERE asset_code IN ({ph})"
                )
                row = s.execute(sql, params).one()
                n += int(row[0] or 0)
                st_nn += int(row[1] or 0)
                ts_nn += int(row[2] or 0)
                st_pos += int(row[3] or 0)
        else:
            row = s.execute(text(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN is_st IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN trade_status IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN is_st = 1 THEN 1 ELSE 0 END) "
                "FROM quote_snapshot"
            )).one()
            n = int(row[0] or 0)
            st_nn = int(row[1] or 0)
            ts_nn = int(row[2] or 0)
            st_pos = int(row[3] or 0)
        if n == 0:
            return {"n_rows": 0, "is_st_rate": None, "trade_status_rate": None}
        return {
            "n_rows": n,
            "is_st_nonnull": st_nn,
            "trade_status_nonnull": ts_nn,
            "is_st_rate": round(st_nn / n, 4),
            "trade_status_rate": round(ts_nn / n, 4),
            "is_st_positive": st_pos,
        }


def resolve_base_codes(spec: str | list[str] | None) -> list[str]:
    """解析回测/诊断标的池。

    - None / "" / watchlist → 自选 Asset(cn)
    - historical_indices → 沪深300+中证500历史并集(回测每日再按成分期过滤)
    - all / stocks / market / pool → quote_snapshot 去重(大盘候选池)
    - "600519,000858" 或 list → 显式
    """
    if isinstance(spec, list):
        return sorted({c.strip() for c in spec if c and str(c).strip()})
    if spec is None or str(spec).strip() == "":
        with session_scope() as s:
            return sorted(
                a.code for a in s.exec(select(Asset).where(Asset.market == "cn")).all()
                if a.code
            )
    s = str(spec).strip()
    low = s.lower()
    if low in ("historical_indices", "historical_index", "csi300_csi500"):
        from stockfu.services.index_universe import historical_member_codes
        return historical_member_codes()
    if low in ("all", "stocks", "market", "pool", "cn_large", UNIVERSE_ID):
        with session_scope() as s:
            codes = [
                c for c in s.exec(select(QuoteSnapshot.asset_code).distinct()).all() if c
            ]
        return sorted(set(codes))
    if low in ("watchlist", "watch", "self"):
        return resolve_base_codes(None)
    return sorted({c.strip() for c in s.split(",") if c.strip()})


def load_day_flags(codes: Iterable[str], as_of: date) -> dict[str, DayFlags]:
    """批量读 as_of 日 is_st / trade_status / amount。"""
    codes = list(codes)
    out: dict[str, DayFlags] = {c: DayFlags(has_row=False) for c in codes}
    if not codes:
        return out
    with session_scope() as s:
        rows = s.exec(
            select(QuoteSnapshot).where(
                QuoteSnapshot.quote_date == as_of,
                QuoteSnapshot.asset_code.in_(codes),
            )
        ).all()
        for r in rows:
            st = bool(r.is_st) if r.is_st is not None else False
            # 名称兜底:部分源 is_st 空但 code 当日仍可能 ST——无历史名表时仅信 is_st 列
            ts = int(r.trade_status) if r.trade_status is not None else 1
            out[r.asset_code] = DayFlags(
                is_st=st,
                trade_status=ts,
                has_row=True,
                amount=float(r.amount) if r.amount is not None else None,
            )
    return out


def backfill_security_master(codes: list[str] | None = None) -> dict:
    """从 baostock query_stock_basic 回补 security_master。

    仅写入/更新 codes(默认=quote_snapshot 全部个股)。返回 {upserted, skipped, errors}。
    """
    if codes is None:
        codes = resolve_base_codes("all")
    code_set = set(codes)

    try:
        import baostock as bs
    except ImportError:
        return {"upserted": 0, "skipped": 0, "errors": 1, "error": "baostock 未安装"}

    from stockfu.data.baostock_proxy import ensure_baostock_login, run_baostock_query
    if not ensure_baostock_login():
        return {"upserted": 0, "skipped": 0, "errors": 1, "error": "baostock 登录失败"}

    # code -> (name, list_date, delist_date, status)
    basic: dict[str, tuple[str, date | None, date | None, str]] = {}
    try:
        rs = run_baostock_query(bs.query_stock_basic, label="stock-basic")
        while getattr(rs, "error_code", "1") == "0" and rs.next():
            row = rs.get_row_data()
            # code, code_name, ipoDate, outDate, type, status
            if len(row) < 6:
                continue
            if row[4] != "1":  # 仅股票
                continue
            raw = row[0]  # sh.600000
            code = raw.split(".")[-1] if raw else ""
            if code_set and code not in code_set:
                continue
            name = row[1] or ""
            list_d = _parse_d(row[2])
            delist_d = _parse_d(row[3]) if row[3] else None
            status = row[5] or "1"
            basic[code] = (name, list_d, delist_d, status)
    except Exception as exc:  # noqa: BLE001
        return {"upserted": 0, "skipped": 0, "errors": 1,
                "error": f"baostock query_stock_basic: {type(exc).__name__}: {exc}"}

    # 无 baostock 行时用 first_quote 兜底写入
    missing = code_set - set(basic.keys())
    first_q: dict[str, date] = {}
    if missing:
        with session_scope() as s:
            from sqlalchemy import func
            for code, d0 in s.exec(
                select(QuoteSnapshot.asset_code, func.min(QuoteSnapshot.quote_date))
                .where(QuoteSnapshot.asset_code.in_(list(missing)))
                .group_by(QuoteSnapshot.asset_code)
            ).all():
                if code and d0:
                    first_q[code] = d0 if isinstance(d0, date) else date.fromisoformat(str(d0)[:10])

    upserted = 0
    now = datetime.now()
    with session_scope() as s:
        for code in sorted(code_set):
            if code in basic:
                name, list_d, delist_d, status = basic[code]
            elif code in first_q:
                name, list_d, delist_d, status = "", first_q[code], None, "1"
            else:
                continue
            board = board_of_code(code)
            row = s.get(SecurityMaster, code)
            if row is None:
                s.add(SecurityMaster(
                    code=code, name=name, list_date=list_d, delist_date=delist_d,
                    board=board, status=status, updated_at=now,
                ))
            else:
                row.name = name or row.name
                row.list_date = list_d or row.list_date
                row.delist_date = delist_d
                row.board = board
                row.status = status
                row.updated_at = now
            upserted += 1
        s.commit()
    return {
        "upserted": upserted,
        "from_baostock": len(basic),
        "from_first_quote": len(first_q),
        "skipped": max(0, len(code_set) - upserted),
        "errors": 0,
    }


def _parse_d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
