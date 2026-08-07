"""V2 引擎真实成交路径的最小合成回测。

这些测试不依赖主库数据，但会走完整的 pending order、VirtualAccount、月调、风险
和 checkpoint 编排，避免只用成熟度不足的小股票池验证空账户路径。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import stockfu.backtest.v2_engine as engine
from stockfu.backtest.engine import _SeriesCtx
from stockfu.scoring.contracts import RawFactorObservation, fingerprint
from stockfu.scoring.profiles import profile_from_dict

# _patch_synthetic 会 patch 掉这些引擎/快照函数；保存原引用供恢复真实行为。
_ORIG_GIT_REVISION = engine.git_revision
from stockfu.strategy.alpha import AlphaDefinition, AlphaFactor
from stockfu.strategy.portfolio import (
    PortfolioConstructor, PortfolioPolicy, SelectionSpec,
)
from stockfu.strategy.risk import RiskOverlay, RiskPolicy


DATES = [date(2024, 1, 2) + timedelta(days=i) for i in range(10)]
STOCKS = ["A", "B"]
BENCH = "sh000300"


class _SyntheticUniverse:
    def __init__(self, codes, rules):
        self.codes = list(codes)
        self.rules = rules

    def eligible_on(self, _as_of, _flags):
        return set(self.codes)

    def board(self, _code):
        return "main"

    def summary(self, sizes=None):
        return {"base_size": len(self.codes), "sizes": list(sizes or [])}


def _profile():
    return profile_from_dict({
        "profile_id": "metric_v1", "version": 1,
        "raw_metric": {"id": "metric", "params": {}},
        "direction": "higher_is_better", "raw_unit": "ratio",
        "mapping": {"mode": "hybrid", "components": {
            "absolute": {"weight": 1.0, "knots": [[0, 0], [100, 100]]},
        }},
    })


def _make_config(*, end: date, checkpoint_path=None, resume_from=None,
                 rebalance: str = "monthly",
                 raw_values: dict[date, dict[str, float]] | None = None,
                 raw_fingerprints: dict[str, str] | None = None,
                 canonical: bool = False):
    profile = _profile()
    alpha = AlphaDefinition(
        alpha_id="synthetic_v2", version=1, market_scope="cn",
        factors=(AlphaFactor("metric_v1", 1.0, True),),
        minimum_coverage=1.0, minimum_valid_factor_count=1,
    )
    policy = PortfolioPolicy(
        portfolio_policy_id="synthetic_monthly", version=1,
        rebalance=rebalance,
        selection=SelectionSpec("top_n_above_score", 1, 60.0),
        weighting="equal", max_single_weight=0.8, max_gross=0.8,
        min_amount_20d=0.0, minimum_listing_days=0,
        max_industry_weight=None,
    )

    # 观测指纹必须与声明一致（引擎会校验每条观测）。
    declared_fp = raw_fingerprints or {"metric": "synthetic-raw"}

    def raw(code, as_of):
        overrides = raw_values or {}
        value = overrides.get(as_of, {}).get(code)
        if value is None:
            value = 90.0 if code == "A" else 80.0
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id="metric",
            raw_value=value, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=True,
            raw_fingerprint=declared_fp["metric"],
        )

    return engine.V2RunConfig(
        alpha=alpha,
        portfolio=PortfolioConstructor(policy),
        risk=RiskOverlay(RiskPolicy(risk_policy_id="none", version=1)),
        profiles={"metric_v1": profile}, raw_computers={"metric": raw},
        raw_params={}, raw_fingerprints=declared_fp,
        raw_computer_bindings={"metric": engine.fn_source_fingerprint(raw)},
        codes=STOCKS, eval_start=DATES[0], eval_end=end,
        history_origin=DATES[0], initial_cash=100_000.0, market_scope="cn",
        benchmark_code=BENCH, observation_count=2,
        checkpoint_path=str(checkpoint_path) if checkpoint_path else None,
        resume_from=str(resume_from) if resume_from else None,
        canonical=canonical,
        # 与 _patch_synthetic 注入的快照一致（identity 含 data_snapshot）。
        snapshot={"snapshot_id": "sha256:" + "0" * 64, "path": "synthetic.db",
                  "data_end": None, "tables": {}, "file_size": 0},
    )


def _patch_lock(monkeypatch, sha: str | None):
    """可控依赖锁身份：sha=None 表示无锁文件（canonical 拒绝）；否则
    同时放行“环境与锁一致”检查（合成测试不依赖真实安装环境）。"""
    if sha is None:
        monkeypatch.setattr(engine, "lock_identity", lambda: None)
    else:
        monkeypatch.setattr(engine, "lock_identity", lambda: {
            "lock_file": "requirements.lock", "lock_sha256": sha})
        monkeypatch.setattr(engine, "lock_matches_environment", lambda lock: True)


def _patch_synthetic(monkeypatch, *, suspended: set[date] | None = None,
                     data_dates: list[date] | None = None):
    prices = {
        "A": [100.0, 100.0, 100.0, 100.0, 110.0, 90.0, 105.0, 95.0, 100.0, 100.0],
        "B": [80.0] * len(DATES),
        BENCH: [100.0] * len(DATES),
    }

    def calendar(_start, end):
        return [d for d in DATES if d <= end]

    def market(codes, as_of, _sctx, valuation_basis="qfq"):
        i = DATES.index(as_of)
        close, open_, bars = {}, {}, {}
        for code in codes:
            if code not in prices:
                continue
            px = prices[code][i]
            close[code] = px
            open_[code] = px
            bars[code] = {
                "open": px, "high": px, "low": px, "close": px,
                "open_raw": px, "high_raw": px, "low_raw": px, "close_raw": px,
                "pct_chg": 0.0, "is_st": False,
                "trade_status": 0 if code == "A" and as_of in (suspended or set()) else 1,
                "amount": 100_000_000.0,
            }
        return close, open_, bars

    series = {
        code: {"c": [prices[code][i] for i in range(len(DATES))],
               "amt": [100_000_000.0] * len(DATES)}
        for code in prices
    }
    valid = {code: [1] * len(DATES) for code in prices}
    if data_dates is not None:
        # 模拟库数据截断：行情只到 data_dates[-1]，交易日历仍预埋到未来。
        series = {
            code: {k: arr[:len(data_dates)] for k, arr in cols.items()}
            for code, cols in series.items()
        }
        valid = {code: arr[:len(data_dates)] for code, arr in valid.items()}
        sctx = _SeriesCtx(
            series, data_dates, {d: i for i, d in enumerate(data_dates)}, valid)
    else:
        sctx = _SeriesCtx(
            series, DATES, {d: i for i, d in enumerate(DATES)}, valid)

    monkeypatch.setattr(engine, "_trade_calendar_days", calendar)
    monkeypatch.setattr(engine, "_preload_market_range", lambda *args, **kwargs: sctx)
    monkeypatch.setattr(engine, "_preload_dividend_events", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        engine, "_load_listing_and_industry",
        lambda codes: ({c: date(2000, 1, 1) for c in codes}, {c: None for c in codes}),
    )
    monkeypatch.setattr(engine, "_get_day_market", market)
    monkeypatch.setattr(
        engine.UniverseContext, "load",
        classmethod(lambda _cls, codes, rules: _SyntheticUniverse(codes, rules)),
    )
    # 合成测试不依赖真实库：数据快照与 git 元数据用固定值（保持测试纯净）。
    import stockfu.backtest.snapshot as snap_mod
    monkeypatch.setattr(
        snap_mod, "create_data_snapshot",
        lambda *a, **k: {"snapshot_id": "sha256:" + "0" * 64, "path": "synthetic.db",
                         "data_end": None, "tables": {}, "file_size": 0})
    monkeypatch.setattr(snap_mod, "validate_snapshot", lambda *a, **k: None)
    # snapshot_engine 的 fake path 无法 mode=ro 打开：stub 回全局主库引擎，
    # 保持合成测试“读主库 + 假 descriptor”语义（阻塞① test seam）。
    from stockfu.db import engine as db_engine
    monkeypatch.setattr(snap_mod, "snapshot_engine", lambda desc: db_engine)
    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "synthetic-git", "dirty": False})


def test_monthly_policy_does_not_rebalance_every_day(monkeypatch):
    _patch_synthetic(monkeypatch)
    result = engine.run_v2_backtest(_make_config(end=DATES[-1]))

    # formal 首日发信号、次日买入；同月后续价格波动不应触发普通组合纠偏。
    assert len(result.trades) == 1
    assert result.trades[0]["code"] == "A"
    assert result.trades[0]["date"] == DATES[3].isoformat()


def test_deferred_order_survives_suspension(monkeypatch):
    _patch_synthetic(monkeypatch, suspended={DATES[3]})
    result = engine.run_v2_backtest(_make_config(end=DATES[-1]))

    # D+1 停牌后保留 pending order，下一可成交日继续执行，而不是静默丢单。
    assert len(result.trades) == 1
    assert result.trades[0]["date"] == DATES[4].isoformat()


def test_deferred_buy_cancelled_when_target_removed(monkeypatch):
    """目标撤销后，遗留的买入挂单必须取消，停牌解除后不得买入已撤销目标。

    回归：挂单只被 `update` 覆盖同一代码，目标撤销(代码从 ideal 消失且未持仓)
    时 decide 不生成订单 → 旧买入挂单残留，停牌解除后照常成交，下一个调仓日
    再卖出（2024-01-05 撤销目标、01-06 仍买入、01-07 再卖出的复现场景）。
    """
    # A 在 D3/D4 连续停牌；D4 起 A/B 分数都掉出 top1(最低分 60) → 目标全部撤销。
    _patch_synthetic(monkeypatch, suspended={DATES[3], DATES[4]})
    dropped = {DATES[i]: {"A": 40.0, "B": 40.0} for i in range(4, len(DATES))}
    result = engine.run_v2_backtest(_make_config(
        end=DATES[-1], rebalance="daily", raw_values=dropped))

    # D2 决策生成买入挂单 → D3/D4 停牌顺延 → D4 目标撤销时挂单被取消。
    # 修复前：D5 停牌解除后买入 A，D6 再卖出（两笔成交）。
    assert result.trades == [], f"已撤销目标的挂单仍成交: {result.trades}"


def test_deferred_sell_survives_when_target_unchanged(monkeypatch):
    """目标未变(仍为 0)时，卖出挂单跨决策日保留，直到成交。

    与上一测试对照：清理规则只取消「与最新目标不一致」的挂单，不能把
    因跌停/停牌顺延的卖出单误删，否则目标为 0 的持仓永远卖不掉。
    """
    # D4 起 A/B 分数都掉出；A 在 D4/D5 连续停牌（卖出挂单顺延），目标保持撤销。
    _patch_synthetic(monkeypatch, suspended={DATES[4], DATES[5]})
    dropped = {DATES[i]: {"A": 40.0, "B": 40.0} for i in range(4, len(DATES))}
    result = engine.run_v2_backtest(_make_config(
        end=DATES[-1], rebalance="daily", raw_values=dropped))

    # D3 买入 A；D4 生成卖出挂单但 A 停牌顺延；D5 决策日目标未变(仍撤销)，
    # 卖出挂单必须保留；D6 停牌解除后成交卖出。
    # V2 卖出统一记 reduce；用 shares 符号区分买卖方向（股数受整百/现金约束缩放）。
    assert [t["date"] for t in result.trades] == [
        DATES[3].isoformat(), DATES[6].isoformat()]
    assert result.trades[0]["shares"] > 0 and result.trades[1]["shares"] < 0


def test_nonempty_checkpoint_resume_matches_uninterrupted(monkeypatch, tmp_path):
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "synthetic-v2.json"
    first = engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    assert first.trades
    assert first.manifest["checkpoint"]["enabled"] is True

    resumed = engine.run_v2_backtest(
        _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                     resume_from=checkpoint))
    uninterrupted = engine.run_v2_backtest(_make_config(end=DATES[-1]))

    assert resumed.trades == uninterrupted.trades
    assert resumed.equity_curve == uninterrupted.equity_curve
    assert resumed.history_checkpoint == uninterrupted.history_checkpoint


def test_eval_end_beyond_data_end_truncates_and_discloses(monkeypatch):
    """请求终点超过库数据末日时，回测截断到 data_end 并在 manifest 披露。

    回归（阻塞项④）：交易日历预埋到未来而行情只到库末日时，旧行为会在无行情
    日产生伪 equity 记录（last_close 兜底）且 checkpoint last_completed 超前。
    修复后：不跑无行情日 + manifest.data_coverage 披露 requested/effective/data_end。
    """
    _patch_synthetic(monkeypatch, data_dates=DATES[:5])   # 行情只到 D4
    result = engine.run_v2_backtest(_make_config(end=DATES[6]))  # 请求到 D6

    cov = result.manifest["data_coverage"]
    assert cov["truncated"] is True
    assert cov["requested_eval_end"] == DATES[6].isoformat()
    assert cov["effective_eval_end"] == DATES[4].isoformat()
    assert cov["data_end"] == DATES[4].isoformat()

    # 伪末日不得进入净值曲线；最后一个交易日是数据末日。
    dates = [p["date"] for p in result.equity_curve]
    assert DATES[6] not in dates
    assert max(dates) == DATES[4]


def test_data_coverage_reports_no_truncation_when_data_sufficient(monkeypatch):
    _patch_synthetic(monkeypatch)
    result = engine.run_v2_backtest(_make_config(end=DATES[-1]))
    cov = result.manifest["data_coverage"]
    assert cov["truncated"] is False
    assert cov["requested_eval_end"] == DATES[-1].isoformat()
    assert cov["effective_eval_end"] == DATES[-1].isoformat()
    assert cov["data_end"] == DATES[-1].isoformat()


def test_checkpoint_identity_changes_with_raw_computer(monkeypatch, tmp_path):
    """替换 raw computer（同 metric 不同算法指纹）必须改变 checkpoint identity，
    resume 必须拒绝续跑（阻塞项②：旧行为替换 raw 后身份仍相同）。

    回归：identity 只含 alpha/portfolio/risk/profile/codes 与 raw_params 时，
    raw 实现被替换但参数不变 → 指纹相同 → 错误恢复。修复后 raw 算法指纹入 identity。
    """
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "identity-v2.json"
    base = dict(end=DATES[4], checkpoint_path=checkpoint)

    cfg_v1 = _make_config(**base, raw_fingerprints={"metric": "fp-v1"})
    cfg_v2 = _make_config(**base, raw_fingerprints={"metric": "fp-v2"})
    assert cfg_v1.checkpoint_identity() != cfg_v2.checkpoint_identity()

    engine.run_v2_backtest(cfg_v1)
    # 同 eval 口径但 raw 指纹不同 → 拒绝 resume。
    with pytest.raises(ValueError, match="配置指纹不匹配"):
        engine.run_v2_backtest(_make_config(
            end=DATES[-1], checkpoint_path=checkpoint, resume_from=checkpoint,
            raw_fingerprints={"metric": "fp-v2"}))


def test_checkpoint_persists_full_manifest(monkeypatch, tmp_path):
    """checkpoint 工件必须持久化完整 run manifest（设计 §14），不只是 opaque hash。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "manifest-v2.json"
    engine.run_v2_backtest(_make_config(
        end=DATES[4], checkpoint_path=checkpoint,
        raw_fingerprints={"metric": "synthetic-fp"}))

    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    man = data["manifest"]
    assert man["raw_metric_fingerprints"] == {"metric": "synthetic-fp"}
    assert man["alpha_fingerprint"] and man["portfolio_fingerprint"]
    assert man["risk_fingerprint"] and man["profile_fingerprints"]
    assert man["eval_start"].startswith("2024-")
    assert man["history_origin"].startswith("2024-")

    # 完整 run manifest（§14）：配置指纹/口径 + 运行结果字段，不只配置摘要。
    # §4.8.4：trades 不再嵌入 manifest（n_trades + trades_checksum，完整列表在 state）。
    for field in ("run_id", "data_coverage", "formal_start", "n_trades",
                  "trades_checksum", "universe", "risk_metrics", "score_diagnostics",
                  "checkpoint", "raw_computer_bindings", "data_snapshot",
                  "reproducibility", "component_checksums", "output_checksum"):
        assert field in man, f"checkpoint manifest 缺少 {field}"
    assert "trades" not in man
    assert man["data_coverage"]["truncated"] is False
    assert man["checkpoint"]["finalized"] is True
    # 合成测试 git 被 patch 为 clean + canonical=False → non_canonical。
    assert man["reproducibility"]["status"] == "non_canonical"
    # manifest 内部自洽：run_id = fingerprint(去 run_id 的 manifest)。
    recomputed = fingerprint(
        {k: v for k, v in man.items() if k != "run_id"}, prefix="v2.run")
    assert man["run_id"] == recomputed
    # config_fingerprint 仍是配置身份（不含运行字段）：用同配置重建验证。
    cfg2 = _make_config(end=DATES[4], raw_fingerprints={"metric": "synthetic-fp"})
    assert data["config_fingerprint"] == cfg2.checkpoint_identity()


