"""V2 回测引擎(设计 §9、§14、§15)。

逐日批量编排,严格时间协议:
    预热期 [history_origin, eval_start)   :只算 raw + 更新历史状态,不评分不交易
    观察期 eval_dates 的前 1/5             :评分(observation=True)但 no-trade
    formal  后 4/5                         :评分 + rebalance 日产生 t+1 订单

每日顺序(§9.3,硬约束):
    1. 执行 t-1 产生的待执行订单(成交时点可见数据)
    2. 解析 t 日点时 universe;评分只读 cutoff < t 的历史状态
    3. 批量算 raw → factor score(同一状态为全部股票评分)
    4. alpha 聚合 → (观察期跳过)组合+risk → t+1 待执行订单
    5. **所有评分完成后**才把 t 日观测追加进历史状态

记账/撮合/分红/费用复用 engine.py 已验证单元(§3.3);估值 qfq、credit_dividends=False
(qfq 已含分红再投)。本引擎只重写评分编排,不沾 V1 per-code analyze + score_full。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from stockfu.backtest.engine import (
    BENCHMARK,
    COMMISSION_RATE,
    INITIAL_CASH,
    MIN_COMMISSION,
    TRANSFER_FEE_RATE,
    VirtualAccount,
    Position,
    _backtest_series_ctx,
    _get_day_market,
    _get_trade_price,
    _metrics,
    _preload_dividend_events,
    _preload_market_range,
    _preload_cash_dividends,
    _preload_stock_dividends,
    _trade_calendar_days,
    settle_dividends,
)
from stockfu.backtest.cash_scaler import scale_buys_to_cash
from stockfu.scoring.contracts import (
    Maturity,
    RawFactorObservation,
    ScoreStatus,
    fingerprint,
)
from stockfu.scoring.history import HistoryState, compute_sample_dates
from stockfu.scoring.scorer import FactorScorer
from stockfu.scoring.profiles import FactorProfile
from stockfu.strategy.alpha import AlphaAggregator, AlphaDefinition
from stockfu.strategy.portfolio import DayContext, PortfolioConstructor
from stockfu.strategy.rebalancer import Rebalancer
from stockfu.strategy.risk import RiskOverlay
from stockfu.services.tradeability import ExecutionRules, check_fill, infer_pre_close
from stockfu.services.universe import DayFlags, UniverseContext, UniverseRules

_PRELOAD_LOOKBACK_DAYS = 1900      # 覆盖 raw 最大回看(low_vol ~年级)
_COMP_SHORT = {"self_history": "self", "market_history": "market",
               "industry_history": "industry"}   # history_specs 名 → update 短名
_CHECKPOINT_SCHEMA_VERSION = 2


# ----------------------------------------------------------- 配置与结果


@dataclass
class V2RunConfig:
    alpha: AlphaDefinition
    portfolio: PortfolioConstructor
    risk: RiskOverlay
    profiles: dict[str, FactorProfile]            # profile_id -> FactorProfile
    raw_computers: dict[str, Callable]             # raw_metric_id -> raw computer
    codes: list[str]
    eval_start: date
    eval_end: date
    history_origin: date
    initial_cash: float = INITIAL_CASH
    market_scope: str = "cn_equity"
    benchmark_code: str = BENCHMARK
    valuation_basis: str = "qfq"
    credit_dividends: bool = False                # qfq 已含分红再投
    observation_count: int | None = None          # None→ceil(0.2·eval);固定则 prefix invariant(§9.4)
    raw_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_fingerprints: dict[str, str] = field(default_factory=dict)   # raw_metric_id -> 算法指纹(观测校验+identity)
    raw_computer_bindings: dict[str, str] = field(default_factory=dict)  # raw_metric_id -> fn 源码指纹(绑定校验+identity)
    universe_rules: UniverseRules = field(default_factory=UniverseRules)
    execution_rules: ExecutionRules = field(default_factory=ExecutionRules)
    checkpoint_path: str | None = None
    resume_from: str | None = None
    checkpoint_every: int = 1
    snapshot: dict | None = None        # 数据快照 descriptor（§4.8.2；None→运行入口生成）
    snapshots_dir: str | None = None    # 快照输出目录（默认 data/snapshots）
    canonical: bool = False             # canonical 门禁：要求干净已提交工作树（§4.8.3）

    def __post_init__(self) -> None:
        if self.eval_end < self.eval_start:
            raise ValueError("eval_end 不得早于 eval_start")
        if self.history_origin > self.eval_start:
            raise ValueError("history_origin 不得晚于 eval_start")
        if self.valuation_basis not in ("raw", "qfq", "hfq"):
            raise ValueError("valuation_basis 必须是 raw/qfq/hfq")
        expected_credit = self.valuation_basis == "raw"
        if self.credit_dividends != expected_credit:
            raise ValueError(
                "credit_dividends 必须与 valuation_basis 一致：raw=True，qfq/hfq=False")
        if self.observation_count is not None and self.observation_count < 0:
            raise ValueError("observation_count 不得为负")
        if self.checkpoint_every <= 0:
            raise ValueError("checkpoint_every 必须为正整数")
        if (self.checkpoint_path or self.resume_from) and self.observation_count is None:
            raise ValueError("断点回测必须显式固定 observation_count")
        if self.raw_fingerprints and set(self.raw_fingerprints) != set(self.raw_computers):
            raise ValueError(
                "raw_fingerprints 必须与 raw_computers 一一对应: "
                f"指纹={sorted(self.raw_fingerprints)} "
                f"计算器={sorted(self.raw_computers)}")
        # 断点回测必须能证明 raw 算法身份：空指纹不允许（无法校验观测/续跑身份）。
        if (self.checkpoint_path or self.resume_from) and set(
                self.raw_fingerprints) != set(self.raw_computers):
            raise ValueError(
                "断点回测必须提供完整 raw_fingerprints（与 raw_computers 一一对应），"
                "否则无法校验 raw 算法身份与续跑")
        # 声明的 fn 绑定必须与实际的 computer 函数一致：直接替换 callable 但保留
        # 声明（旧 bug 可绕过 identity）→ 立即拒绝。
        if self.raw_computer_bindings:
            actual = {m: fn_source_fingerprint(self.raw_computers[m])
                      for m in self.raw_computers}
            if any(actual[m] != self.raw_computer_bindings.get(m) for m in actual):
                raise ValueError(
                    "raw_computer_bindings 与实际 raw computer 函数不匹配："
                    "请通过 v2_run 注册表绑定 raw 计算器，不要直接替换 callable")

    def manifest(self, **extra) -> dict:
        base = {
            "alpha_fingerprint": self.alpha.fingerprint(),
            "portfolio_fingerprint": self.portfolio.policy.fingerprint(),
            "risk_fingerprint": self.risk.policy.fingerprint(),
            "profile_fingerprints": {pid: p.mapping_fingerprint() for pid, p in self.profiles.items()},
            "codes_count": len(self.codes),
            "eval_start": self.eval_start.isoformat(),
            "eval_end": self.eval_end.isoformat(),
            "history_origin": self.history_origin.isoformat(),
            "initial_cash": self.initial_cash,
            "market_scope": self.market_scope,
            "benchmark_code": self.benchmark_code,
            "valuation_basis": self.valuation_basis,
            "credit_dividends": self.credit_dividends,
            "observation_count": self.observation_count,
            "raw_metric_params": {m: dict(self.raw_params[m]) for m in sorted(self.raw_params)},
            "raw_metric_fingerprints": dict(sorted(self.raw_fingerprints.items())),
            "raw_computer_bindings": dict(sorted(self.raw_computer_bindings.items())),
            "codes_fingerprint": fingerprint(sorted(self.codes), prefix="v2.codes"),
            "universe_rules": self.universe_rules.to_dict(),
            "execution_rules": self.execution_rules.to_dict(),
            "data_snapshot": self.snapshot,
        }
        base.update(extra)
        return base

    def checkpoint_identity(self) -> str:
        """可续跑配置指纹；故意忽略 eval_end，允许固定口径向后延长终点。

        data_snapshot 只绑稳定 snapshot_id（内容 hash），不绑 created_at/path——
        同一快照幂等重建 descriptor 时 snapshot_id 不变，合法 resume 不被误拒。
        manifest 仍保留完整 descriptor 供人工审计。
        """
        data = self.manifest()
        data.pop("eval_end", None)
        snap = data.get("data_snapshot")
        if snap:
            data["data_snapshot"] = {"snapshot_id": snap.get("snapshot_id")}
        return fingerprint(data, prefix="v2.checkpoint.config")


@dataclass
class V2Result:
    metrics: dict
    equity_curve: list[dict]                      # 全期(含预热/观察)
    formal_equity_curve: list[dict]               # 仅 formal 期
    benchmark: list[dict]                         # formal 期归一基准
    trades: list[dict]
    manifest: dict
    history_checkpoint: dict
    observation_summary: dict
    formal_summary: dict
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    score_diagnostics: dict = field(default_factory=dict)
    daily_audit: list[dict] = field(default_factory=list)


# ----------------------------------------------------------- 辅助


def git_revision() -> dict:
    """当前 git commit + 工作区是否 dirty（设计 §14：每次运行必须保存 git commit）。"""
    import subprocess

    def _run(cmd: list[str]) -> str | None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    commit = _run(["git", "rev-parse", "HEAD"])
    if commit is None:
        return {"commit": None, "dirty": None}
    porc = _run(["git", "status", "--porcelain"])
    # status 失败/超时代表工作区状态未知，不能把 unknown 折叠成 clean；
    # canonical_preflight 只接受明确的 ``dirty is False``，因此会 fail-closed。
    if porc is None:
        return {"commit": commit, "dirty": None}
    return {"commit": commit, "dirty": bool(porc)}


# 唯一受支持的依赖锁文件（§4.16.2）：浮动 requirements.txt 不是锁，不得作为
# canonical 可复现身份。uv.lock 是 TOML 格式、当前解析器未实现，不得声明支持——
# 要么只认一种真源（pip requirements 风格），要么连解析器/测试整套替换。
_SUPPORTED_LOCK_FILES = ("requirements.lock",)


def lock_identity() -> dict | None:
    """受支持依赖锁文件的身份；无锁文件时返回 None（canonical 拒绝）。

    返回 ``{"lock_file": ..., "lock_sha256": ...}``。只认明确生成的
    requirements.lock，未锁的 requirements.txt / pyproject.toml / uv.lock 一律
    不算（§4.14.2 阻塞一、§4.16.2）。
    """
    for name in _SUPPORTED_LOCK_FILES:
        p = Path(name)
        if p.exists():
            return {
                "lock_file": name,
                "lock_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
    return None


# 顶层 requirement：name[extra]==<PEP440 版本>，可选尾部续行符 ``\``。
# 版本字符类不含 ,<>~*! 等范围/多版本分隔符——非精确版本必须拒绝。
_LOCK_TOP_RE = re.compile(
    r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?==([0-9A-Za-z.+-]+?)\s*(?:\\\s*)?$")
# 只认 sha256 的 64 位 hex 下载哈希（uv pip compile --generate-hashes 输出），
# 允许尾部续行符 ``\``（uv 输出中除最后一行外的 hash 行都带）。
_LOCK_HASH_RE = re.compile(r"^--hash=sha256:[0-9a-fA-F]{64}\s*(?:\\\s*)?$")


def _parse_lock_versions(lock: dict) -> dict[str, str]:
    """解析 requirements.lock 为 {规范化包名: 版本}（fail-closed，§4.16.2）。

    只接受 uv pip compile --generate-hashes 输出的 pip requirements 风格：
    每个顶层 requirement 必须精确 ``==`` 且带至少一个 ``--hash=sha256:``。
    空锁、无法识别的非注释行、非精确版本、缺 hash、同包重复（重复冲突）
    一律 ValueError——杜绝“循环零次返回 True”的假阳性放行。
    """
    text = Path(lock["lock_file"]).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    top: str | None = None      # 当前顶层 requirement 行（已 strip）
    cont: list[str] = []        # 其缩进续行（--hash / # via 说明）

    def _flush() -> None:
        nonlocal top, cont
        if top is None:
            return
        m = _LOCK_TOP_RE.match(top)
        if not m:
            raise ValueError(
                "依赖锁格式不支持：顶层 requirement 必须是精确版本 "
                f"name==version（当前行：{top!r}），浮动/范围/多版本均不接受")
        # PEP 503/packaging 的 distribution 名称等价规则：大小写不敏感，且
        # 连续的 ``-``/``_``/``.`` 都归一成 ``-``。否则同一包可换拼法绕过
        # 重复声明门禁（如 typing-extensions / typing_extensions）。
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if not any(_LOCK_HASH_RE.match(line) for line in cont):
            raise ValueError(
                f"依赖锁格式不支持：{m.group(1)} 缺少 --hash=sha256:<64hex> 行；"
                "请用 uv pip compile --generate-hashes 生成锁文件")
        for line in cont:
            if line.startswith("#"):
                continue
            if _LOCK_HASH_RE.match(line):
                continue
            if line.startswith("--hash="):
                raise ValueError(
                    f"依赖锁只认 --hash=sha256:<64hex> 哈希行：{line!r}")
            raise ValueError(f"依赖锁包含无法识别的续行：{line!r}")
        if name in out:
            raise ValueError(f"依赖锁重复声明包 {name}（重复冲突）")
        out[name] = m.group(3)
        top, cont = None, []

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith((" ", "\t")):
            line = raw.strip()
            if top is None:
                raise ValueError(
                    f"依赖锁包含没有所属 requirement 的孤立续行：{line!r}")
            cont.append(line)
            continue
        _flush()
        if raw.startswith("#"):
            continue        # 顶层注释（uv 文件头说明），不算 requirement
        top = raw.strip()
    _flush()
    if not out:
        raise ValueError(
            "依赖锁解析为空：requirements.lock 中没有有效的精确版本 requirement")
    return out


def lock_matches_environment(lock: dict) -> bool:
    """lock 中每个包都已安装且版本一致（缺包/漂移 → False）。

    canonical 门禁据此拒绝“环境与锁不符”的假阳性：同一锁文件在不同环境
    安装出不同版本时，无法证明本次运行环境 = 锁定的环境。
    锁文件本身不合法（空/无 hash/非精确版本/重复）时解析器抛 ValueError，
    同样 fail-closed——不再因“零个包可比对”而放行（§4.16.2）。
    """
    from importlib.metadata import PackageNotFoundError, version

    parsed = _parse_lock_versions(lock)
    if not parsed:
        return False        # 防御兜底：解析器已拒绝空集，这里再 fail-closed 一次
    for name, ver in parsed.items():
        try:
            if version(name) != ver:
                return False
        except PackageNotFoundError:
            return False
    return True


def environment_identity() -> dict:
    """实际运行环境身份（§4.14.2 方案 3）：解释器/版本/平台/SQLite +
    规范化已安装 distribution ``name==version`` 列表 hash。写入 manifest 供审计，
    同一环境重复调用结果相同，任何安装变更都会改变 installed_hash。
    """
    import platform as _platform
    import sqlite3 as _sqlite3
    import sys as _sys
    from importlib.metadata import distributions

    dists = sorted(
        f"{d.metadata['Name']}=={d.version}" for d in distributions())
    h = hashlib.sha256()
    for d in dists:
        h.update(d.encode("utf-8"))
        h.update(b"\n")
    return {
        "python_impl": _sys.implementation.name,
        "python_version": _sys.version.split()[0],
        "platform": _platform.platform(),
        "sqlite_version": _sqlite3.sqlite_version,
        "installed_hash": h.hexdigest(),
    }


def canonical_preflight(canonical: bool) -> dict:
    """无副作用 canonical 预检（§4.13.3-2），调用前不得有任何写盘/建库/查库。

    canonical=True 时 fail-closed 要求（任一不满足立即 ValueError）：
    - git commit 完整 40 位（git 不可用/无 HEAD → 拒绝，不再 fail-open）；
    - dirty is False（git status 未知 → 拒绝）；
    - 真实依赖锁文件存在（唯一真源 requirements.lock；浮动 requirements.txt
      不是锁，§4.14.2 阻塞一 → 拒绝）；
    - 锁文件内容合法（非空/精确版本/带 sha256 hash，§4.16.2 → 拒绝）；
    - 当前安装环境与锁一致（缺包/版本漂移 → 拒绝）。
    非 canonical 不设门禁。返回 run_meta（git/lock/env/reproducibility），
    供 run_v2_backtest 直接复用进 manifest，避免重复查 git/文件。
    """
    run_meta = {
        "git": git_revision(),
        "lock": lock_identity(),
        "env": environment_identity(),
    }
    if canonical:
        git = run_meta["git"]
        commit = git.get("commit")
        if not commit or len(str(commit)) != 40:
            raise ValueError(
                "canonical 回测要求已提交代码（完整 40 位 git commit）；"
                "当前仓库无 HEAD 或 git 不可用。探索性运行请使用 canonical=False")
        if git.get("dirty") is not False:
            raise ValueError(
                "canonical 回测要求干净工作树和已提交代码（git dirty）；"
                "请先提交 V2 代码与配置。探索性运行请使用 canonical=False")
        lock = run_meta["lock"]
        if not lock:
            raise ValueError(
                "canonical 回测要求提交真实依赖锁文件 requirements.lock；"
                "浮动 requirements.txt 不是锁，不能作为可复现依赖身份。"
                "请用 uv pip compile --generate-hashes 生成并提交锁文件")
        if not lock_matches_environment(lock):
            raise ValueError(
                "当前安装环境与依赖锁不一致（缺包或版本漂移）；"
                "请按 requirements.lock 重建/同步环境后再运行 canonical")
    run_meta["reproducibility"] = {
        "status": ("canonical" if canonical
                   else ("non_canonical_dirty" if run_meta["git"].get("dirty")
                         else "non_canonical")),
        "git_commit": run_meta["git"].get("commit"),
        "git_dirty": bool(run_meta["git"].get("dirty")),
        # deps_hash 保留旧字段名（resume 链校验复用），语义 = 锁文件 sha256。
        "deps_hash": (run_meta["lock"] or {}).get("lock_sha256"),
        "lock_file": (run_meta["lock"] or {}).get("lock_file"),
        "env_identity": run_meta["env"],
    }
    return run_meta


def fn_source_fingerprint(fn: Callable) -> str:
    """raw computer 函数指纹：源码文本 hash。

    替换实现/换函数（含同名不同实）都会改变指纹，用于把声明的
    raw 指纹真正绑定到实际可调用对象（v2_run 注册表绑定 + 引擎校验）。
    无法读取源码时回退到 module.qualname。
    """
    import inspect

    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        src = f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', repr(fn))}"
    return fingerprint(src, prefix="raw.fn")


def _load_listing_and_industry(codes: list[str]) -> tuple[dict, dict]:
    """一次性查 stock_basic 的上市日与行业(点时近似:用当前分类,见 v2-notes §0.4)。"""
    from sqlalchemy import text
    from stockfu.db import read_engine

    listing: dict[str, date] = {}
    industry: dict[str, str | None] = {}
    codes = list(codes)
    with read_engine().connect() as conn:
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            ph = ",".join(f":c{j}" for j in range(len(chunk)))
            params = {f"c{j}": chunk[j] for j in range(len(chunk))}
            rows = conn.execute(text(
                f"select code, listing_date, industry from stock_basic "
                f"where code in ({ph})"), params).all()
            for r in rows:
                ld = r[1]
                listing[r[0]] = date.fromisoformat(ld) if ld else None
                industry[r[0]] = r[2]
    return listing, industry


def _amount_20d(sctx, code: str, as_of: date, window: int = 20) -> float:
    di = sctx.date_idx.get(as_of)
    if di is None:
        return 0.0
    cols = sctx.series.get(code)
    if cols is None:
        return 0.0
    arr = cols.get("amt")
    if arr is None:
        return 0.0
    lo = max(0, di - window + 1)
    vals = [arr[i] for i in range(lo, di + 1) if not math.isnan(arr[i])]
    return sum(vals) / len(vals) if vals else 0.0


def _classify(current_w: float, target_w: float) -> str:
    if target_w <= 0.0 and current_w > 0.0:
        return "sell"
    if target_w < current_w - 0.001:
        return "reduce"
    if target_w > current_w + 0.001:
        return "add" if current_w > 0.0 else "buy"
    return "hold"


def _targets_differ(left: dict[str, float], right: dict[str, float],
                    tol: float = 1e-12) -> bool:
    """比较两个目标权重，缺失代码按 0 处理。"""
    for code in set(left) | set(right):
        if abs(float(left.get(code, 0.0)) - float(right.get(code, 0.0))) > tol:
            return True
    return False


def _validate_raw_observation(obs: RawFactorObservation, metric: str,
                              as_of: date, expected_unit: str,
                              expected_asset: str,
                              expected_fingerprint: str = "") -> None:
    """在进入评分/历史状态前执行 RawFactorObservation 的硬契约检查。

    设计 §16 将 source_max_date>as_of 定义为硬失败；同时拒绝计算器静默返回
    错 asset/metric/单位、valid 却无值、invalid 却带值或缺失原因、空指纹，
    避免接错 raw 后仍能产出看似正常的分数。expected_fingerprint 非空时还要求
    每条观测的 raw_fingerprint 与声明算法指纹一致（阻断「计算器换了算法但
    声明指纹没变」的静默错配）。
    """
    if not isinstance(obs, RawFactorObservation):
        raise ValueError(f"raw computer 必须返回 RawFactorObservation: {metric}")
    if obs.asset_code != expected_asset:
        raise ValueError(
            f"raw asset 不匹配: expected={expected_asset}, got={obs.asset_code}")
    if obs.raw_metric_id != metric:
        raise ValueError(
            f"raw metric 不匹配: expected={metric}, got={obs.raw_metric_id}")
    if obs.as_of != as_of:
        raise ValueError(f"raw as_of 不匹配: expected={as_of}, got={obs.as_of}")
    if obs.raw_unit != expected_unit:
        raise ValueError(
            f"raw unit 不匹配({metric}): expected={expected_unit}, got={obs.raw_unit}")
    if obs.source_max_date is None:
        raise ValueError(f"raw source_max_date 不能为空: {metric}/{obs.asset_code}")
    source_max_date = (obs.source_max_date.date()
                       if isinstance(obs.source_max_date, datetime)
                       else obs.source_max_date)
    if not isinstance(source_max_date, date):
        raise ValueError(f"raw source_max_date 必须是日期: {metric}/{obs.asset_code}")
    if source_max_date > as_of:
        raise ValueError(
            f"raw source_max_date 违反点时约束: {metric}/{obs.asset_code} "
            f"source_max_date={source_max_date} > as_of={as_of}")
    if obs.available_at is None:
        raise ValueError(f"raw available_at 不能为空: {metric}/{obs.asset_code}")
    available = (obs.available_at.date()
                 if isinstance(obs.available_at, datetime) else obs.available_at)
    if not isinstance(available, date):
        raise ValueError(f"raw available_at 必须是日期: {metric}/{obs.asset_code}")
    if available > as_of:
        raise ValueError(
            f"raw available_at 违反点时约束: {metric}/{obs.asset_code} "
            f"available_at={available} > as_of={as_of}")
    if not isinstance(obs.raw_fingerprint, str) or not obs.raw_fingerprint:
        raise ValueError(f"raw_fingerprint 不能为空: {metric}/{obs.asset_code}")
    if expected_fingerprint and obs.raw_fingerprint != expected_fingerprint:
        raise ValueError(
            f"raw_fingerprint 与声明算法指纹不一致({metric}/{obs.asset_code}): "
            f"obs={obs.raw_fingerprint[:16]}… expected={expected_fingerprint[:16]}…")
    if not obs.valid and obs.raw_value is not None:
        raise ValueError(f"invalid raw 不能带 raw_value: {metric}/{obs.asset_code}")
    if obs.valid and obs.raw_value is None:
        raise ValueError(f"valid raw 必须带 raw_value: {metric}/{obs.asset_code}")
    if obs.raw_value is not None and not math.isfinite(float(obs.raw_value)):
        raise ValueError(f"raw_value 必须为有限数: {metric}/{obs.asset_code}")
    if not obs.valid and obs.missing_reason is None:
        raise ValueError(f"invalid raw 必须带 missing_reason: {metric}/{obs.asset_code}")


def _checkpoint_jsonable(value: Any) -> Any:
    """把回测状态转成稳定 JSON；checkpoint 不使用 pickle。"""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _checkpoint_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_checkpoint_jsonable(v) for v in value]
    if isinstance(value, set):
        return [_checkpoint_jsonable(v) for v in sorted(value)]
    return value


def _account_to_checkpoint(account: VirtualAccount) -> dict[str, Any]:
    return {
        "initial": account.initial,
        "cash": account.cash,
        "cash_receivable": account.cash_receivable,
        "fee_paid": account.fee_paid,
        "dividend_received": account.dividend_received,
        "dividend_tax_paid": account.dividend_tax_paid,
        "positions": {
            code: {
                "shares": pos.shares,
                "avg_cost": pos.avg_cost,
                "lots": [[shares, buy_date.isoformat()] for shares, buy_date in pos.lots],
                "receivable_shares": pos.receivable_shares,
                "peak_close": pos.peak_close,
                "take_profit_anchor_shares": pos.take_profit_anchor_shares,
                "take_profit_fired": sorted(pos.take_profit_fired),
                "take_profit_cap_shares": pos.take_profit_cap_shares,
            }
            for code, pos in sorted(account.positions.items())
        },
    }


def _account_from_checkpoint(data: dict[str, Any]) -> VirtualAccount:
    account = VirtualAccount(float(data["initial"]))
    account.cash = float(data["cash"])
    account.cash_receivable = float(data.get("cash_receivable", 0.0))
    account.fee_paid = float(data.get("fee_paid", 0.0))
    account.dividend_received = float(data.get("dividend_received", 0.0))
    account.dividend_tax_paid = float(data.get("dividend_tax_paid", 0.0))
    account.positions = {}
    for code, raw in (data.get("positions") or {}).items():
        pos = Position(
            shares=int(raw.get("shares", 0)),
            avg_cost=float(raw.get("avg_cost", 0.0)),
            lots=[(int(shares), date.fromisoformat(str(buy_date)))
                  for shares, buy_date in raw.get("lots", [])],
            receivable_shares=int(raw.get("receivable_shares", 0)),
            peak_close=float(raw.get("peak_close", 0.0)),
            take_profit_anchor_shares=int(raw.get("take_profit_anchor_shares", 0)),
            take_profit_fired=set(raw.get("take_profit_fired", [])),
            take_profit_cap_shares=(
                int(raw["take_profit_cap_shares"])
                if raw.get("take_profit_cap_shares") is not None else None
            ),
        )
        account.positions[code] = pos
    return account


def _rebalancer_to_checkpoint(rebalancer: Rebalancer) -> dict[str, Any]:
    return {
        "holding_since": {c: d.isoformat() for c, d in sorted(rebalancer.holding_since.items())},
        "last_buy_date": {c: d.isoformat() for c, d in sorted(rebalancer.last_buy_date.items())},
    }


def _rebalancer_from_checkpoint(rebalancer: Rebalancer, data: dict[str, Any]) -> None:
    rebalancer.holding_since = {
        c: date.fromisoformat(str(d)) for c, d in (data.get("holding_since") or {}).items()
    }
    rebalancer.last_buy_date = {
        c: date.fromisoformat(str(d)) for c, d in (data.get("last_buy_date") or {}).items()
    }


def _atomic_write_checkpoint(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(_checkpoint_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _read_checkpoint(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的 V2 checkpoint schema: {data.get('schema_version')!r}"
        )
    if data.get("kind") != "stockfu.v2.backtest.checkpoint":
        raise ValueError("checkpoint kind 不匹配")
    state = data.get("state") or {}
    expected_checksum = data.get("state_checksum")
    if not expected_checksum:
        raise ValueError("checkpoint 缺少 state_checksum，请从新版本重新生成")
    actual_checksum = fingerprint(
        _checkpoint_jsonable(state), prefix="v2.checkpoint.state")
    if actual_checksum != expected_checksum:
        raise ValueError("checkpoint state_checksum 校验失败，拒绝恢复")
    return data


def _verify_audit_file(path: str, expected_n: int, expected_checksum: str,
                       expected_offset: int) -> list[dict]:
    """校验 append-only audit artifact 的已提交前缀，返回前 expected_n 条记录。

    链式 checksum 口径与 _flush_audit 一致：fingerprint({"prev","line\\n"})。
    - expected_n==0：零行（链=""、offset=0），清掉任何陈旧内容，返回 []。
    - 文件缺失（expected_n>0）/ 行数不足 / 链不符 / offset 不符 → 硬失败
      （已提交前缀被删除、截断或篡改）。
    - 行数 > expected_n：未提交尾部（崩溃半截写），截断文件到前 expected_n 行。
    """
    p = Path(path)
    if expected_n <= 0:
        if p.exists():
            p.write_text("", encoding="utf-8")
        return []
    if not p.exists():
        raise ValueError(
            f"audit artifact 缺失（checkpoint 声明 {expected_n} 行）: {path}")
    parts = p.read_text(encoding="utf-8").split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) < expected_n:
        raise ValueError(
            f"audit artifact 行数不足：文件 {len(parts)} 行 < checkpoint 声明 "
            f"{expected_n} 行（已提交记录被删除或文件被截断）: {path}")
    running = ""
    offset = 0
    for ln in parts[:expected_n]:
        line = ln + "\n"
        running = fingerprint({"prev": running, "line": line}, prefix="v2.audit")
        offset += len(line.encode("utf-8"))
    if running != expected_checksum:
        raise ValueError(
            f"audit artifact 链式 checksum 校验失败（已提交前缀被篡改）: {path}")
    if offset != expected_offset:
        raise ValueError(
            f"audit artifact offset 校验失败：重算 {offset} != 声明 "
            f"{expected_offset}: {path}")
    if len(parts) > expected_n:
        p.write_text("".join(ln + "\n" for ln in parts[:expected_n]),
                     encoding="utf-8")
    return [json.loads(ln) for ln in parts[:expected_n]]


def _rebuild_audit_file(path: str, records: list[dict]) -> None:
    """把已校验记录整写到输出 audit artifact（截断已有内容）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    buf = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
           for row in records]
    p.write_text("".join(buf), encoding="utf-8")


