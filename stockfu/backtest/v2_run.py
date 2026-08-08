"""V2 回测的加载、组装与运行入口。

从 configs/ 下的 yaml 加载 alpha / profile / portfolio / risk,绑定 raw 计算器,
组装成 V2RunConfig 后调用 v2_engine.run_v2_backtest。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

from stockfu.backtest.v2_engine import (
    V2RunConfig,
    V2Result,
    fn_source_fingerprint,
    run_v2_backtest,
)
from stockfu.factors.raw import raw_fingerprint
from stockfu.factors.raw.beta import compute_low_beta
from stockfu.factors.raw.dividend import compute_dividend_yield_ttm
from stockfu.factors.raw.value import compute_value
from stockfu.factors.raw.volatility import compute_low_volatility_20d
from stockfu.scoring.profiles import profile_from_dict
from stockfu.strategy.alpha import alpha_from_dict
from stockfu.strategy.portfolio import PortfolioConstructor, portfolio_from_dict
from stockfu.strategy.risk import RiskOverlay, risk_from_dict
from stockfu.services.universe import UniverseRules

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
ARCHIVE_MIGRATION_MAP = (
    CONFIGS.parent / "docs" / "legacy" / "strategy-v1" / "migration-map.yaml"
)


class RawComputerSpec(NamedTuple):
    """raw 计算器注册项：函数 + 算法名（算法名进入 checkpoint identity，
    替换 raw 实现/改参数都会改变指纹，阻止错误续跑）。"""

    fn: Callable
    algo: str


# raw_metric_id → 纯 raw 计算器 + 算法名(新因子迁移时在此登记)。
# algo 必须与 raw 模块内 raw_fingerprint(...) 的 algo 参数一致。
RAW_COMPUTERS = {
    "dividend_yield_ttm": RawComputerSpec(
        compute_dividend_yield_ttm, "ttm_cash_over_close_raw_zero_no_dividend"),
    "low_beta": RawComputerSpec(compute_low_beta, "cov_over_var_vs_bench"),
    "low_volatility_20d": RawComputerSpec(compute_low_volatility_20d, "std_ret_x_sqrt252_x100"),
    "value": RawComputerSpec(compute_value, "pe_percentile"),
}

# alpha 的默认 deployment。显式传入 --portfolio-v2/--risk-v2 仍可做对照实验；
# 其他 alpha 继续沿用原有默认组合与风险政策，避免策略间相互污染。
DEFAULT_V2_DEPLOYMENTS = {
    "dividend_low_vol_v2": {
        "portfolio_id": "cn_equity_top15_daily_softlock30_v2",
        "risk_id": "dividend_low_vol_trailing_v2",
    },
}


def _legacy_migration_map() -> dict:
    if not ARCHIVE_MIGRATION_MAP.is_file():
        return {}
    try:
        data = yaml.safe_load(ARCHIVE_MIGRATION_MAP.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    mapping = data.get("mapping")
    return mapping if isinstance(mapping, dict) else {}


def validate_v2_alpha_id(alpha_id: str) -> None:
    """在创建快照/初始化数据库前拒绝旧 V1 id，避免静默近似迁移。"""
    path = CONFIGS / "alphas" / f"{alpha_id}.yaml"
    if path.is_file():
        return
    if alpha_id in _legacy_migration_map():
        raise ValueError(
            f"V1 strategy_id {alpha_id!r} 已归档；请查阅 "
            "docs/legacy/strategy-v1/migration-map.yaml"
        )
    available = sorted(p.stem for p in (CONFIGS / "alphas").glob("*.yaml"))
    raise ValueError(f"未知 V2 alpha_id {alpha_id!r}；可选: {available}")


def _load(rel: str) -> dict:
    return yaml.safe_load((CONFIGS / rel).read_text(encoding="utf-8"))


def build_v2_config(
    alpha_id: str,
    portfolio_id: str,
    risk_id: str,
    codes: list[str],
    eval_start: date,
    eval_end: date,
    history_origin: date | None = None,
    initial_cash: float | None = None,
    observation_count: int | None = None,
    universe_rules: UniverseRules | None = None,
    checkpoint_path: str | None = None,
    resume_from: str | None = None,
    checkpoint_every: int = 1,
    snapshot: dict | None = None,
    snapshots_dir: str | None = None,
    canonical: bool = False,
) -> V2RunConfig:
    validate_v2_alpha_id(alpha_id)
    alpha = alpha_from_dict(_load(f"alphas/{alpha_id}.yaml"))
    profiles = {}
    for f in alpha.factors:
        profiles[f.profile_id] = profile_from_dict(_load(f"factor_profiles/{f.profile_id}.yaml"))

    raw_computers: dict[str, callable] = {}
    raw_params: dict[str, dict] = {}
    raw_fingerprints: dict[str, str] = {}
    raw_computer_bindings: dict[str, str] = {}
    for p in profiles.values():
        if p.raw_metric_id not in RAW_COMPUTERS:
            raise KeyError(f"raw_metric_id {p.raw_metric_id} 未在 RAW_COMPUTERS 登记")
        spec = RAW_COMPUTERS[p.raw_metric_id]
        raw_computers[p.raw_metric_id] = spec.fn
        params = dict(p.raw_metric_params)
        previous = raw_params.get(p.raw_metric_id)
        if previous is not None and previous != params:
            raise ValueError(
                f"同一 raw_metric_id={p.raw_metric_id} 被不同参数重复引用，"
                "请拆分 raw_metric_id/profile")
        # 算法指纹 = metric + algo 名 + 参数：进 manifest/checkpoint identity，
        # 并用于校验每条观测的 raw_fingerprint 一致。
        raw_fingerprints[p.raw_metric_id] = raw_fingerprint(
            p.raw_metric_id, spec.algo, params)
        # fn 源码指纹：把声明绑定到实际可调用对象，替换实现/换函数即失效。
        raw_computer_bindings[p.raw_metric_id] = fn_source_fingerprint(spec.fn)
        raw_params[p.raw_metric_id] = params

    portfolio = PortfolioConstructor(portfolio_from_dict(_load(f"portfolio_policies/{portfolio_id}.yaml")))
    risk = RiskOverlay(risk_from_dict(_load(f"risk_policies/{risk_id}.yaml")))

    if history_origin is None:
        # 默认预热 5 年:覆盖 low_vol/dividend 的 self 历史窗口到 mature
        history_origin = date(eval_start.year - 5, eval_start.month, eval_start.day)

    kw: dict = dict(
        alpha=alpha, portfolio=portfolio, risk=risk, profiles=profiles,
        raw_computers=raw_computers, raw_params=raw_params,
        raw_fingerprints=raw_fingerprints,
        raw_computer_bindings=raw_computer_bindings,
        codes=list(codes),
        eval_start=eval_start, eval_end=eval_end, history_origin=history_origin,
        market_scope=alpha.market_scope,
    )
    if initial_cash is not None:
        kw["initial_cash"] = initial_cash
    if observation_count is not None:
        kw["observation_count"] = observation_count
    if universe_rules is not None:
        kw["universe_rules"] = universe_rules
    if checkpoint_path is not None:
        kw["checkpoint_path"] = checkpoint_path
    if resume_from is not None:
        kw["resume_from"] = resume_from
    kw["checkpoint_every"] = checkpoint_every
    kw["snapshot"] = snapshot
    kw["snapshots_dir"] = snapshots_dir
    kw["canonical"] = canonical
    return V2RunConfig(**kw)


def hs300_universe() -> list[str]:
    """沪深300历史成分并集，供 V2 预加载；每日成员由 UniverseRules 过滤。

    这里返回历年成分的并集只是为了准备行情、分红和历史状态所需的候选数据，
    不能把并集本身当成某一天的可交易股票池。实际每日集合由
    ``UniverseContext.eligible_on`` 根据 ``effective_from/effective_to`` 决定。
    """
    from stockfu.services.index_universe import historical_member_codes

    return historical_member_codes(("000300",))


def historical_hs300_universe_rules() -> UniverseRules:
    """V2 沪深300历史成分规则；与 V1 公共宇宙服务共用 membership 数据。"""
    return UniverseRules(
        universe_id="cn_historical_baostock_csi300_v1",
        index_codes=("000300",),
    )


def historical_full_universe() -> list[str]:
    """沪深300+中证500历史成分并集，供 V2 预加载。"""
    from stockfu.services.index_universe import historical_member_codes

    return historical_member_codes()


def historical_full_universe_rules() -> UniverseRules:
    """沪深300+中证500历史点时成员规则；每日按有效区间过滤。"""
    from stockfu.services.index_universe import (
        HISTORICAL_INDEX_CODES, HISTORICAL_UNIVERSE_ID,
    )

    return UniverseRules(
        universe_id=HISTORICAL_UNIVERSE_ID,
        index_codes=HISTORICAL_INDEX_CODES,
    )


def default_universe(eval_start: date, eval_end: date) -> list[str]:
    """默认回测池:区间内有行情的 A 股个股(00/30/60/68 开头,6 位)。

    点时可交易性由引擎内 universe 过滤;此处只给候选全集。
    读引擎跟随 ``read_engine()``：V2 快照激活时读快照，否则读主库
    （§4.13.3-1：不得直接读 live ``engine``，否则外层 use_read_engine 失效）。
    """
    from sqlalchemy import text
    from stockfu.db import read_engine

    with read_engine().connect() as conn:
        rows = conn.execute(text(
            "select distinct asset_code from quote_snapshot "
            "where quote_date between :s and :e "
            "and length(asset_code)=6 "
            "and (asset_code like '00%' or asset_code like '30%' "
            "     or asset_code like '60%' or asset_code like '68%') "
            "order by asset_code"),
            {"s": eval_start.isoformat(), "e": eval_end.isoformat()}).all()
    return [r[0] for r in rows]


def run(alpha_id: str, *, eval_start: date, eval_end: date,
        codes: list[str] | None = None, portfolio_id: str | None = None,
        risk_id: str | None = None, history_origin: date | None = None,
        initial_cash: float | None = None,
        observation_count: int | None = None,
        universe_rules: UniverseRules | None = None,
        checkpoint_path: str | None = None,
        resume_from: str | None = None,
        checkpoint_every: int = 20,
        snapshot: dict | None = None,
        snapshots_dir: str | None = None,
        canonical: bool = False) -> V2Result:
    """便捷入口:默认 portfolio/risk 与 alpha 约定匹配。

    数据快照在股票池解析前确定（阻塞①）：未提供时新建或从 resume 工件恢复；
    codes 省略（默认池）时解析必须在快照只读上下文内，确保候选池也来自快照。
    """
    deployment = DEFAULT_V2_DEPLOYMENTS.get(alpha_id, {})
    if portfolio_id is None:
        portfolio_id = deployment.get("portfolio_id", "cn_equity_top15_v2")
    if risk_id is None:
        risk_id = deployment.get("risk_id", "no_overlay_v1")
    from stockfu.backtest.v2_engine import canonical_preflight, resolve_snapshot
    # fail-closed 预检（§4.13.3-2）：canonical 门禁必须先于任何副作用——
    # 此处 resolve_snapshot 可能新建快照，故 preflight 必须在它之前。
    if canonical:
        canonical_preflight(canonical)
    snap = resolve_snapshot(provided=snapshot, resume_from=resume_from,
                            snapshots_dir=snapshots_dir)
    if codes is None:
        from stockfu.backtest.snapshot import snapshot_engine
        from stockfu.db import use_read_engine
        with use_read_engine(snapshot_engine(snap)):
            codes = default_universe(eval_start, eval_end)
    cfg = build_v2_config(alpha_id, portfolio_id, risk_id, codes,
                          eval_start, eval_end, history_origin, initial_cash,
                          observation_count, universe_rules, checkpoint_path,
                          resume_from, checkpoint_every,
                          snapshot=snap, snapshots_dir=snapshots_dir,
                          canonical=canonical)
    return run_v2_backtest(cfg)