def test_score_diagnostics_collected(monkeypatch):
    """§15 分数诊断：合成确定性分数下分位/饱和/唯一值/覆盖/钳制/成熟度统计正确。"""
    _patch_synthetic(monkeypatch)
    result = engine.run_v2_backtest(_make_config(end=DATES[-1]))
    diag = result.score_diagnostics
    assert diag["score"]["n"] > 0
    # 横截面只有 A=90 / B=80 两个值 → 分位在 [80, 90] 内、唯一值比例 < 100%。
    assert diag["score"]["p01"] == pytest.approx(80.0, abs=1.0)
    assert diag["score"]["p99"] == pytest.approx(90.0, abs=1.0)
    # 每日横截面 2 票 2 个唯一值 → 每日唯一值比例均值 = 100%（非全期扁平去重率）。
    assert diag["score"]["unique_ratio"] == pytest.approx(100.0)
    assert diag["score"]["unique_ratio_days"] == diag["score"]["n"] // 2
    # absolute knots [[0,0],[100,100]] 直接线性映射 → 无 0/100 钳制。
    assert diag["score"]["saturation_0_100"] == 0.0
    # 分期口径：formal/obs 独立，不得混期（阻塞③）。
    assert diag["factor_clamp_rate"]["formal"] == 0.0
    assert diag["factor_clamp_rate"]["obs"] == 0.0
    assert diag["score_coverage"]["formal"]["mean"] == pytest.approx(1.0)
    assert diag["score_coverage"]["obs"]["mean"] == pytest.approx(1.0)
    # absolute-only profile：全部分量成熟（formal 与 obs 各自计数）。
    assert diag["factor_maturity"]["formal"]["mature"] > 0
    assert diag["factor_maturity"]["formal"]["immature"] == 0
    assert diag["factor_maturity"]["obs"]["mature"] > 0
    assert diag["observation_score"]["n"] > 0
    # 逐日审计：每 eval 日一条，含 strategy/factors/raw（§14 阻塞③）。
    assert result.manifest["score_diagnostics"]["score"]["n"] == diag["score"]["n"]
    assert result.manifest["daily_audit"] == {"n_days": len(result.daily_audit)}
    audit = result.daily_audit
    assert len(audit) == diag["score"]["n"] // 2 + diag["observation_score"]["n"] // 2
    first = audit[0]
    assert set(first) == {"date", "period", "strategy", "factors", "raw"}
    assert set(first["strategy"]) == {"A", "B"}
    assert "metric_v1" in first["factors"]["A"]
    assert "metric" in first["raw"]