def resolve_snapshot(*, provided: dict | None, resume_from: str | None,
                     snapshots_dir: str | None) -> dict:
    """解析数据快照 descriptor（阻塞③/§4.8.2 的单一入口）。

    - provided 非空：校验文件与内容一致后返回（--snapshot 或上层已解析）。
    - resume_from 非空：从来源 checkpoint manifest 恢复 descriptor（合法 resume
      不再要求调用方手动传快照）；文件缺失/路径失效则硬失败并提示 --snapshot。
    - 否则：新建快照（幂等去重）。

    绝不静默重建——身份绑定 snapshot_id，重建会换 ID 导致 resume 失配。
    """
    from stockfu.backtest.snapshot import create_data_snapshot, validate_snapshot
    if provided is not None:
        validate_snapshot(provided)
        return provided
    if resume_from is not None:
        checkpoint = _read_checkpoint(resume_from)
        snap = (checkpoint.get("manifest") or {}).get("data_snapshot")
        if not snap or not snap.get("snapshot_id"):
            raise ValueError(
                "来源 checkpoint 缺少 data_snapshot，无法恢复数据快照；"
                "请用 --snapshot 显式指定快照文件")
        try:
            validate_snapshot(snap)
        except ValueError as e:
            if "不存在" in str(e):
                raise ValueError(
                    f"来源 checkpoint 绑定的快照文件缺失或路径失效："
                    f"{snap.get('path')}\n请用 --snapshot 显式指定现有快照文件后重试"
                ) from e
            raise
        return snap
    return create_data_snapshot(snapshots_dir)


