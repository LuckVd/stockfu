"""全量 factor_profile ↔ raw 计算器指纹一致性回归。

背景（2026-08-17 审查发现，与 4c4bbc7 momentum metric_id 修复同类）：
``configs/factor_profiles/dividend_yield_ttm_v1.yaml`` 声明的 params 缺
``no_dividend_policy``，而 ``compute_dividend_yield_ttm`` 的指纹恒含该键 →
``build_v2_config`` 声明的期望指纹与逐观测校验的实际指纹必然不同，引用该
profile 的 V2 回测在首条观测即 fail-closed（v2_engine._validate_raw_observation）。

本测试把「声明 params ↔ 实际指纹 params」「声明 metric_id ↔ 实际 metric_id」
「RAW_COMPUTERS 注册 algo ↔ 实际 algo」的比对固化下来：拦截各 raw 模块的
``raw_fingerprint``（在任何 DB 访问之前 raise），用假 code/as_of 逐 profile
调用计算函数，比对捕获值。新 profile / 新 raw 因子接入时自动纳入守护。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import yaml

from stockfu.backtest.v2_run import RAW_COMPUTERS
from stockfu.scoring.profiles import profile_from_dict

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
AS_OF = date(2026, 6, 30)


class _Captured(Exception):
    """拦截 raw_fingerprint 调用（在其触发任何 DB 访问前抛出）。"""

    def __init__(self, metric_id: str, algo: str, params: dict):
        super().__init__(metric_id)
        self.metric_id = metric_id
        self.algo = algo
        self.params = dict(params)


def _capture_fingerprint(compute_fn, declared_params: dict):
    """调用 compute_fn 拦截其 raw_fingerprint；返回捕获的 (metric, algo, params)。"""
    import stockfu.factors.raw as raw_pkg  # noqa: F401  # 确保子模块可定位

    fn_module = sys.modules[compute_fn.__module__]
    captured: dict[str, object] = {}

    def _recorder(metric_id, algo, params):
        captured["v"] = _Captured(metric_id, algo, params)
        raise captured["v"]

    original = getattr(fn_module, "raw_fingerprint", None)
    if original is None:
        raise AssertionError(f"{compute_fn.__module__} 未导入 raw_fingerprint")
    fn_module.raw_fingerprint = _recorder
    try:
        try:
            compute_fn(code="600000", as_of=AS_OF, **declared_params)
        except _Captured:
            pass
        # 未触发 _Captured 即走到 DB/网络：视为失败（所有 raw 因子都应先建指纹）
        if "v" not in captured:
            raise AssertionError(
                f"{compute_fn.__name__} 未在 DB 访问前调用 raw_fingerprint")
        got = captured["v"]
        return got.metric_id, got.algo, got.params
    finally:
        fn_module.raw_fingerprint = original


class TestProfileFingerprintConsistency(unittest.TestCase):
    """每个 profile 的声明必须与 raw 计算函数实际构造的指纹完全一致。"""

    def test_all_profiles_match_raw_fingerprints(self):
        problems: list[str] = []
        checked = 0
        for pf in sorted((CONFIGS / "factor_profiles").glob("*.yaml")):
            data = yaml.safe_load(pf.read_text(encoding="utf-8"))
            profile = profile_from_dict(data)
            metric = profile.raw_metric_id
            if metric not in RAW_COMPUTERS:
                problems.append(f"{pf.name}: {metric!r} 未在 RAW_COMPUTERS 登记")
                continue
            spec = RAW_COMPUTERS[metric]
            try:
                got_metric, got_algo, got_params = _capture_fingerprint(
                    spec.fn, dict(profile.raw_metric_params))
            except TypeError as exc:
                problems.append(f"{pf.name}: params 与计算函数签名不匹配 → {exc}")
                continue
            checked += 1
            if got_metric != metric:
                problems.append(
                    f"{pf.name}: metric_id 失配 声明={metric!r} 实际={got_metric!r}")
            if got_algo != spec.algo:
                problems.append(
                    f"{pf.name}: algo 失配 注册={spec.algo!r} 实际={got_algo!r}")
            if dict(profile.raw_metric_params) != got_params:
                declared = dict(profile.raw_metric_params)
                detail = []
                if set(declared) - set(got_params):
                    detail.append(f"声明多出 {sorted(set(declared) - set(got_params))}")
                if set(got_params) - set(declared):
                    detail.append(f"实际多出 {sorted(set(got_params) - set(declared))}")
                diff = {
                    k: (declared.get(k), got_params.get(k))
                    for k in set(declared) & set(got_params)
                    if declared.get(k) != got_params.get(k)
                }
                if diff:
                    detail.append(f"值不同 {diff}")
                problems.append(f"{pf.name}: 指纹 params 失配 → {'; '.join(detail)}")
        self.assertTrue(
            checked >= 30, f"应至少校验 30 个 profile，实际 {checked}（发现异常）")
        self.assertEqual(
            problems, [],
            f"{len(problems)} 个 profile 指纹不一致（回测将 fail-closed）: {problems}")

    def test_dividend_yield_ttm_v1_declares_no_dividend_policy(self):
        """回归锚点：v1 股息率 profile 必须显式带 no_dividend_policy。"""
        data = yaml.safe_load(
            (CONFIGS / "factor_profiles" / "dividend_yield_ttm_v1.yaml")
            .read_text(encoding="utf-8"))
        params = data["raw_metric"]["params"]
        self.assertEqual(params.get("no_dividend_policy"), "zero")


if __name__ == "__main__":
    unittest.main()