def test_replacing_raw_callable_while_keeping_declared_fingerprint_is_rejected(monkeypatch):
    """真实反例（阻塞①）：直接替换 raw_computers 的 callable、保留声明指纹，
    必须被拒绝——声明指纹绑定实际函数（fn 源码指纹），不能绕过。

    旧行为：identity 只看声明的 raw_fingerprints，替换 callable 后身份不变。
    """
    _patch_synthetic(monkeypatch)
    cfg = _make_config(end=DATES[4])

    def other_raw(code, as_of):
        """另一个算法实现（源码不同 → fn 指纹不同）。"""
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id="metric",
            raw_value=10.0, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint="synthetic-raw",
        )

    cfg.raw_computers["metric"] = other_raw     # 替换 callable，声明指纹不变
    with pytest.raises(ValueError, match="raw_computer_bindings 与实际"):
        engine.V2RunConfig(
            alpha=cfg.alpha, portfolio=cfg.portfolio, risk=cfg.risk,
            profiles=cfg.profiles, raw_computers=cfg.raw_computers,
            raw_params=cfg.raw_params, raw_fingerprints=cfg.raw_fingerprints,
            raw_computer_bindings=cfg.raw_computer_bindings,
            codes=cfg.codes, eval_start=cfg.eval_start, eval_end=cfg.eval_end,
            history_origin=cfg.history_origin, initial_cash=cfg.initial_cash,
            market_scope=cfg.market_scope, benchmark_code=cfg.benchmark_code,
            observation_count=cfg.observation_count,
        )


