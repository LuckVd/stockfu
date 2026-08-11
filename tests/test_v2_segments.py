from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockfu.backtest.segments import (
    EARLY_SEGMENT,
    FORMAL_BACKTEST_SEGMENTS,
    RECENT_SEGMENT,
    resolve_segments,
)
from stockfu.backtest.v2_suite import (
    V2Deployment,
    research_deployments,
    run_segmented_backtests,
)
from stockfu.backtest.v2_run import build_v2_config


def test_formal_segments_are_fixed_and_history_is_independent():
    assert [s.segment_id for s in resolve_segments()] == [
        "full", "2013-2019", "2020-2026"
    ]
    assert resolve_segments("2020-2026,full") == FORMAL_BACKTEST_SEGMENTS[:1] + (
        RECENT_SEGMENT,
    )
    assert EARLY_SEGMENT.history_origin() == date(2013, 1, 1)
    assert RECENT_SEGMENT.history_origin() == date(2015, 1, 1)


def test_research_deployment_matrix_is_ten_by_three():
    deployments = research_deployments()
    assert len(deployments) == 30
    assert {deployment.variant_id for deployment in deployments} == {
        "monthly", "weekly", "daily"
    }
    assert sum(deployment.alpha_id == "dividend_income_v2" for deployment in deployments) == 3
    dividend_month = next(
        deployment for deployment in deployments
        if deployment.alpha_id == "dividend_income_v2"
        and deployment.variant_id == "monthly"
    )
    assert dividend_month.portfolio_id == "pf_monthly_top10_v2"


def test_segment_id_is_persisted_in_v2_manifest():
    cfg = build_v2_config(
        "dividend_low_vol_v2", "cn_equity_top15_v2", "no_overlay_v1",
        ["600001"], date(2021, 1, 1), date(2021, 2, 1), date(2018, 1, 1),
        observation_count=1, segment_id="2020-2026",
    )
    assert cfg.manifest()["sample_segment"] == "2020-2026"


def test_segmented_suite_keeps_each_segment_artifact(monkeypatch, tmp_path):
    import stockfu.backtest.v2_suite as suite

    calls: list[dict] = []

    def fake_run(alpha_id, **kwargs):
        calls.append({"alpha_id": alpha_id, **kwargs})
        checkpoint = Path(kwargs["checkpoint_path"])
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("checkpoint", encoding="utf-8")
        Path(str(checkpoint) + ".audit.jsonl").write_text("", encoding="utf-8")
        return SimpleNamespace(
            manifest={
                "data_coverage": {
                    "effective_eval_end": kwargs["eval_end"].isoformat(),
                    "data_end": "2026-08-04",
                    "truncated": False,
                },
                "observation_count": kwargs["observation_count"],
                "formal_start": kwargs["eval_start"].isoformat(),
                "run_id": f"run-{kwargs['segment_id']}",
                "risk_metrics": {},
            },
            metrics={"total_return": 1.0, "annualized": 2.0},
            formal_summary={"n_days": 10, "label": "formal"},
            observation_summary={"label": "observation"},
            first_trade_date=None,
            last_trade_date=None,
            trades=[],
            score_diagnostics={},
        )

    monkeypatch.setattr(suite, "run", fake_run)
    root = tmp_path / "suite"
    result = run_segmented_backtests(
        [V2Deployment("dividend_low_vol_v2", variant_id="daily")],
        output_root=root,
        codes=["A"],
        snapshot={"snapshot_id": "sha256:" + "0" * 64},
    )

    assert len(result.runs) == 3
    assert [call["segment_id"] for call in calls] == [
        "full", "2013-2019", "2020-2026"
    ]
    assert [call["history_origin"] for call in calls] == [
        date(2013, 1, 1), date(2013, 1, 1), date(2015, 1, 1)
    ]
    manifest = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert len(manifest["entries"]) == 3
    assert all(entry["status"] == "complete" for entry in manifest["entries"])
    assert all(run.summary_path.exists() for run in result.runs)
    assert all(run.checkpoint_path.exists() for run in result.runs)
    assert {run.segment.segment_id for run in result.runs} == {
        "full", "2013-2019", "2020-2026"
    }

    with pytest.raises(FileExistsError):
        run_segmented_backtests(
            [V2Deployment("dividend_low_vol_v2", variant_id="daily")],
            output_root=root,
            codes=["A"],
            snapshot={"snapshot_id": "sha256:" + "0" * 64},
        )


def test_canonical_segment_suite_rejects_partial_selection(tmp_path):
    with pytest.raises(ValueError, match="三段"):
        run_segmented_backtests(
            [V2Deployment("dividend_low_vol_v2")],
            output_root=tmp_path / "suite",
            segments="full",
            canonical=True,
        )


def test_segmented_suite_resume_skips_complete_and_reuses_checkpoint(monkeypatch, tmp_path):
    import stockfu.backtest.v2_suite as suite

    calls: list[dict] = []

    def fake_run(alpha_id, **kwargs):
        calls.append({"alpha_id": alpha_id, **kwargs})
        checkpoint = Path(kwargs["checkpoint_path"])
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("checkpoint", encoding="utf-8")
        Path(str(checkpoint) + ".audit.jsonl").write_text("", encoding="utf-8")
        return SimpleNamespace(
            manifest={
                "data_coverage": {
                    "effective_eval_end": kwargs["eval_end"].isoformat(),
                    "data_end": "2026-08-04",
                    "truncated": False,
                },
                "observation_count": kwargs["observation_count"],
                "formal_start": kwargs["eval_start"].isoformat(),
                "run_id": f"run-{kwargs['segment_id']}",
                "risk_metrics": {},
            },
            metrics={}, formal_summary={"n_days": 1},
            observation_summary={}, first_trade_date=None,
            last_trade_date=None, trades=[], score_diagnostics={},
        )

    monkeypatch.setattr(suite, "run", fake_run)
    root = tmp_path / "suite"
    kwargs = dict(
        codes=["A"], snapshot={"snapshot_id": "sha256:" + "0" * 64},
        observation_count=1,
    )
    run_segmented_backtests(
        [V2Deployment("dividend_low_vol_v2", variant_id="daily")],
        output_root=root, **kwargs,
    )
    assert len(calls) == 3

    manifest_path = root / "suite.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    interrupted = manifest["entries"][1]
    interrupted["status"] = "running"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed = run_segmented_backtests(
        [V2Deployment("dividend_low_vol_v2", variant_id="daily")],
        output_root=root, resume_existing=True, **kwargs,
    )
    assert len(resumed.runs) == 3
    assert len(calls) == 4
    assert calls[-1]["segment_id"] == interrupted["segment_id"]
    assert calls[-1]["resume_from"] == str(root / interrupted["checkpoint"])