# ----------------------------------------------------------- 主入口


def run_v2_backtest(cfg: V2RunConfig) -> V2Result:
    # 运行入口强制绑定 raw callable：__post_init__ 只覆盖构造时点，构造后
    # 替换 cfg.raw_computers 的 callable（即使新函数仍返回原声明指纹）也必须在
    # 这里被拦截；同时让 checkpoint identity 始终使用真实函数绑定。
    actual_bindings = {m: fn_source_fingerprint(fn)
                       for m, fn in cfg.raw_computers.items()}
    if cfg.raw_computer_bindings and any(
            actual_bindings[m] != cfg.raw_computer_bindings.get(m)
            for m in actual_bindings):
        raise ValueError(
            "raw_computer_bindings 与实际 raw computer 函数不匹配（运行入口校验）："
            "请通过 v2_run 注册表绑定 raw 计算器，不要直接替换 callable")
    cfg.raw_computer_bindings = actual_bindings

    # canonical 预检前移到任何写盘/预载之前（§4.8.3/§4.13.3-2）：fail-closed，
    # 不生成快照、不预载。run_meta（git/deps）一次算好供 build_manifest 复用。
    run_meta = canonical_preflight(cfg.canonical)

    # 数据快照 descriptor 解析（阻塞③/§4.8.2）：provided 优先，resume 时从来源
    # 工件恢复，否则新建。
    cfg.snapshot = resolve_snapshot(provided=cfg.snapshot,
                                    resume_from=cfg.resume_from,
                                    snapshots_dir=cfg.snapshots_dir)

    # 整条取数（日历/预载/listing/universe/eval 循环）走快照只读引擎（阻塞①）。
    # 局部 import：便于测试 patch snap_mod.snapshot_engine（fake path 无法打开）。
    from stockfu.backtest.snapshot import snapshot_engine
    from stockfu.db import reset_read_engine, set_read_engine
    _read_token = set_read_engine(snapshot_engine(cfg.snapshot))
    try:
        return _run_v2_backtest_body(cfg, run_meta)
    finally:
        reset_read_engine(_read_token)