def test_replacing_raw_callable_changes_checkpoint_identity(monkeypatch, tmp_path):
    """真实反例（阻塞①）：替换 callable 且同步伪造 bindings → identity 必须变化，
    resume 必须拒绝（旧行为 identity 只含声明指纹，替换后仍相同）。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "swap-fn.json"
    cfg1 = _make_config(end=DATES[4], checkpoint_path=checkpoint)
    engine.run_v2_backtest(cfg1)

    def other_raw(code, as_of):
        """另一个算法实现（源码不同）。"""
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id="metric",
            raw_value=10.0, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint="synthetic-raw",
        )

    cfg2 = _make_config(
        end=DATES[-1], checkpoint_path=checkpoint, resume_from=checkpoint)
    cfg2.raw_computers["metric"] = other_raw
    # 伪造 bindings 与声明指纹也同步改 → 仍必须因 identity 变化而拒绝。
    cfg2.raw_computer_bindings["metric"] = engine.fn_source_fingerprint(other_raw)
    cfg2.raw_fingerprints["metric"] = "synthetic-raw-swapped"
    assert cfg1.checkpoint_identity() != cfg2.checkpoint_identity()
    with pytest.raises(ValueError, match="配置指纹不匹配"):
        engine.run_v2_backtest(cfg2)


def test_observation_fingerprint_mismatch_with_declared_is_rejected(monkeypatch):
    """真实反例（阻塞①）：raw computer 返回的观测指纹与声明算法指纹不一致
    必须被拒绝（换算法但声明没更新的静默错配）。"""
    _patch_synthetic(monkeypatch)
    cfg = _make_config(end=DATES[-1])
    assert cfg.raw_fingerprints["metric"] == "synthetic-raw"

    def wrong_fp_raw(code, as_of):
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id="metric",
            raw_value=90.0, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint="other-algo-fp",
        )

    cfg.raw_computers["metric"] = wrong_fp_raw
    cfg.raw_computer_bindings["metric"] = engine.fn_source_fingerprint(wrong_fp_raw)
    with pytest.raises(ValueError, match="raw_fingerprint 与声明算法指纹不一致"):
        engine.run_v2_backtest(cfg)


def test_run_entry_rejects_post_construction_callable_swap(monkeypatch):
    """真实反例（第四轮阻塞①）：配置构造后替换 raw_computers 的 callable，
    且新函数仍返回原声明指纹 → 运行入口必须拒绝（__post_init__ 只覆盖构造时点）。

    旧行为：bindings 只在构造时校验，构造后替换无感，checkpoint_identity 不变。
    """
    _patch_synthetic(monkeypatch)
    cfg = _make_config(end=DATES[-1])

    def other_raw(code, as_of):
        """另一个实现：源码不同，但观测仍返回原声明指纹（绕过观测指纹校验）。"""
        return RawFactorObservation(
            asset_code=code, as_of=as_of, raw_metric_id="metric",
            raw_value=10.0, raw_unit="ratio", source_max_date=as_of,
            available_at=as_of, valid=True, raw_fingerprint="synthetic-raw",
        )

    cfg.raw_computers["metric"] = other_raw     # 构造后替换，声明指纹/绑定不变
    with pytest.raises(ValueError, match="运行入口校验"):
        engine.run_v2_backtest(cfg)


def test_intermediate_checkpoint_manifest_uses_save_date(monkeypatch, tmp_path):
    """真实反例（第四轮阻塞②）：中途 checkpoint 的 manifest.checkpoint.
    last_completed_date 必须是本次保存日，不能写 dates_all[-1]。

    旧行为：build_manifest 用 dates_all[-1]，首日工件 state 是 01-02 但
    manifest 谎称 01-11；只有最终工件看起来正常。
    """
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "mid.json"
    saved = []
    real_write = engine._atomic_write_checkpoint

    def spy(path, payload):
        saved.append((path, payload))
        real_write(path, payload)

    monkeypatch.setattr(engine, "_atomic_write_checkpoint", spy)
    engine.run_v2_backtest(_make_config(end=DATES[5], checkpoint_path=checkpoint))

    assert len(saved) >= 2
    for _path, payload in saved:
        man_date = payload["manifest"]["checkpoint"]["last_completed_date"]
        state_date = payload["state"]["last_completed_date"]
        assert man_date == state_date, \
            f"manifest 日期 {man_date} != state 日期 {state_date}"
    # 首次保存（D0 日末）不得谎称已到末日 DATES[5]。
    first = saved[0][1]
    assert first["manifest"]["checkpoint"]["last_completed_date"] == DATES[0].isoformat()
    assert first["state"]["last_completed_date"] == DATES[0].isoformat()
    # 最终工件 = 请求终点。
    last = saved[-1][1]
    assert last["manifest"]["checkpoint"]["last_completed_date"] == DATES[5].isoformat()


def test_manifest_reproducibility_fields(monkeypatch, tmp_path):
    """§14 可复现字段（第四轮阻塞③）：git commit、数据快照身份、输出校验和、
    checkpoint 来源（resume 链）齐全且自洽。"""
    _patch_synthetic(monkeypatch)
    # 本测试需要真实 git 元数据（_patch_synthetic 已 patch 掉，这里恢复）；
    # 数据快照保持合成 fake（真实快照需 backup 2GB 主库，由集成验收覆盖）。
    monkeypatch.setattr(engine, "git_revision", _ORIG_GIT_REVISION)
    checkpoint = tmp_path / "rep.json"

    r1 = engine.run_v2_backtest(_make_config(end=DATES[4], checkpoint_path=checkpoint))
    man = r1.manifest
    assert man["git"]["commit"] and len(man["git"]["commit"]) == 40
    assert "dirty" in man["git"]
    assert man["data_snapshot"]["snapshot_id"].startswith("sha256:")
    assert man["output_checksum"]
    # 同配置同输出重跑（另一个 checkpoint 路径，内容相同）→ 输出校验和逐位一致。
    r3 = engine.run_v2_backtest(_make_config(
        end=DATES[4], checkpoint_path=tmp_path / "rep2.json"))
    assert r3.manifest["output_checksum"] == man["output_checksum"]

    # resume 工件记录来源（可定位续跑链）。
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    r2 = engine.run_v2_backtest(_make_config(
        end=DATES[-1], checkpoint_path=checkpoint, resume_from=checkpoint))
    src = r2.manifest["checkpoint"]["resume_source"]
    assert src["path"] == str(checkpoint)
    # source_run_id 是来源工件（保存时点 manifest）的 run_id，可定位。
    assert src["source_run_id"] == data["manifest"]["run_id"]
    assert src["source_state_checksum"] == data["state_checksum"]
    assert src["source_last_completed"] == DATES[4].isoformat()


def test_snapshot_identity_gate_blocks_cross_snapshot_resume(monkeypatch, tmp_path):
    """§4.8.2/4.8.5：data_snapshot 进 checkpoint identity，不同快照不能续跑；
    同快照可续跑。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "snap-gate.json"
    snap_a = {"snapshot_id": "sha256:" + "a" * 64, "path": "a.db"}
    snap_b = {"snapshot_id": "sha256:" + "b" * 64, "path": "b.db"}

    cfg_a = _make_config(end=DATES[4], checkpoint_path=checkpoint)
    cfg_a.snapshot = snap_a
    cfg_b = _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint)
    cfg_b.snapshot = snap_b
    assert cfg_a.checkpoint_identity() != cfg_b.checkpoint_identity()
    engine.run_v2_backtest(cfg_a)
    with pytest.raises(ValueError, match="配置指纹不匹配"):
        engine.run_v2_backtest(cfg_b)

    # 同快照续跑 → 正常。
    cfg_a2 = _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                          resume_from=checkpoint)
    cfg_a2.snapshot = snap_a
    res = engine.run_v2_backtest(cfg_a2)
    assert res.manifest["checkpoint"]["resumed"] is True


