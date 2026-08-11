"""V2 准确性验证:prefix invariance(设计 §16.8)。

跑到 T1 与 T2(T2>T1),截至 T1 的 formal 净值与成交必须逐位一致
(延长结束日不改变既有日期的任何输出)。同时验证:观察期零订单。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import stockfu.backtest.snapshot as snap_mod
from stockfu.backtest.v2_run import build_v2_config
from stockfu.backtest.v2_run import run
from stockfu.scoring.history import build_history_retention

_FAKE_SNAPSHOT = {
    "snapshot_id": "sha256:" + "0" * 64, "path": "synthetic.db",
    "created_at": "2026-08-06T00:00:00+08:00", "data_end": None,
    "file_size": 0, "tables": {}, "calendar_source": "test",
}


@pytest.fixture(autouse=True)
def _fake_data_snapshot(monkeypatch):
    """单元测试注入快照 descriptor，不备份 2.2GB 真实主库（§4.8.5）。

    snapshot_engine 的 fake path 无法 mode=ro 打开：stub 回全局主库引擎，
    保持“读主库 + 假 descriptor”语义（阻塞① test seam）。
    """
    monkeypatch.setattr(snap_mod, "create_data_snapshot",
                        lambda *a, **k: dict(_FAKE_SNAPSHOT))
    monkeypatch.setattr(snap_mod, "validate_snapshot", lambda *a, **k: None)
    from stockfu.db import engine as db_engine
    monkeypatch.setattr(snap_mod, "snapshot_engine", lambda desc: db_engine)

CODES = ["600519", "000858", "000001", "601398", "600036", "601288", "600276",
         "000333", "600030", "601166", "601318", "600000", "000651", "600887", "000568"]
HISTORY_ORIGIN = date(2020, 1, 1)
EVAL_START = date(2023, 6, 1)
T1 = date(2023, 12, 29)
T2 = date(2024, 6, 1)


def _run_until(end: date):
    # 固定 observation_count:让 formal_start 不随 eval_end 变(§9.4 prefix invariance)
    return run("dividend_low_vol_v2", eval_start=EVAL_START, eval_end=end,
               codes=CODES, history_origin=HISTORY_ORIGIN, observation_count=28)


def _trades_until(res, cutoff: date):
    keys = []
    for t in res.trades:
        d = date.fromisoformat(t["date"])
        if d <= cutoff:
            keys.append((t["date"], t["code"], t.get("kind"), t.get("shares"),
                         round(t.get("price", 0) or 0, 4)))
    return sorted(keys)


def test_prefix_invariance_equity_and_trades():
    short = _run_until(T1)
    long = _run_until(T2)

    eq_short = [p["equity"] for p in short.formal_equity_curve]
    eq_long_prefix = [p["equity"] for p in long.formal_equity_curve
                      if p["date"] <= T1]
    assert eq_short == eq_long_prefix, (
        f"prefix equity 不一致: short={eq_short[-3:]} long_prefix={eq_long_prefix[-3:]}")

    tr_short = _trades_until(short, T1)
    tr_long = _trades_until(long, T1)
    assert tr_short == tr_long, (
        f"prefix trades 不一致: short={len(tr_short)} long={len(tr_long)}")


def test_observation_period_no_trades():
    """观察期(formal 前的 1/5)零订单(§9.1)。"""
    res = _run_until(T2)
    formal_start = date.fromisoformat(res.manifest["formal_start"])
    obs_trades = [t for t in res.trades
                  if date.fromisoformat(t["date"]) < formal_start]
    assert obs_trades == [], f"观察期不应有成交: {obs_trades[:3]}"
    # 成熟门禁可能让短小样本池在整个区间都不交易；若有成交，首单仍须
    # 晚于 formal_start。
    assert res.first_trade_date is None or res.first_trade_date >= formal_start


def test_same_alpha_different_dates_independent():
    """history_origin 相同、延长结束日:既有 formal 净值逐位一致(再次确认 §9.4)。"""
    a = _run_until(date(2024, 3, 29))
    b = _run_until(date(2024, 9, 30))
    eq_a = [p["equity"] for p in a.formal_equity_curve if p["date"] <= date(2024, 3, 29)]
    eq_b = [p["equity"] for p in b.formal_equity_curve if p["date"] <= date(2024, 3, 29)]
    assert eq_a == eq_b


def test_raw_summary_split_by_period():
    """missing_rate 诊断按观察/formal 分期独立计数,预热期不计(§15、§22.4)。

    回归:raw_missing/raw_total 曾是全期单一计数器(预热+观察+formal 累计),同时
    喂给 observation_summary 与 formal_summary,导致两期 missing_rate 完全相同且
    混入预热期。修复后按 t∈obs_set/formal_set 分桶。本测锁住「两期独立」:
    formal 期(约 4/5 天数)raw_total 必明显大于 observation 期(1/5);旧的全期
    单一计数器会使两者相等 → 本测失败。
    """
    res = _run_until(T2)
    obs, frm = res.observation_summary, res.formal_summary
    for summary in (obs, frm):
        assert {"raw_total", "missing_count", "missing_rate"} <= set(summary)
    for m in ("dividend_yield_ttm", "low_volatility_20d"):
        assert frm["raw_total"][m] > obs["raw_total"][m], (
            f"{m}: 两期 raw_total 未分期独立 "
            f"obs={obs['raw_total'][m]} formal={frm['raw_total'][m]}")
        for d in (obs["missing_rate"][m], frm["missing_rate"][m]):
            assert d is None or 0.0 <= d <= 1.0


def test_full_checkpoint_resume_matches_uninterrupted(tmp_path):
    """完整断点包含评分/账户/挂单/换手状态，续跑结果逐位等于不中断运行。"""
    checkpoint = tmp_path / "v2-checkpoint.json"
    codes = CODES[:5]
    start = date(2023, 6, 1)
    first_end = date(2023, 8, 31)
    final_end = date(2023, 12, 29)
    kwargs = dict(
        eval_start=start, history_origin=HISTORY_ORIGIN, codes=codes,
        observation_count=10, risk_id="v1_core_v1",
    )
    first = run("dividend_low_vol_v2", eval_end=first_end,
                checkpoint_path=str(checkpoint), **kwargs)
    assert checkpoint.exists()
    resumed = run("dividend_low_vol_v2", eval_end=final_end,
                   checkpoint_path=str(checkpoint), resume_from=str(checkpoint), **kwargs)
    uninterrupted = run("dividend_low_vol_v2", eval_end=final_end, **kwargs)

    assert resumed.equity_curve == uninterrupted.equity_curve
    assert resumed.trades == uninterrupted.trades
    assert resumed.formal_equity_curve == uninterrupted.formal_equity_curve
    assert resumed.metrics == uninterrupted.metrics
    # 诊断计数（分数/覆盖/成熟度）随 checkpoint 续跑累积，恢复后与不中断一致。
    assert resumed.score_diagnostics == uninterrupted.score_diagnostics
    # 逐日审计同样随 checkpoint 续跑累积，逐位一致。
    assert resumed.daily_audit == uninterrupted.daily_audit
    assert resumed.manifest["checkpoint"]["resumed"] is True
    assert first.manifest["checkpoint"]["enabled"] is True


def test_checkpoint_state_prefix_matches_shorter_run(tmp_path):
    """完整 prefix invariance：短跑到 T1 的 checkpoint state，是长跑到 T2 的
    checkpoint state 中截至 T1 的前缀（历史状态/净值/成交逐字段一致）。"""
    ckpt1 = tmp_path / "ck-short.json"
    ckpt2 = tmp_path / "ck-long.json"
    codes = CODES[:5]
    start = date(2023, 6, 1)
    t1 = date(2023, 8, 31)
    t2 = date(2023, 12, 29)
    kwargs = dict(
        eval_start=start, history_origin=HISTORY_ORIGIN, codes=codes,
        observation_count=10, risk_id="v1_core_v1",
    )
    run("dividend_low_vol_v2", eval_end=t1, checkpoint_path=str(ckpt1), **kwargs)
    run("dividend_low_vol_v2", eval_end=t2, checkpoint_path=str(ckpt2), **kwargs)

    s1 = json.loads(ckpt1.read_text(encoding="utf-8"))["state"]
    s2 = json.loads(ckpt2.read_text(encoding="utf-8"))["state"]
    assert s1["last_completed_date"] == t1.isoformat()

    retention = build_history_retention(build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "v1_core_v1", codes,
        start, t2, HISTORY_ORIGIN, observation_count=10,
    ).profiles.values())

    def _prefix(arr2, cutoff):
        return [row for row in arr2
                if date.fromisoformat(
                    row["date"] if isinstance(row, dict) else row[0]) <= cutoff]

    # 历史状态：rolling 分量在长跑 checkpoint 中会按更晚 cutoff 逐出更老
    # 的行；比较两者仍共同保留的可见前缀。expanding 分量仍要求完整前缀。
    for scope in ("self", "market", "industry"):
        for metric, groups in s1["history"].get(scope, {}).items():
            for key, arr1 in groups.items():
                arr2 = s2["history"].get(scope, {}).get(metric, {}).get(key, [])
                policy = retention.get(metric, {}).get(scope)
                if policy is None or policy[0] == "expanding":
                    expected = arr1
                else:
                    boundary = t2 - timedelta(days=int(policy[1] * 365.25))
                    expected = [
                        row for row in arr1
                        if date.fromisoformat(str(row[0])) > boundary
                    ]
                assert expected == _prefix(arr2, t1), f"{scope}/{metric}/{key} 前缀不一致"
    # 净值 / 成交 / 宇宙大小前缀一致。
    assert s1["equity_curve"] == _prefix(s2["equity_curve"], t1)
    assert s1["trades"] == _prefix(s2["trades"], t1)
    assert s1["universe_sizes"] == s2["universe_sizes"][:len(s1["universe_sizes"])]
    # raw 缺失/总数是累计计数：观察期相同则 obs 计数相等；formal 短跑 ≤ 长跑。
    assert s1["raw_missing"] == s2["raw_missing"]
    for metric in s1["raw_total"]:
        assert s1["raw_total"][metric]["obs"] == s2["raw_total"][metric]["obs"]
        assert s1["raw_total"][metric]["formal"] <= s2["raw_total"][metric]["formal"]
    # 分数样本：短跑样本是长跑样本的前缀（续跑只追加）。
    assert s1["score_samples"] == {
        p: s2["score_samples"][p][:len(s1["score_samples"][p])]
        for p in ("obs", "formal")}
    # 逐日审计（§14）：daily_audit 为 append-only artifact（§4.8.4），
    # checkpoint 只存摘要；短跑审计 == 长跑审计中截至 T1 的前缀。
    def _read_audit(ckpt):
        p = str(ckpt) + ".audit.jsonl"
        return [json.loads(ln) for ln in
                __import__("pathlib").Path(p).read_text(encoding="utf-8").splitlines()
                if ln]
    a1, a2 = _read_audit(ckpt1), _read_audit(ckpt2)
    assert a1 == [row for row in a2 if date.fromisoformat(row["date"]) <= t1]
    assert s1["audit"]["n_days"] == len(a1)
    assert s2["audit"]["n_days"] == len(a2)
    # 每日横截面唯一值计数前缀一致（长跑只是追加后续日）。
    assert s1["daily_unique"] == {
        p: s2["daily_unique"][p][:len(s1["daily_unique"][p])]
        for p in ("obs", "formal")}