def _run_v2_backtest_body(cfg: V2RunConfig, run_meta: dict) -> V2Result:
    alpha = cfg.alpha
    portfolio = cfg.portfolio
    risk = cfg.risk
    profiles = cfg.profiles
    raw_computers = cfg.raw_computers
    codes = list(cfg.codes)
    bench = cfg.benchmark_code

    # profile_id -> raw_metric_id
    pid_to_metric = {pid: p.raw_metric_id for pid, p in profiles.items()}
    alpha_profile_ids = [f.profile_id for f in alpha.factors]

    # 同一 raw metric 在一个 run 内必须只有一个单位；历史状态/评分不能把
    # percent、ratio 等不同量纲混进同一分布。
    metric_units: dict[str, str] = {}
    for profile in profiles.values():
        old_unit = metric_units.get(profile.raw_metric_id)
        if old_unit is not None and old_unit != profile.raw_unit:
            raise ValueError(
                f"raw_metric_id={profile.raw_metric_id} 被不同 raw_unit 引用: "
                f"{old_unit!r} vs {profile.raw_unit!r}")
        metric_units[profile.raw_metric_id] = profile.raw_unit

    # 交易日历 + 预载(含 benchmark)
    dates_all = _trade_calendar_days(cfg.history_origin, cfg.eval_end)
    eval_dates = [d for d in dates_all if d >= cfg.eval_start]
    if len(eval_dates) < 5:
        raise ValueError(f"eval_dates 过少({len(eval_dates)})")
    if cfg.observation_count is not None:
        # 固定观察期:延长 eval_end 不改 formal_start(§9.4 prefix invariance)
        obs_count = min(cfg.observation_count, max(0, len(eval_dates) - 1))
    else:
        obs_count = math.ceil(len(eval_dates) * 0.20)
    formal_dates = eval_dates[obs_count:]
    formal_set = set(formal_dates)
    obs_set = set(eval_dates[:obs_count])

    pre_start = cfg.history_origin - timedelta(days=_PRELOAD_LOOKBACK_DAYS)
    risk_bench = getattr(risk.policy, "market_regime_code", None) or bench
    preload_codes = sorted({*codes, bench, risk_bench})
    sctx = _preload_market_range(preload_codes, pre_start, cfg.eval_end)
    div_index = _preload_dividend_events(codes, pre_start, cfg.eval_end)

    # 数据截断检测：交易日历可能预埋到未来，但行情只到库末日。
    # 请求终点超过库数据末日时截断到 data_end，不跑无行情日（否则会产生
    # 无观测的伪末日：equity 用 last_close 兜底记录、checkpoint 的
    # last_completed_date 超前），并在 manifest.data_coverage 强制披露。
    data_end = max(sctx.dates) if (sctx and sctx.dates) else None
    truncated = data_end is not None and data_end < cfg.eval_end
    effective_end = data_end if truncated else cfg.eval_end
    if truncated:
        print(
            f"  v2 警告: 请求终点 {cfg.eval_end.isoformat()} 超过库数据末日 "
            f"{data_end.isoformat()}，回测截断到 {effective_end.isoformat()}"
            f"（详见 manifest.data_coverage）", flush=True)
        dates_all = [d for d in dates_all if d <= effective_end]
        eval_dates = [d for d in eval_dates if d <= effective_end]
        if len(eval_dates) < 5:
            raise ValueError(
                f"数据截断后 eval_dates 过少({len(eval_dates)})")
        if cfg.observation_count is not None:
            obs_count = min(cfg.observation_count, max(0, len(eval_dates) - 1))
        else:
            obs_count = math.ceil(len(eval_dates) * 0.20)
        formal_dates = eval_dates[obs_count:]
        formal_set = set(formal_dates)
        obs_set = set(eval_dates[:obs_count])

    # 运行元数据已在 run_v2_backtest 入口算好（含 canonical 门禁），此处直接复用。
    listing, industry = _load_listing_and_industry(codes)
    uni_ctx = UniverseContext.load(codes, cfg.universe_rules)
    exec_rules = cfg.execution_rules
    # universe 静态摘要（含一次 status SQL）只在运行开始算一次；build_manifest
    # 动态拼 sizes，避免每次保存 checkpoint 都重查库。
    uni_static_summary = uni_ctx.summary()

    # 采样日集合(确定性,只由 sampling 规则决定)。额外读取一小段交易日历，
    # 让 eval_end 前的最后一个交易日能依据真实后继日期判断周/月边界；不能把
    # “本次运行最后一天”自动当成期末，否则 checkpoint 延长会改变历史状态。
    sampling_calendar = _trade_calendar_days(
        cfg.history_origin, effective_end + timedelta(days=31))
    sample_dates: dict[tuple[str, str], set[date]] = {}
    for p in profiles.values():
        for comp, spec in p.history_specs.items():
            key = (p.raw_metric_id, comp, spec.sampling)
            if key not in sample_dates:
                sample_dates[key] = {
                    d for d in compute_sample_dates(sampling_calendar, spec.sampling)
                    if d <= effective_end
                }

    history = HistoryState()
    scorers = {pid: FactorScorer(profiles[pid]) for pid in alpha_profile_ids}
    aggregator = AlphaAggregator(alpha)

    acct = VirtualAccount(cfg.initial_cash)
    equity_curve: list[dict] = []
    trades: list[dict] = []
    pending_orders: dict[str, float] = {}      # code -> target_weight(昨日信号,今日执行)
    ideal_target: dict[str, float] = {}        # portfolio policy 的理想目标
    last_target: dict[str, float] = {}         # 最近一次 risk-adjusted target
    previous_risk_target: dict[str, float] = {}
    rebalancer = Rebalancer(portfolio.policy)  # 换手抑制(偏离阈值+冷却+最小持仓;默认0=关)
    last_close: dict[str, float] = {}          # 停牌日估值兜底(沿用上一交易日 close)
    prev_eval_date: date | None = None
    universe_sizes: list[int] = []             # 每个交易日实际参与截面的数量
    benchmark_closes: list[float] = []          # V1-compatible regime/vol target state
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    credit_div = cfg.credit_dividends
    if credit_div:
        cash_dividends = _preload_cash_dividends(codes, pre_start, cfg.eval_end)
        stock_dividends = _preload_stock_dividends(codes, pre_start, cfg.eval_end)
    else:
        cash_dividends = {}
        stock_dividends = {}

    # raw_value 缺失统计(按观察/formal 期分桶;预热期不计入诊断)
    raw_missing: dict[str, dict[str, int]] = {
        m: {"obs": 0, "formal": 0} for m in raw_computers}
    raw_total: dict[str, dict[str, int]] = {
        m: {"obs": 0, "formal": 0} for m in raw_computers}

    # §15 分数诊断收集(观察/正式分桶;全部进 checkpoint 支持续跑)
    score_samples: dict[str, list[float]] = {"obs": [], "formal": []}
    score_coverage_sum: dict[str, float] = {"obs": 0.0, "formal": 0.0}
    score_coverage_n: dict[str, int] = {"obs": 0, "formal": 0}
    factor_clamp: dict[str, int] = {"obs": 0, "formal": 0}      # 因子分钳制在 0/100 的观测数
    factor_total: dict[str, int] = {"obs": 0, "formal": 0}      # 因子分观测总数
    maturity_counts: dict[str, dict[str, int]] = {
        "obs": {"immature": 0, "partial": 0, "mature": 0},
        "formal": {"immature": 0, "partial": 0, "mature": 0},
    }
    daily_unique: dict[str, list[list[int]]] = {"obs": [], "formal": []}  # 每期 [横截面唯一值数, 总数]
    daily_audit: list[dict] = []     # §14 factor_score_audit 逐日摘要
    # daily_audit 的 append-only artifact（§4.8.4）：checkpoint 只存摘要，
    # 完整审计行落在 <checkpoint>.audit.jsonl，避免 73MiB 工件每日整写。
    audit_path: str | None = None
    audit_offset = 0
    audit_checksum = ""
    audit_written = 0
    first_mature_date: date | None = None

    checkpoint_target = cfg.checkpoint_path or cfg.resume_from
    if checkpoint_target:
        audit_path = str(Path(checkpoint_target)) + ".audit.jsonl"
    resumed_from = cfg.resume_from is not None
    resume_last_completed: date | None = None
    resume_source: dict | None = None   # §14：状态 checkpoint 来源（可定位续跑链）
    if cfg.resume_from:
        checkpoint = _read_checkpoint(cfg.resume_from)
        expected_identity = cfg.checkpoint_identity()
        actual_identity = checkpoint.get("config_fingerprint")
        if actual_identity != expected_identity:
            raise ValueError(
                "checkpoint 配置指纹不匹配；只允许使用相同 alpha/profile/portfolio/"
                "risk/宇宙/费用口径，并在固定 observation_count 下延长 eval_end"
            )
        source_manifest = checkpoint.get("manifest") or {}
        if cfg.canonical:
            # 锁定 canonical 恢复链（§4.13.3-3）：来源必须是同一 clean commit 的
            # canonical 工件，否则正式工件会被 dirty/旧提交/未知依赖“洗白”。
            src_rep = source_manifest.get("reproducibility") or {}
            cur_rep = run_meta["reproducibility"]
            if (src_rep.get("status") != "canonical"
                    or src_rep.get("git_dirty") is not False):
                raise ValueError(
                    "canonical 续跑要求来源 checkpoint 为 canonical 且 git 干净；"
                    "禁止把 non-canonical/dirty 工件提升为 canonical")
            if src_rep.get("git_commit") != cur_rep.get("git_commit"):
                raise ValueError(
                    "canonical 续跑要求来源与当前处于同一 git commit："
                    f"来源 {src_rep.get('git_commit')} vs "
                    f"当前 {cur_rep.get('git_commit')}")
            if (not cur_rep.get("deps_hash")
                    or src_rep.get("deps_hash") != cur_rep.get("deps_hash")):
                raise ValueError(
                    "canonical 续跑要求依赖锁文件与来源一致（deps_hash 不匹配）")
            src_env = src_rep.get("env_identity")
            cur_env = cur_rep.get("env_identity")
            if (not isinstance(src_env, dict)
                    or not isinstance(cur_env, dict)
                    or src_env != cur_env):
                raise ValueError(
                    "canonical 续跑要求来源与当前运行环境身份完全一致"
                    "（Python/平台/SQLite/安装包集合发生变化）")
        resume_source = {
            "path": cfg.resume_from,
            "source_run_id": source_manifest.get("run_id"),
            "source_state_checksum": checkpoint.get("state_checksum"),
            "source_last_completed": (
                (checkpoint.get("state") or {}).get("last_completed_date")),
        }
        raw_state = checkpoint.get("state") or {}
        completed = raw_state.get("last_completed_date")
        if not completed:
            raise ValueError("checkpoint 缺少 last_completed_date")
        resume_last_completed = date.fromisoformat(str(completed))
        if resume_last_completed > cfg.eval_end:
            raise ValueError("checkpoint 已超过本次 eval_end，不能倒退续跑")
        if resume_last_completed not in dates_all:
            raise ValueError(f"checkpoint 日期不在本次交易日历中: {resume_last_completed}")
        history = HistoryState.from_checkpoint(raw_state.get("history") or {})
        acct = _account_from_checkpoint(raw_state["account"])
        _rebalancer_from_checkpoint(rebalancer, raw_state.get("rebalancer") or {})
        equity_curve = [
            {**row, "date": date.fromisoformat(str(row["date"]))}
            for row in raw_state.get("equity_curve", [])
        ]
        trades = list(raw_state.get("trades", []))
        pending_orders = {c: float(w) for c, w in (raw_state.get("pending_orders") or {}).items()}
        last_target = {c: float(w) for c, w in (raw_state.get("last_target") or {}).items()}
        ideal_target = {c: float(w) for c, w in (
            raw_state.get("ideal_target") or raw_state.get("last_target") or {}
        ).items()}
        previous_risk_target = dict(last_target)
        last_close = {c: float(v) for c, v in (raw_state.get("last_close") or {}).items()}
        universe_sizes = [int(v) for v in raw_state.get("universe_sizes", [])]
        benchmark_closes = [float(v) for v in raw_state.get(
            "risk_benchmark_closes",
            raw_state.get("benchmark_closes", []),
        )]
        prev_eval_date = (
            date.fromisoformat(str(raw_state["prev_eval_date"]))
            if raw_state.get("prev_eval_date") else None
        )
        first_trade_date = (
            date.fromisoformat(str(raw_state["first_trade_date"]))
            if raw_state.get("first_trade_date") else None
        )
        last_trade_date = (
            date.fromisoformat(str(raw_state["last_trade_date"]))
            if raw_state.get("last_trade_date") else None
        )
        for metric in raw_missing:
            for period in ("obs", "formal"):
                raw_missing[metric][period] = int(
                    (raw_state.get("raw_missing") or {}).get(metric, {}).get(period, 0)
                )
                raw_total[metric][period] = int(
                    (raw_state.get("raw_total") or {}).get(metric, {}).get(period, 0)
                )
        for period in ("obs", "formal"):
            score_samples[period] = [
                float(v) for v in (raw_state.get("score_samples") or {}).get(period, [])]
            score_coverage_sum[period] = float(
                (raw_state.get("score_coverage_sum") or {}).get(period, 0.0))
            score_coverage_n[period] = int(
                (raw_state.get("score_coverage_n") or {}).get(period, 0))
            factor_clamp[period] = int(
                (raw_state.get("factor_clamp") or {}).get(period, 0))
            factor_total[period] = int(
                (raw_state.get("factor_total") or {}).get(period, 0))
            daily_unique[period] = [
                [int(a), int(b)] for a, b in
                (raw_state.get("daily_unique") or {}).get(period, [])]
        restored_maturity = raw_state.get("maturity_counts") or {}
        for period in ("obs", "formal"):
            maturity_counts[period] = {
                k: int((restored_maturity.get(period) or {}).get(k, 0))
                for k in maturity_counts[period]}
        audit_summary = raw_state.get("audit")
        if audit_summary is None:
            raise ValueError(
                "来源 checkpoint 缺少 audit 摘要（不一致/遗留工件），拒绝恢复")
        # resume：显式从来源 audit artifact（<resume_from>.audit.jsonl）读+校验，
        # 重算链式 checksum 与 offset，拒绝缺失/篡改/截断，丢弃未提交尾部。
        source_audit_path = str(Path(cfg.resume_from)) + ".audit.jsonl"
        verified = _verify_audit_file(
            source_audit_path,
            int(audit_summary.get("n_days") or 0),
            str(audit_summary.get("checksum") or ""),
            int(audit_summary.get("offset") or 0))
        daily_audit = verified
        audit_written = len(verified)
        audit_checksum = str(audit_summary.get("checksum") or "")
        if audit_path == source_audit_path:
            # 输出即来源：续 append（_verify_audit_file 已截断尾部）
            audit_offset = os.path.getsize(source_audit_path)
        else:
            # 输出 ≠ 来源：把已校验记录重建写入新输出文件
            _rebuild_audit_file(audit_path, verified)
            audit_offset = os.path.getsize(audit_path)
        fm = raw_state.get("first_mature_date")
        first_mature_date = date.fromisoformat(str(fm)) if fm else None
        restore_risk = getattr(risk, "restore_state", None)
        if callable(restore_risk):
            restore_risk(raw_state.get("risk_state") or {})
        # 恢复后同步挂单与 last_target（防旧版本 checkpoint 残留已撤销目标的
        # 买入挂单）；此后每次决策日还会再同步一次。
        for code in list(pending_orders):
            if abs(pending_orders[code]
                   - float(last_target.get(code, 0.0))) > 1e-12:
                del pending_orders[code]
    elif audit_path:
        # 新运行（非 resume）：清空输出 audit artifact，防旧文件残留污染续写。
        Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
        Path(audit_path).write_text("", encoding="utf-8")

    # ---- §4.8.1 两阶段 checkpoint：partial（可恢复）→ finalized（含输出校验和）----
    # 三个职责单一的函数：state / payload / persist。

    def _audit_state() -> dict | None:
        if not audit_path:
            return None
        # 不含 path：恢复时由 resume_from 推导（<checkpoint>.audit.jsonl），
        # 避免同内容不同路径的工件 state_checksum 不同。
        return {"offset": audit_offset,
                "n_days": audit_written, "checksum": audit_checksum}

    def build_checkpoint_state(last_completed: date) -> dict:
        risk_state_fn = getattr(risk, "checkpoint_state", None)
        risk_state = risk_state_fn() if callable(risk_state_fn) else {}
        return {
            "last_completed_date": last_completed.isoformat(),
            "history": history.to_checkpoint(),
            "account": _account_to_checkpoint(acct),
            "pending_orders": pending_orders,
            "ideal_target": ideal_target,
            "last_target": last_target,
            "rebalancer": _rebalancer_to_checkpoint(rebalancer),
            "risk_state": risk_state,
            "last_close": last_close,
            "prev_eval_date": prev_eval_date.isoformat() if prev_eval_date else None,
            "first_trade_date": first_trade_date.isoformat() if first_trade_date else None,
            "last_trade_date": last_trade_date.isoformat() if last_trade_date else None,
            "equity_curve": equity_curve,
            "trades": trades,
            "universe_sizes": universe_sizes,
            "benchmark_closes": benchmark_closes,
            "risk_benchmark_closes": benchmark_closes,
            "raw_missing": raw_missing,
            "raw_total": raw_total,
            "score_samples": score_samples,
            "score_coverage_sum": score_coverage_sum,
            "score_coverage_n": score_coverage_n,
            "factor_clamp": factor_clamp,
            "factor_total": factor_total,
            "maturity_counts": maturity_counts,
            "daily_unique": daily_unique,
            # daily_audit 为 append-only artifact（§4.8.4）：checkpoint 只存摘要，
            # 完整审计在 <checkpoint>.audit.jsonl，避免 73MiB 工件每日整写。
            "audit": _audit_state(),
            "first_mature_date": (
                first_mature_date.isoformat() if first_mature_date else None),
        }

    def build_checkpoint_payload(state: dict, manifest: dict) -> dict:
        return {
            "kind": "stockfu.v2.backtest.checkpoint",
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "config_fingerprint": cfg.checkpoint_identity(),
            "manifest": manifest,
            "state": state,
            "state_checksum": fingerprint(
                _checkpoint_jsonable(state), prefix="v2.checkpoint.state"),
        }

    def persist_checkpoint(path: str, state: dict, manifest: dict) -> None:
        _atomic_write_checkpoint(path, build_checkpoint_payload(state, manifest))

    def build_manifest(last_completed: date | None, *, finalized: bool,
                       state_checksum: str | None = None) -> dict:
        """完整 run manifest（设计 §14）：配置指纹/口径 + 运行结果字段。

        - partial（finalized=False）：不做完整诊断排序（§4.8.4），score_diagnostics
          只保存样本数与增量计数；output_checksum=None。
        - finalized：计算完整 score_diagnostics、各组件 checksum 与总
          output_checksum，再最后算 run_id。最终磁盘工件与 V2Result.manifest
          复用同一对象，不得分别构造。
        - 运行中不变的元数据（git commit、数据快照、universe 静态摘要、依赖锁）
          在 run_meta/uni_static_summary/cfg.snapshot 缓存，不重复查库。
        """
        uni = {**uni_static_summary,
               "avg_size": round(sum(universe_sizes) / len(universe_sizes), 1)
               if universe_sizes else None,
               "min_size": min(universe_sizes) if universe_sizes else None,
               "max_size": max(universe_sizes) if universe_sizes else None}
        if finalized:
            diag = _score_diagnostics(
                score_samples, formal_dates, first_mature_date,
                score_coverage_sum, score_coverage_n,
                factor_clamp, factor_total, maturity_counts, daily_unique)
            score_diag_field: dict | None = diag
        else:
            score_diag_field = {
                "status": "partial",
                "obs_samples": len(score_samples["obs"]),
                "formal_samples": len(score_samples["formal"]),
                "obs_days": len(daily_unique["obs"]),
                "formal_days": len(daily_unique["formal"]),
            }
        m = cfg.manifest(
            observation_count=obs_count,
            formal_start=formal_dates[0].isoformat() if formal_dates else None,
            first_trade_date=first_trade_date.isoformat() if first_trade_date else None,
            last_trade_date=last_trade_date.isoformat() if last_trade_date else None,
            n_trades=len(trades),
            trades_checksum=fingerprint(trades, prefix="v2.trades"),
            universe=uni,
            risk_metrics=(risk.metrics() if hasattr(risk, "metrics") else {}),
            data_coverage={
                "requested_eval_end": cfg.eval_end.isoformat(),
                "effective_eval_end": effective_end.isoformat(),
                "data_end": data_end.isoformat() if data_end else None,
                "truncated": truncated,
            },
            data_snapshot=cfg.snapshot,
            reproducibility=run_meta["reproducibility"],
            git=run_meta["git"],
            checkpoint={
                "schema_version": _CHECKPOINT_SCHEMA_VERSION,
                "enabled": checkpoint_target is not None,
                "finalized": finalized,
                "resumed": resumed_from,
                "resume_source": resume_source,
                "last_completed_date": (
                    last_completed.isoformat() if last_completed else None),
                "risk_benchmark_code": risk_bench,
            },
            score_diagnostics=score_diag_field,
            daily_audit={"n_days": len(daily_audit)},
        )
        if finalized:
            # 组件 checksum（§4.8.1：不复制完整数组，只存摘要）。
            component_checksums = {
                "state": state_checksum,
                "trades": m["trades_checksum"],
                "formal_equity": fingerprint(formal_equity, prefix="v2.equity"),
                "benchmark": fingerprint(formal_bench, prefix="v2.bench"),
                "metrics": fingerprint(metrics, prefix="v2.metrics"),
                "observation_summary": fingerprint(obs_summary, prefix="v2.obs"),
                "formal_summary": fingerprint(formal_summary, prefix="v2.formal"),
                "score_diagnostics": fingerprint(diag, prefix="v2.diag"),
                "data_snapshot": fingerprint(
                    (cfg.snapshot or {}).get("snapshot_id"), prefix="v2.snapshot"),
            }
            m["component_checksums"] = component_checksums
            m["output_checksum"] = fingerprint(
                component_checksums, prefix="v2.output")
        m["run_id"] = fingerprint(m, prefix="v2.run")
        return m

    def _flush_audit() -> None:
        """把新增审计行 append 到 artifact，维护链式 checksum 与 offset。"""
        nonlocal audit_offset, audit_checksum, audit_written
        if not audit_path or audit_written >= len(daily_audit):
            return
        rows = daily_audit[audit_written:]
        with open(audit_path, "a", encoding="utf-8") as f:
            for row in rows:
                line = json.dumps(
                    row, ensure_ascii=False, separators=(",", ":")) + "\n"
                f.write(line)
                audit_checksum = fingerprint(
                    {"prev": audit_checksum, "line": line}, prefix="v2.audit")
        audit_offset = os.path.getsize(audit_path)
        audit_written = len(daily_audit)

    def save_checkpoint(last_completed_date: date) -> None:
        if not checkpoint_target:
            return
        _flush_audit()
        state = build_checkpoint_state(last_completed_date)
        manifest = build_manifest(
            last_completed_date, finalized=False)
        persist_checkpoint(checkpoint_target, state, manifest)

    # 实际完成日：以 resume 起点为初值，主循环每推进一日更新一次；
    # 循环结束后 finalize 用它（覆盖正常/截断/空续跑各路径）。
    actual_last_completed: date | None = resume_last_completed

    with _backtest_series_ctx(sctx, div_index):
        for day_index, t in enumerate(dates_all):
            if resume_last_completed is not None and t <= resume_last_completed:
                continue
            if equity_curve and len(equity_curve) % 50 == 0:
                print(f"  v2 progress: {len(equity_curve)}/{len(dates_all)} days  as_of={t}",
                      flush=True)
            close_q, open_q, day_bars = _get_day_market(
                preload_codes, t, sctx, valuation_basis=cfg.valuation_basis)
            bench_px = close_q.get(risk_bench)
            if bench_px is not None and bench_px > 0:
                benchmark_closes.append(float(bench_px))

            # ---- 1. 执行 t-1 订单(T+1 开盘价;raw 判涨跌停;先卖后买 + 现金约束)----
            if pending_orders:
                open_prices = dict(open_q)
                # 交易日缺少持仓的开盘行时，沿用上一可用估值；不能让停牌股在
                # 计算当前权重时变成 0，从而把其他仓位错误放大。
                execution_valuation_prices = {**last_close, **open_prices}
                sells, buys = [], []
                remaining_orders: dict[str, float] = {}
                for code in sorted(pending_orders):
                    tw = pending_orders[code]
                    if code not in open_prices and code in close_q:
                        open_prices[code] = close_q[code]
                        execution_valuation_prices[code] = close_q[code]
                    px, source = _get_trade_price(code, open_prices, close_q)
                    if px <= 0:
                        if exec_rules.on_unfillable == "defer":
                            remaining_orders[code] = tw
                        continue
                    cur_w = acct.weight(code, execution_valuation_prices)
                    act = _classify(cur_w, tw)
                    if act == "hold":
                        continue
                    bar = day_bars.get(code, {})
                    side = "sell" if act in ("sell", "reduce") else "buy"
                    fill = check_fill(
                        side, px,
                        pct_chg=bar.get("pct_chg"),
                        open_=bar.get("open_raw") or bar.get("open"),
                        high=bar.get("high_raw") or bar.get("high"),
                        low=bar.get("low_raw") or bar.get("low"),
                        close=bar.get("close_raw") or bar.get("close"),
                        board=uni_ctx.board(code),
                        is_st=bool(bar.get("is_st")),
                        trade_status=int(bar.get("trade_status", 1)),
                        pre_close=infer_pre_close(
                            bar.get("close_raw") or bar.get("close"), bar.get("pct_chg")),
                        rules=exec_rules,
                    )
                    if not fill.ok:
                        if exec_rules.on_unfillable == "defer":
                            remaining_orders[code] = tw
                        continue
                    if side == "sell":
                        sells.append((code, tw, fill.price, source))
                    else:
                        buys.append((code, tw, fill.price, source))
                # 先卖(释放现金)
                for code, tw, px, source in sells:
                    shares_before = acct.positions[code].shares if code in acct.positions else 0
                    tr = acct.apply_action(
                        code, "reduce", tw, px, execution_valuation_prices, as_of=t)
                    if tr:
                        tr.update(date=t.isoformat(), status="filled",
                                  price_source=source, reason="v2_open_exec")
                        trades.append(tr)
                        first_trade_date = first_trade_date or t
                        last_trade_date = t
                        if (acct.positions[code].shares if code in acct.positions else 0) == 0:
                            rebalancer.record_close(code)
                # 买单等比缩放到现金
                scaled, _safety, _constrained = scale_buys_to_cash(
                    acct, [(c, tw, px) for c, tw, px, _ in buys],
                    execution_valuation_prices,
                    commission_rate=COMMISSION_RATE, transfer_fee_rate=TRANSFER_FEE_RATE,
                    min_commission=MIN_COMMISSION)
                scaled_by = {c: (stw, spx) for c, stw, spx in scaled}
                for code, tw, px, source in buys:
                    stw, spx = scaled_by.get(code, (tw, px))
                    shares_before = acct.positions[code].shares if code in acct.positions else 0
                    tr = acct.apply_action(
                        code, "buy", stw, spx, execution_valuation_prices, as_of=t)
                    if tr:
                        tr.update(date=t.isoformat(), status="filled",
                                  price_source=source, reason="v2_open_exec")
                        trades.append(tr)
                        first_trade_date = first_trade_date or t
                        last_trade_date = t
                        rebalancer.record_buy(code, t, was_new=shares_before == 0)
                pending_orders = remaining_orders

            # 分红结算(qfq 下 credit_dividends=False → 跳过)
            if credit_div:
                trades.extend(settle_dividends(
                    acct, t, cash_dividends, stock_dividends, credit_div))

            # ---- 2. universe + 3. raw ----
            day_flags: dict[str, DayFlags] = {}
            for c in codes:
                bar = day_bars.get(c)
                if bar:
                    day_flags[c] = DayFlags(
                        is_st=bool(bar.get("is_st")),
                        trade_status=int(bar.get("trade_status", 1)),
                        has_row=True, amount=bar.get("amount"))
                else:
                    day_flags[c] = DayFlags(has_row=False)
            eligible = {c for c in uni_ctx.eligible_on(t, day_flags) if c in close_q}
            universe_sizes.append(len(eligible))

            # raw 分期属:obs/formal 才计入 missing_rate 诊断,预热期(None)不计
            period = "obs" if t in obs_set else ("formal" if t in formal_set else None)
            raw_by_metric: dict[str, dict[str, RawFactorObservation]] = {}
            for metric, computer in raw_computers.items():
                m = {}
                params = cfg.raw_params.get(metric, {})
                for c in sorted(eligible):
                    obs = computer(c, t, **params) if params else computer(c, t)
                    _validate_raw_observation(
                        obs, metric, t, metric_units[metric], c,
                        cfg.raw_fingerprints.get(metric, ""))
                    m[c] = obs
                    if period is not None:
                        raw_total[metric][period] += 1
                        if not obs.valid:
                            raw_missing[metric][period] += 1
                raw_by_metric[metric] = m

            # ---- 4. 评分(仅 eval 期;读 cutoff<t 的历史)----
            is_eval = t in obs_set or t in formal_set
            in_obs = t in obs_set
            strategy_scores = {}
            if is_eval:
                cutoff = history.cutoff          # < t(上一交易日 update 后的值)
                for sc in scorers.values():
                    sc.new_day()
                for c in sorted(eligible):
                    fs = {}
                    for pid in alpha_profile_ids:
                        metric = pid_to_metric[pid]
                        fs[pid] = scorers[pid].score(
                            raw_by_metric[metric][c], history,
                            industry.get(c), cfg.market_scope, cutoff)
                    strategy_scores[c] = aggregator.aggregate(
                        c, t, fs, reference_cutoff=cutoff,
                        universe_status="in_universe", observation=in_obs)

                # ---- §15 分数诊断收集(观察/正式分桶;进 checkpoint 支持续跑) ----
                # 注意：必须在逐票评分循环之外，按日收集整个横截面一次。
                period_key = "obs" if in_obs else "formal"
                scores_now = [so.strategy_score for so in strategy_scores.values()]
                daily_unique[period_key].append(
                    [len(set(scores_now)), len(scores_now)])
                for c, so in strategy_scores.items():
                    score_samples[period_key].append(so.strategy_score)
                    score_coverage_sum[period_key] += so.effective_coverage
                    score_coverage_n[period_key] += 1
                    for fs in so.factor_scores.values():
                        factor_total[period_key] += 1
                        if fs.score <= 0.0 or fs.score >= 100.0:
                            factor_clamp[period_key] += 1
                        maturity_counts[period_key][fs.maturity.value] += 1
                # §14 factor_score_audit 逐日摘要：日期/期 + 每票 strategy score、
                # factor score/maturity/coverage、raw 值（进 checkpoint 供 prefix
                # 比较与审计，round 控体积）。
                daily_audit.append({
                    "date": t.isoformat(),
                    "period": period_key,
                    "strategy": {c: round(float(so.strategy_score), 4)
                                 for c, so in sorted(strategy_scores.items())},
                    "factors": {
                        c: {pid: [round(float(fs.score), 4), fs.maturity.value,
                                   round(float(fs.evidence_coverage), 4)]
                            for pid, fs in sorted(so.factor_scores.items())}
                        for c, so in sorted(strategy_scores.items())},
                    "raw": {
                        m: {c: (None if obs.raw_value is None
                                else round(float(obs.raw_value), 6))
                            for c, obs in sorted(raw_by_metric.get(m, {}).items())}
                        for m in sorted(raw_by_metric)},
                })
                if not in_obs and first_mature_date is None:
                    for so in strategy_scores.values():
                        if so.score_status == ScoreStatus.TRADABLE and all(
                                fs.maturity == Maturity.MATURE
                                for fs in so.factor_scores.values()):
                            first_mature_date = t
                            break

                # ---- 组合 + 风险(formal;组合按 policy 调仓,风险可日级触发)----
                if not in_obs:
                    scheduled_rebalance = portfolio.is_rebalance_day(t, prev_eval_date)
                    if scheduled_rebalance:
                        ctx = DayContext(
                            price={c: (day_bars.get(c, {}).get("close_raw")
                                       or day_bars.get(c, {}).get("close") or 0.0) for c in eligible},
                            amount_20d={c: _amount_20d(sctx, c, t) for c in eligible},
                            listing_date=listing, is_st={c: day_flags[c].is_st for c in eligible},
                            industry=industry,
                        )
                        ideal_target = portfolio.select_target(strategy_scores, ctx, t)
                    risk_prices = {**last_close, **close_q}
                    risk_target = risk.apply(
                        ideal_target, acct, risk_prices, t,
                        execution_prices={
                            # 风控盈亏必须与 VirtualAccount.avg_cost/估值使用同一
                            # valuation_basis；默认 qfq，不能拿 raw close 比较 qfq 成本。
                            c: risk_prices.get(c, 0.0)
                            for c in acct.positions
                        },
                        benchmark_closes=benchmark_closes,
                    )
                    risk_changed = _targets_differ(risk_target, previous_risk_target)
                    last_target = risk_target
                    # 普通组合目标只在 scheduled rebalance 日纠偏；风险目标发生变化
                    # 或风险仍在主动压敞口时，允许日级订单维持风险约束。
                    should_decide = (
                        scheduled_rebalance
                        or risk_changed
                        or bool(getattr(risk, "last_adjusted", False))
                    )
                # 生成 t+1 待执行订单(rebalancer 按偏离阈值+冷却+最小持仓[软锁]筛选)
                if not in_obs:
                    if should_decide:
                        held = {c for c, p in acct.positions.items() if p.shares > 0}
                        risk_prices = {**last_close, **close_q}
                        cur_w = {c: acct.weight(c, risk_prices)
                                 for c in held | set(last_target)}
                        pnl_pct = {
                            c: risk_prices.get(c, 0.0) / acct.positions[c].avg_cost - 1.0
                            for c in held
                            if acct.positions[c].avg_cost > 0 and risk_prices.get(c, 0.0) > 0
                        }
                        new_orders = rebalancer.decide(
                            last_target, cur_w, held, t, pnl_pct,
                            risk_exit_codes=getattr(risk, "forced_exit_codes", set()),
                        )
                        # 挂单必须与最新 risk-adjusted target 一致：目标已撤销
                        # (→0/消失)的买入挂单立即取消，否则停牌解除后会买入一个
                        # 已被撤销的目标；卖出挂单(target=0 且目标仍为 0)保留；
                        # risk 目标变化的旧挂单作废，由本次 decide 按新目标重下。
                        for code in list(pending_orders):
                            if abs(pending_orders[code]
                                   - float(last_target.get(code, 0.0))) > 1e-12:
                                del pending_orders[code]
                        # 若前一日订单因停牌/涨跌停顺延，新目标只覆盖同一代码，
                        # 其他未成交订单继续保留。
                        pending_orders.update(new_orders)
                    previous_risk_target = dict(last_target)
                    # 观察期不参与换仓日历；否则 formal 首日如果与观察期
                    # 最后一天同月/同周，会被误判为非 rebalance 日。
                    prev_eval_date = t

            # ---- 5. 日末:评分完成后追加 t 日观测到历史 ----
            sample_flags: dict[str, dict[str, bool]] = {}
            metric_values: dict[str, dict[str, float | None]] = {}
            for p in profiles.values():
                m = p.raw_metric_id
                if m not in metric_values:
                    metric_values[m] = {
                    c: obs.raw_value for c, obs in raw_by_metric.get(m, {}).items()}
                    flags = {}
                    for comp, spec in p.history_specs.items():
                        flags[_COMP_SHORT[comp]] = t in sample_dates[(m, comp, spec.sampling)]
                    sample_flags[m] = flags
            history.update(t, metric_values, industry, cfg.market_scope, sample_flags)

            # ---- equity 记录(qfq 估值;停牌日沿用 last_close,避免持仓估值跳 0)----
            last_close.update({c: v for c, v in close_q.items() if v > 0})
            eq = acct.equity({**last_close, **close_q})
            equity_curve.append({"date": t, "equity": eq, "cash": acct.cash,
                                 "n_positions": sum(1 for p in acct.positions.values() if p.shares > 0),
                                 "in_obs": in_obs, "is_formal": t in formal_set})
            actual_last_completed = t
            if checkpoint_target and (
                (day_index + 1) % cfg.checkpoint_every == 0 or t == dates_all[-1]
            ):
                save_checkpoint(t)

    # ---- 绩效(formal 段;§15:净值从 formal 起归一)----
    formal_equity = [p for p in equity_curve if p["is_formal"]]
    formal_bench = _build_benchmark_curve(sctx, bench, [p["date"] for p in formal_equity])

    days = len(formal_equity)
    metrics = _metrics(formal_equity, formal_bench, cfg.initial_cash, days) if formal_equity else {}

    obs_summary = _raw_summary(
        eval_dates[:obs_count],
        {m: raw_missing[m]["obs"] for m in raw_missing},
        {m: raw_total[m]["obs"] for m in raw_total}, "observation")
    formal_summary = _raw_summary(
        formal_dates,
        {m: raw_missing[m]["formal"] for m in raw_missing},
        {m: raw_total[m]["formal"] for m in raw_total}, "formal")

    # ---- §4.8.1 finalize：计算最终输出并原子覆盖 checkpoint ----
    # 运行期快照可能被另一进程原地修改/替换（§4.13.3-4）：finalize 写盘前必须
    # 再验一次内容 SHA，不一致硬失败且不得留下 finalized=True 工件。
    from stockfu.backtest.snapshot import validate_snapshot as _finalize_validate
    _finalize_validate(cfg.snapshot)
    final_completed = actual_last_completed or (dates_all[-1] if dates_all else None)
    if checkpoint_target:
        _flush_audit()
        final_state = build_checkpoint_state(final_completed)
        final_state_checksum = fingerprint(
            _checkpoint_jsonable(final_state), prefix="v2.checkpoint.state")
        manifest = build_manifest(
            final_completed, finalized=True, state_checksum=final_state_checksum)
        persist_checkpoint(checkpoint_target, final_state, manifest)
    else:
        manifest = build_manifest(
            final_completed, finalized=True, state_checksum=None)

    return V2Result(
        metrics=metrics, equity_curve=equity_curve,
        formal_equity_curve=formal_equity, benchmark=formal_bench, trades=trades,
        manifest=manifest, history_checkpoint=history.to_checkpoint(),
        observation_summary=obs_summary, formal_summary=formal_summary,
        first_trade_date=first_trade_date, last_trade_date=last_trade_date,
        score_diagnostics=manifest.get("score_diagnostics", {}),
        daily_audit=daily_audit,
    )