def test_canonical_gate_requires_clean_worktree(monkeypatch):
    """§4.8.3：canonical=True + git dirty → 硬失败；探索性运行标记 non_canonical_dirty。"""
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "x" * 40, "dirty": True})
    cfg = _make_config(end=DATES[-1], canonical=True)
    with pytest.raises(ValueError, match="干净工作树"):
        engine.run_v2_backtest(cfg)

    cfg2 = _make_config(end=DATES[-1], canonical=False)
    res = engine.run_v2_backtest(cfg2)
    assert res.manifest["reproducibility"]["status"] == "non_canonical_dirty"


def test_finalized_checkpoint_disk_matches_result_manifest(monkeypatch, tmp_path):
    """§4.8.5：最终磁盘工件 == V2Result.manifest（finalized=True、output_checksum
    可独立重算、run_id 自洽）；两阶段落盘：中途均为 partial。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "final.json"
    saved = []
    real_write = engine._atomic_write_checkpoint

    def spy(path, payload):
        saved.append((path, payload))
        real_write(path, payload)

    monkeypatch.setattr(engine, "_atomic_write_checkpoint", spy)
    res = engine.run_v2_backtest(
        _make_config(end=DATES[5], checkpoint_path=checkpoint))

    # 磁盘工件 == 返回 manifest（同一对象语义；JSON round-trip 后 tuple→list 归一）。
    disk = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert disk["manifest"] == engine._checkpoint_jsonable(res.manifest)
    assert disk["manifest"]["checkpoint"]["finalized"] is True
    assert disk["manifest"]["output_checksum"]
    assert disk["state_checksum"] == disk["manifest"]["component_checksums"]["state"]

    # 中途均为 partial，最后一次为 finalized。
    partials = [p for _p, p in saved[:-1]]
    assert all(p["manifest"]["checkpoint"]["finalized"] is False for p in partials)
    assert all(p["manifest"].get("output_checksum") is None for p in partials)
    assert saved[-1][1]["manifest"]["checkpoint"]["finalized"] is True

    # 独立重算：state_checksum / 总 output_checksum / run_id。
    state = disk["state"]
    recomputed_state = engine.fingerprint(
        engine._checkpoint_jsonable(state), prefix="v2.checkpoint.state")
    assert recomputed_state == disk["state_checksum"]
    man = disk["manifest"]
    recomputed_output = engine.fingerprint(
        man["component_checksums"], prefix="v2.output")
    assert recomputed_output == man["output_checksum"]
    recomputed_run = engine.fingerprint(
        {k: v for k, v in man.items() if k != "run_id"}, prefix="v2.run")
    assert recomputed_run == man["run_id"]


def test_performance_partial_checkpoints_skip_diagnostics_and_duplicate_payload(monkeypatch, tmp_path):
    """§4.8.4/4.8.5：checkpoint_every=1 的 10 日运行中，
    - `_score_diagnostics()` 只在 finalize 执行一次（partial 只存样本数）；
    - 中途工件不重复嵌入完整 trades/daily_audit（manifest 只有 checksum/摘要，
      trades 只在 state 一份，audit 只在 artifact）；
    - 完整 checkpoint 写入 = 交易日数(partial) + 1(finalize)。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "perf.json"
    diag_calls = {"n": 0}
    orig_diag = engine._score_diagnostics

    def spy_diag(*a, **k):
        diag_calls["n"] += 1
        return orig_diag(*a, **k)

    monkeypatch.setattr(engine, "_score_diagnostics", spy_diag)
    saved = []
    real_write = engine._atomic_write_checkpoint

    def spy_write(path, payload):
        saved.append((path, payload))
        real_write(path, payload)

    monkeypatch.setattr(engine, "_atomic_write_checkpoint", spy_write)

    res = engine.run_v2_backtest(
        _make_config(end=DATES[-1], checkpoint_path=checkpoint))

    assert diag_calls["n"] == 1, \
        f"_score_diagnostics 应只在 finalize 执行一次: {diag_calls['n']}"
    assert len(saved) == len(DATES) + 1, \
        f"写入次数 = 每日 partial({len(DATES)}) + 1 finalize: {len(saved)}"
    for _p, payload in saved:
        man = payload["manifest"]
        assert "trades" not in man and "trades_checksum" in man
        # manifest 只有审计摘要（n_days），完整审计行在 artifact。
        assert man["daily_audit"] == {"n_days": payload["state"]["audit"]["n_days"]}
        state = payload["state"]
        assert "trades" in state                    # 完整 trades 只在 state 一份
        assert "daily_audit" not in state           # 审计不在 checkpoint state
        assert "audit" in state                     # 只有摘要（offset/n/checksum）
    # 审计行全部在 append-only artifact，不在任何 checkpoint payload 里。
    audit_rows = [ln for ln in Path(str(checkpoint) + ".audit.jsonl")
                  .read_text(encoding="utf-8").splitlines() if ln]
    assert len(audit_rows) == len(DATES)
    assert all("daily_audit" not in payload["state"] for _p, payload in saved)
    # 最终工件摘要与 artifact 行数一致。
    disk = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert disk["manifest"]["daily_audit"]["n_days"] == len(DATES)
    assert disk["state"]["audit"]["n_days"] == len(DATES)
    assert res.daily_audit == [json.loads(ln) for ln in audit_rows]


