"""交易日历三层兜底(2026-08-24 修复):进程缓存 → 联网 → 本地持久化 JSON。

网络失败且无任何缓存才退化为 None(只判周末);曾成功拉取过则断网也能
正确拒绝节假日,避免 stamp 表盖非交易日章。
"""
import json
from datetime import date, timedelta

from unittest import mock

import stockfu.services.snapshot as snap


def _cal(days=400):
    """生成仅工作日的简单日历。"""
    d0 = date.today() - timedelta(days=days)
    out, d = set(), d0
    while d <= date.today() + timedelta(days=60):
        if d.weekday() < 5:
            out.add(d)
        d += timedelta(days=1)
    return out


def test_network_failure_falls_back_to_persisted_calendar(tmp_path, monkeypatch):
    cal = _cal()
    f = tmp_path / "trade_calendar.json"
    f.write_text(json.dumps({
        "fetched_at": "2026-08-24T10:00:00+08:00",
        "dates": sorted(d.isoformat() for d in cal)}), encoding="utf-8")
    monkeypatch.setattr(snap, "_TRADE_CAL", None)
    monkeypatch.setattr("stockfu.config.DATA_DIR", tmp_path)
    # 联网失败:direct_connection/akshare 导入失败路径都抛
    with mock.patch("stockfu.data.base.direct_connection",
                    side_effect=OSError("network down")):
        got = snap._trade_calendar()
    assert got == cal


def test_successful_fetch_persists_calendar(tmp_path, monkeypatch):
    cal = _cal()
    monkeypatch.setattr(snap, "_TRADE_CAL", None)
    monkeypatch.setattr("stockfu.config.DATA_DIR", tmp_path)
    with mock.patch("akshare.tool_trade_date_hist_sina",
                    return_value={"trade_date": [d.isoformat() for d in sorted(cal)]}), \
         mock.patch("stockfu.data.base.direct_connection"):
        got = snap._trade_calendar()
    assert got == cal
    raw = json.loads((tmp_path / "trade_calendar.json").read_text(encoding="utf-8"))
    assert {date.fromisoformat(s) for s in raw["dates"]} == cal


def test_stale_process_cache_refetches(tmp_path, monkeypatch):
    """进程缓存最晚日期 < 今天(跨年) → 重新拉取而非直接复用旧缓存。"""
    stale = {date(2025, 1, 2) + timedelta(days=i) for i in range(0, 200, 2)}
    assert max(stale) < date.today()
    monkeypatch.setattr(snap, "_TRADE_CAL", stale)
    fresh = _cal()
    with mock.patch("akshare.tool_trade_date_hist_sina",
                    return_value={"trade_date": [d.isoformat() for d in sorted(fresh)]}), \
         mock.patch("stockfu.data.base.direct_connection"):
        got = snap._trade_calendar()
    assert got == fresh


def test_all_layers_empty_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "_TRADE_CAL", None)
    monkeypatch.setattr("stockfu.config.DATA_DIR", tmp_path)
    with mock.patch("stockfu.data.base.direct_connection",
                    side_effect=OSError("network down")):
        assert snap._trade_calendar() is None