def _build_benchmark_curve(sctx, bench_code: str, formal_dates: list[date]) -> list[dict]:
    """formal 段基准净值(归一为 1)。从列式预载取 benchmark qfq close。"""
    if sctx is None or not formal_dates:
        return []
    cols = sctx.series.get(bench_code)
    if cols is None:
        return []
    closes = cols.get("c") or cols.get("c_raw") or cols.get("close")
    if closes is None:
        return []
    out: list[dict] = []
    base = None
    for d in formal_dates:
        di = sctx.date_idx.get(d)
        if di is None:
            continue
        v = closes[di]
        if math.isnan(v):
            continue
        if base is None:
            base = v
        out.append({"date": d, "equity": v / base})
    return out


def _raw_summary(period_dates: list[date],
                 raw_missing: dict[str, int], raw_total: dict[str, int],
                 label: str) -> dict:
    return {
        "label": label,
        "n_days": len(period_dates),
        "raw_total": dict(raw_total),
        "missing_count": dict(raw_missing),
        "missing_rate": {m: (round(raw_missing[m] / raw_total[m], 4) if raw_total[m] else None)
                         for m in raw_missing},
    }


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    """线性插值分位（与设计 §15 的 score P01/P05/P50/P95/P99 口径一致）。"""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return round(float(sorted_vals[lo]), 4)
    return round(float(sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)), 4)


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return round(mean, 4), round(math.sqrt(var), 4)