def test_audit_forged_appended_rows_truncated_on_resume(monkeypatch, tmp_path):
    """阻塞②反例：checkpoint 后向 audit artifact 追加伪造行，resume 必须截断、
    最终审计与不中断运行一致（旧行为整文件读入、伪造行被当作真实天数）。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "audit-forged.json"
    audit = Path(str(checkpoint) + ".audit.jsonl")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    assert audit.exists() and audit.read_text(encoding="utf-8").splitlines()
    # 追加两行伪造尾部
    with open(audit, "a", encoding="utf-8") as f:
        f.write(json.dumps({"forged": 1}) + "\n")
        f.write(json.dumps({"forged": 2}) + "\n")

    resumed = engine.run_v2_backtest(
        _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                     resume_from=checkpoint))
    uninterrupted = engine.run_v2_backtest(_make_config(end=DATES[-1]))
    # 伪造行被丢弃，逐日审计与不中断运行逐位一致
    assert resumed.daily_audit == uninterrupted.daily_audit
    final = [ln for ln in audit.read_text(encoding="utf-8").splitlines() if ln]
    assert len(final) == len(uninterrupted.daily_audit)
    assert all("forged" not in ln for ln in final)


def test_audit_within_prefix_tamper_rejected_on_resume(monkeypatch, tmp_path):
    """阻塞②反例：篡改已提交前缀内某行 → 链式 checksum 不符 → resume 硬失败。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "audit-tamper.json"
    audit = Path(str(checkpoint) + ".audit.jsonl")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    lines = audit.read_text(encoding="utf-8").splitlines()
    lines[2] = json.dumps({"tampered": True})           # 改已提交的第 3 行
    audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="链式 checksum"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint))


def test_audit_missing_file_rejected_on_resume(monkeypatch, tmp_path):
    """阻塞②反例：checkpoint 声明有审计行但 artifact 文件缺失 → resume 硬失败。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "audit-missing.json"
    audit = Path(str(checkpoint) + ".audit.jsonl")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    audit.unlink()
    with pytest.raises(ValueError, match="audit artifact 缺失"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint))


def test_audit_fresh_run_clears_stale_file(monkeypatch, tmp_path):
    """阻塞②：新运行（非 resume）复用旧路径时，必须清空残留 audit 文件。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "audit-stale.json"
    audit = Path(str(checkpoint) + ".audit.jsonl")
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps({"stale_from_prev_run": True}) + "\n",
                     encoding="utf-8")
    res = engine.run_v2_backtest(
        _make_config(end=DATES[-1], checkpoint_path=checkpoint))
    # 旧残留被清掉，文件只含本次运行的真实审计行
    rows = [ln for ln in audit.read_text(encoding="utf-8").splitlines() if ln]
    assert all("stale_from_prev_run" not in ln for ln in rows)
    assert res.daily_audit == [json.loads(ln) for ln in rows]


def test_canonical_dirty_fails_before_any_write(monkeypatch, tmp_path):
    """阻塞④：canonical=True + git dirty 必须在任何写盘（生成快照）之前硬失败。

    cfg.snapshot=None 强制走 create 路径；create_data_snapshot 被 spy 成「被调即报错」。
    门禁若未前移到写之前 → 会触发 spy 的 AssertionError（测试失败）；门禁正确 → ValueError。
    """
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "x" * 40, "dirty": True})
    import stockfu.backtest.snapshot as snap_mod

    def _no_create(*a, **k):
        raise AssertionError("canonical+dirty 不应在门禁前生成快照/写盘")
    monkeypatch.setattr(snap_mod, "create_data_snapshot", _no_create)
    cfg = _make_config(end=DATES[-1], canonical=True)
    cfg.snapshot = None
    with pytest.raises(ValueError, match="干净工作树"):
        engine.run_v2_backtest(cfg)


def test_data_snapshots_dir_is_gitignored():
    """阻塞④：data/snapshots/ 必须进 .gitignore（否则 canonical 干净树自触发 dirty）。"""
    gi = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "data/snapshots/" in gi


def test_checkpoint_identity_stable_across_snapshot_recreation():
    """阻塞③：同一快照内容（同 snapshot_id）幂等重建 descriptor——created_at/path/
    file_size/tables 不同——checkpoint_identity 必须不变，合法 resume 不被误拒。
    不同 snapshot_id → identity 必须不同（拒绝跨快照续跑）。"""
    cfg = _make_config(end=DATES[-1])
    cfg.snapshot = {"snapshot_id": "sha256:" + "a" * 64, "path": "/x/a.db",
                    "created_at": "2026-08-06T00:00:00+08:00", "file_size": 1,
                    "tables": {}, "data_end": None}
    cfg2 = _make_config(end=DATES[-1])
    cfg2.snapshot = {"snapshot_id": "sha256:" + "a" * 64, "path": "/y/b.db",
                     "created_at": "2026-08-07T00:00:00+08:00", "file_size": 2,
                     "tables": {"quote_snapshot": {"rows": 9}}, "data_end": "2026-08-04"}
    assert cfg.checkpoint_identity() == cfg2.checkpoint_identity()
    cfg3 = _make_config(end=DATES[-1])
    cfg3.snapshot = {"snapshot_id": "sha256:" + "b" * 64, "path": "/y/b.db",
                     "created_at": "2026-08-07T00:00:00+08:00"}
    assert cfg.checkpoint_identity() != cfg3.checkpoint_identity()





# ----------------------------------------------------------- 阻塞②：fail-closed 预检


def test_canonical_preflight_fail_closed_unknown_git(monkeypatch):
    """阻塞②：git 不可用（commit=None/dirty=None）时 canonical 必须拒绝（fail-closed）。

    修复前门禁只判 dirty truthy，None 会放行并标记 canonical + git_commit=None。
    """
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": None, "dirty": None})
    with pytest.raises(ValueError, match="40 位"):
        engine.canonical_preflight(True)
    # 非 canonical 不设门禁。
    meta = engine.canonical_preflight(False)
    assert meta["reproducibility"]["status"] == "non_canonical"


def test_git_revision_preserves_unknown_status(monkeypatch):
    """rev-parse 成功但 git status 失败时，dirty 必须保持 unknown，不能误报 clean。"""
    import subprocess

    results = iter([
        subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"], 0, stdout="a" * 40 + "\n", stderr=""),
        subprocess.CompletedProcess(
            ["git", "status", "--porcelain"], 1, stdout="", stderr="fatal"),
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: next(results))

    assert _ORIG_GIT_REVISION() == {"commit": "a" * 40, "dirty": None}


def test_canonical_preflight_rejects_unknown_status_with_valid_commit(monkeypatch):
    """commit 可读但工作区状态未知也必须 fail-closed。"""
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": None})
    with pytest.raises(ValueError, match="干净工作树"):
        engine.canonical_preflight(True)


def test_canonical_preflight_fail_closed_short_commit(monkeypatch):
    """阻塞②：commit 不是完整 40 位 → canonical 拒绝。"""
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "short", "dirty": False})
    with pytest.raises(ValueError, match="40 位"):
        engine.canonical_preflight(True)