def _score_diagnostics(score_samples: dict[str, list[float]],
                       formal_dates: list[date],
                       first_mature_date: date | None,
                       coverage_sum: dict[str, float], coverage_n: dict[str, int],
                       factor_clamp: dict[str, int], factor_total: dict[str, int],
                       maturity_counts: dict[str, dict[str, int]],
                       daily_unique: dict[str, list[list[int]]]) -> dict:
    """§15 分数/成熟度/分位诊断：checkpoint 收集的聚合输入 → 可读报告。

    - score P01/P05/P50/P95/P99、0/100 饱和比例、均值（formal 横截面样本）
    - unique_ratio：每日横截面唯一值比例的均值（§15「横截面唯一值比例」），
      不是全期扁平去重率
    - score_coverage / factor_clamp_rate / factor_maturity 按观察/正式期分桶
    - maturity_delay_days（formal 首日到首次成熟的天数）
    - observation 分数均值/标准差（§15 观察期只报告分数稳定性，无收益指标）
    """
    formal_sorted = sorted(score_samples["formal"])
    n = len(formal_sorted)
    obs_mean, obs_std = _mean_std(score_samples["obs"])
    delay: int | None = None
    if first_mature_date is not None and first_mature_date in formal_dates:
        delay = formal_dates.index(first_mature_date)

    def _unique_ratio(period: str) -> float | None:
        days = daily_unique.get(period) or []
        ratios = [u / total for u, total in days if total > 0]
        return round(100.0 * sum(ratios) / len(ratios), 4) if ratios else None

    def _coverage(period: str) -> dict:
        total = coverage_n[period]
        return {
            "n": total,
            "mean": round(coverage_sum[period] / total, 4) if total else None,
        }

    return {
        "score": {
            "n": n,
            "mean": round(sum(formal_sorted) / n, 4) if n else None,
            "p01": _percentile(formal_sorted, 1),
            "p05": _percentile(formal_sorted, 5),
            "p50": _percentile(formal_sorted, 50),
            "p95": _percentile(formal_sorted, 95),
            "p99": _percentile(formal_sorted, 99),
            "saturation_0_100": (
                round(100.0 * sum(1 for v in formal_sorted if v <= 0.0 or v >= 100.0) / n, 4)
                if n else None),
            "unique_ratio": _unique_ratio("formal"),
            "unique_ratio_days": len(daily_unique.get("formal") or []),
        },
        "score_coverage": {
            "formal": _coverage("formal"),
            "obs": _coverage("obs"),
        },
        "factor_clamp_rate": {
            period: (round(factor_clamp[period] / factor_total[period], 4)
                     if factor_total[period] else None)
            for period in ("obs", "formal")
        },
        "factor_maturity": {
            period: dict(sorted(maturity_counts[period].items()))
            for period in ("obs", "formal")
        },
        "maturity_delay_days": delay,
        "observation_score": {
            "n": len(score_samples["obs"]),
            "mean": obs_mean,
            "std": obs_std,
            "unique_ratio": _unique_ratio("obs"),
        },
    }