def test_canonical_preflight_fail_closed_no_lock_file(monkeypatch):
    """§4.14.2 阻塞一：无真实锁文件（浮动 requirements.txt 不算锁）→ canonical 拒绝。

    修复前 deps_hash 把无版本约束的 requirements.txt 文本 hash 当锁，
    是 canonical 假阳性；现在只认 requirements.lock/uv.lock。
    """
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, None)
    with pytest.raises(ValueError, match="依赖锁文件"):
        engine.canonical_preflight(True)
    # 非 canonical：无锁可运行，manifest 记录 null。
    meta = engine.canonical_preflight(False)
    assert meta["reproducibility"]["deps_hash"] is None
    assert meta["reproducibility"]["lock_file"] is None


def test_canonical_preflight_passes_clean_committed(monkeypatch):
    """阻塞②正向：clean + 完整 commit + deps 可取得 → canonical 通过并返回 run_meta。"""
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    meta = engine.canonical_preflight(True)
    rep = meta["reproducibility"]
    assert rep["status"] == "canonical"
    assert rep["git_commit"] == "a" * 40 and rep["git_dirty"] is False
    assert rep["deps_hash"] == "deps-1"
    assert rep["lock_file"] == "requirements.lock"
    # 环境身份记录齐全且可审计。
    env = rep["env_identity"]
    assert env["python_impl"] and env["python_version"]
    assert env["platform"] and env["sqlite_version"] and env["installed_hash"]


# ----------------------------------------------------------- 阻塞③：锁定 canonical 恢复链


def test_canonical_resume_rejects_dirty_source(monkeypatch, tmp_path):
    """阻塞③反例：dirty 探索运行产生的 checkpoint 不得被 canonical 恢复提升。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-dirty.json"
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": True})
    _patch_lock(monkeypatch, "deps-1")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    with pytest.raises(ValueError, match="提升为 canonical"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint, canonical=True))


def test_canonical_resume_rejects_non_canonical_source(monkeypatch, tmp_path):
    """阻塞③反例：clean 但未声明 canonical 的 checkpoint 不得被 canonical 恢复。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-nc.json"
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint))
    with pytest.raises(ValueError, match="提升为 canonical"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint, canonical=True))


def test_canonical_resume_rejects_different_commit(monkeypatch, tmp_path):
    """阻塞③反例：来源与当前 git commit 不同 → canonical 恢复拒绝。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-commit.json"
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint, canonical=True))
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "b" * 40, "dirty": False})
    with pytest.raises(ValueError, match="同一 git commit"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint, canonical=True))


def test_canonical_resume_rejects_different_deps(monkeypatch, tmp_path):
    """阻塞③反例：来源与当前依赖锁 hash 不同 → canonical 恢复拒绝。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-deps.json"
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint, canonical=True))
    _patch_lock(monkeypatch, "deps-2")
    with pytest.raises(ValueError, match="deps_hash 不匹配"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint, canonical=True))


def test_canonical_resume_rejects_different_environment(monkeypatch, tmp_path):
    """同 commit/lock 但 Python/平台/安装集合身份变化时不得拼接 canonical 状态。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-env.json"
    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    env = {"installed_hash": "env-1"}
    monkeypatch.setattr(engine, "environment_identity", lambda: dict(env))
    engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint, canonical=True))

    env["installed_hash"] = "env-2"
    with pytest.raises(ValueError, match="环境身份"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                         resume_from=checkpoint, canonical=True))


def test_canonical_resume_passes_same_clean_commit(monkeypatch, tmp_path):
    """阻塞③正向：同一 clean commit + 同 deps 的 canonical partial 可恢复。"""
    _patch_synthetic(monkeypatch)
    checkpoint = tmp_path / "canon-resume-ok.json"
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    first = engine.run_v2_backtest(
        _make_config(end=DATES[4], checkpoint_path=checkpoint, canonical=True))
    assert first.manifest["reproducibility"]["status"] == "canonical"
    resumed = engine.run_v2_backtest(
        _make_config(end=DATES[-1], checkpoint_path=checkpoint,
                     resume_from=checkpoint, canonical=True))
    assert resumed.manifest["reproducibility"]["status"] == "canonical"
    assert resumed.manifest["checkpoint"]["resumed"] is True
    assert resumed.manifest["checkpoint"]["resume_source"]["source_run_id"] == \
        first.manifest["run_id"]


# ----------------------------------------------------------- 阻塞④：finalize 前复验快照


def test_finalize_rejects_modified_snapshot(monkeypatch, tmp_path):
    """阻塞④反例：运行中快照被改/替换 → finalize 前复验失败 → 硬失败且不留 finalized 工件。

    validate_snapshot 第一次调用（运行前 resolve）通过，第二次（finalize 前）抛错，
    模拟另一进程在回测期间原地修改快照文件。
    """
    _patch_synthetic(monkeypatch)
    import stockfu.backtest.snapshot as snap_mod
    checkpoint = tmp_path / "final-verify.json"
    calls = {"n": 0}

    def flaky_validate(*_a, **_k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise ValueError("快照内容与 descriptor 不一致（模拟运行中被改）")

    monkeypatch.setattr(snap_mod, "validate_snapshot", flaky_validate)
    with pytest.raises(ValueError, match="不一致"):
        engine.run_v2_backtest(
            _make_config(end=DATES[-1], checkpoint_path=checkpoint))
    # 不得留下 finalized=True 工件（最多只留中途 partial）。
    if checkpoint.exists():
        disk = json.loads(checkpoint.read_text(encoding="utf-8"))
        assert disk["manifest"]["checkpoint"]["finalized"] is not True
    assert calls["n"] == 2


def test_run_canonical_dirty_fails_before_snapshot_resolution(monkeypatch):
    """阻塞②：public run() 的 canonical 预检必须先于 resolve_snapshot。

    resolve_snapshot 可能新建 GB 级快照；dirty + canonical=True 时它必须一次都不被触发。
    """
    import stockfu.backtest.v2_run as run_mod

    monkeypatch.setattr(
        engine, "git_revision", lambda: {"commit": "x" * 40, "dirty": True})

    def _no_resolve(**kw):
        raise AssertionError("canonical+dirty 不应触发 resolve_snapshot")

    monkeypatch.setattr(engine, "resolve_snapshot", _no_resolve)
    with pytest.raises(ValueError, match="干净工作树"):
        run_mod.run("dividend_low_vol_v2",
                    eval_start=DATES[0], eval_end=DATES[-1],
                    canonical=True)


# ----------------------------------------------------------- §4.14.2：真实依赖锁门禁


def test_canonical_preflight_rejects_env_mismatch(monkeypatch):
    """§4.14.2 方案 3：锁文件存在但当前环境与锁不一致（缺包/漂移）→ canonical 拒绝。"""
    _patch_synthetic(monkeypatch)
    monkeypatch.setattr(engine, "git_revision", lambda: {"commit": "a" * 40, "dirty": False})
    _patch_lock(monkeypatch, "deps-1")
    monkeypatch.setattr(engine, "lock_matches_environment", lambda lock: False)
    with pytest.raises(ValueError, match="环境与依赖锁不一致"):
        engine.canonical_preflight(True)


def test_lock_identity_changes_with_lock_content(tmp_path, monkeypatch):
    """§4.14.2 方案 4：锁文件任一版本/hash 改变 → lock_identity 改变。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text("fastapi==0.139.0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    id1 = engine.lock_identity()
    lock.write_text("fastapi==0.141.1\n", encoding="utf-8")
    id2 = engine.lock_identity()
    assert id1["lock_file"] == "requirements.lock"
    assert id1["lock_sha256"] != id2["lock_sha256"]
    # 没有锁文件 → None（只有浮动 requirements.txt 不算锁）。
    lock.unlink()
    assert engine.lock_identity() is None


def test_environment_identity_stable():
    """§4.14.2 方案 4：同一环境重复计算 identity 相同。"""
    a = engine.environment_identity()
    b = engine.environment_identity()
    assert a == b
    assert a["python_impl"] == "cpython"
    assert a["python_version"].count(".") >= 1
    assert a["sqlite_version"] and len(a["installed_hash"]) == 64


def test_parse_lock_versions_handles_uv_hash_format(tmp_path, monkeypatch):
    """锁文件解析器必须吃下 uv pip compile --generate-hashes 的续行/hash/via 格式。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "# This file was autogenerated by uv via the following command:\n"
        "#    uv pip compile requirements.txt -o requirements.lock --generate-hashes\n"
        "akshare==1.18.82 \\\n"
        "    --hash=sha256:456f92529a6aecdf6ee77882a18b5b0392092a75cd33a3c00cbeab0bb6691488 \\\n"
        "    --hash=sha256:bb5df05b060e3a6aebbeb711d89167fc58f55ccd94317054b419ccaab1891317\n"
        "    # via -r requirements.txt\n"
        "uvicorn[standard]==0.52.1 \\\n"
        "    --hash=sha256:112ec661814189acbccd3f7b86460147cc065fc92c0821afa78918780e4354dd\n"
        "    # via\n"
        "    #   -r requirements.txt\n",
        encoding="utf-8")
    parsed = engine._parse_lock_versions({"lock_file": str(lock)})
    assert parsed == {"akshare": "1.18.82", "uvicorn": "0.52.1"}


def test_lock_matches_environment_detects_drift(tmp_path, monkeypatch):
    """§4.14.2 方案 4：锁中任一包缺装/版本漂移 → lock_matches_environment 为 False。"""
    # 真实锁文件必须可解析（存在性由仓库保证）。
    real_lock = {"lock_file": str(Path("requirements.lock").resolve())}
    assert Path(real_lock["lock_file"]).exists()
    assert isinstance(engine.lock_matches_environment(real_lock), bool)

    fake = tmp_path / "fake.lock"
    fake.write_text(
        "definitely-not-a-real-pkg-xyz==9.9.9 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8")
    assert engine.lock_matches_environment({"lock_file": str(fake)}) is False
    # 版本漂移：真实存在的包配错误版本 → False。
    fake2 = tmp_path / "fake2.lock"
    fake2.write_text(
        "sqlalchemy==0.0.1 \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8")
    assert engine.lock_matches_environment({"lock_file": str(fake2)}) is False


def test_lock_rejects_toml_uv_lock(tmp_path, monkeypatch):
    """§4.16.2 反例 1：TOML uv.lock 不得被当作锁通过（当前只认 requirements.lock）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text(
        'version = 1\n'
        '[[package]]\n'
        'name = "fastapi"\n'
        'version = "0.141.1"\n',
        encoding="utf-8")
    # lock_identity 只认 requirements.lock：TOML uv.lock 不在支持列表 → None。
    assert engine.lock_identity() is None
    # 即使把 TOML 内容塞进 requirements.lock，也识别不出精确版本 requirement。
    (tmp_path / "requirements.lock").write_text(
        'version = 1\n'
        '[[package]]\n'
        'name = "fastapi"\n'
        'version = "0.141.1"\n',
        encoding="utf-8")
    with pytest.raises(ValueError, match="精确版本"):
        engine.lock_matches_environment(
            {"lock_file": str(tmp_path / "requirements.lock")})


def test_lock_rejects_empty_lock(tmp_path):
    """§4.16.2 反例 2：空锁（仅注释/空白）不得通过。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "# This file was autogenerated by uv via the following command:\n"
        "#    uv pip compile requirements.txt -o requirements.lock --generate-hashes\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="解析为空"):
        engine._parse_lock_versions({"lock_file": str(lock)})
    with pytest.raises(ValueError, match="解析为空"):
        engine.lock_matches_environment({"lock_file": str(lock)})


def test_lock_rejects_missing_hash(tmp_path):
    """§4.16.2 反例 3：精确版本但缺 sha256 hash 不得通过。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text("fastapi==0.141.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="--hash=sha256"):
        engine._parse_lock_versions({"lock_file": str(lock)})
    # 非 sha256 哈希算法同样拒绝（只认 sha256:64hex）。
    lock.write_text(
        "fastapi==0.141.1 \\\n"
        "    --hash=md5:00000000000000000000000000000000\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        engine._parse_lock_versions({"lock_file": str(lock)})


def test_lock_rejects_non_exact_version(tmp_path):
    """§4.16.2 反例 4：非精确版本（浮动/范围/多版本）不得通过。"""
    lock = tmp_path / "requirements.lock"
    for bad in ("fastapi>=0.141.1", "fastapi~=0.141", "fastapi==0.141.1,<1"):
        lock.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="精确版本"):
            engine._parse_lock_versions({"lock_file": str(lock)})


def test_lock_rejects_duplicate_package(tmp_path):
    """§4.16.2：同包重复声明（重复冲突）不得通过。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "fastapi==0.141.1 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n"
        "fastapi==0.141.1 \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="重复声明"):
        engine._parse_lock_versions({"lock_file": str(lock)})


def test_lock_rejects_orphan_hash_before_requirement(tmp_path):
    """hash 必须属于其前面的 requirement；孤立续行不得替后续包补 hash。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "    --hash=sha256:" + "a" * 64 + "\n"
        "fastapi==0.141.1\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="孤立续行"):
        engine._parse_lock_versions({"lock_file": str(lock)})


def test_lock_rejects_duplicate_normalized_package_name(tmp_path):
    """PEP 503 等价名称（连字符/下划线/点）必须视为同一包并拒绝重复。"""
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "typing-extensions==4.15.0 \\\n"
        "    --hash=sha256:" + "a" * 64 + "\n"
        "typing_extensions==4.15.0 \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="重复声明"):
        engine._parse_lock_versions({"lock_file": str(lock)})
