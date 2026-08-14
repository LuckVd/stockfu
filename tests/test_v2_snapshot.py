"""数据快照单测（整改 §4.8.2 / §4.8.5）。

- 快照 ID 必须是内容身份：日期/行数不变但值变化 → ID 必须变化。
- 分红/成分等输入表变化 → ID 必须变化。
- validate 拒绝伪造/被改文件。
- 不同快照 → checkpoint identity 不同（拒绝跨快照恢复）。
- 候选池（default_universe）必须读快照而非 live 主库（阻塞①）。
- 快照文件必须运行期不可变（阻塞④）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from stockfu.backtest.snapshot import (
    clear_snapshot_engines,
    create_data_snapshot,
    descriptor_from_file,
    snapshot_engine,
    validate_snapshot,
)


def _make_quotes_db(path, codes):
    """仅含 quote_snapshot 的最小库：候选池查询所需表。"""
    con = sqlite3.connect(path)
    con.executescript("""
        create table quote_snapshot (asset_code text, quote_date text, close real);
    """)
    for c in codes:
        con.execute(
            "insert into quote_snapshot values (?, '2024-01-02', 10.0)", (c,))
    con.commit()
    con.close()


def _make_db(path, *, close=100.0, dividend=1.0):
    """构造最小依赖表库：行情 3 行 + 分红 1 行 + 基础/成分各 1 行。"""
    con = sqlite3.connect(path)
    con.executescript("""
        create table quote_snapshot (asset_code text, quote_date text, close real);
        create table index_quote_daily (asset_code text, quote_date text, close real);
        create table etf_quote_daily (asset_code text, quote_date text, close real);
        create table dividend_event (code text, ex_date text, per_share_cash real);
        create table stock_basic (code text, name text);
        create table index_constituent (code text, effective_from text);
        insert into quote_snapshot values
            ('600001','2024-01-02',{close}),
            ('600001','2024-01-03',{close}),
            ('600002','2024-01-03',{close});
        insert into index_quote_daily values ('sh000300','2024-01-03',4000.0);
        insert into etf_quote_daily values ('510300','2024-01-03',3.5);
        insert into dividend_event values ('600001','2024-01-10',{dividend});
        insert into stock_basic values ('600001','测试');
        insert into index_constituent values ('600001','2020-01-01');
    """.format(close=close, dividend=dividend))
    con.commit()
    con.close()


def test_snapshot_id_changes_when_values_change_but_shape_identical(tmp_path):
    """§4.8.5：最大日期与行数完全相同、只改 close 值 → snapshot ID 必须不同。"""
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _make_db(a, close=100.0)
    _make_db(b, close=999.0)
    d1 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(a))
    d2 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(b))
    assert d1["snapshot_id"] != d2["snapshot_id"]
    assert d1["snapshot_id"].startswith("sha256:")
    assert d1["tables"]["quote_snapshot"]["rows"] == 3
    assert d1["tables"]["quote_snapshot"]["max_date"] == "2024-01-03"


def test_snapshot_id_changes_when_dividend_or_membership_changes(tmp_path):
    """§4.8.5：只改一条分红或历史指数成分 → snapshot ID 必须不同。"""
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _make_db(a, dividend=1.0)
    _make_db(b, dividend=2.0)
    d1 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(a))
    d2 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(b))
    assert d1["snapshot_id"] != d2["snapshot_id"]

    c = tmp_path / "c.db"
    _make_db(c)
    con = sqlite3.connect(c)
    con.execute("update index_constituent set effective_from='2021-01-01'")
    con.commit()
    con.close()
    d3 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(c))
    assert d1["snapshot_id"] != d3["snapshot_id"]


def test_snapshot_idempotent_for_identical_content(tmp_path):
    """同内容重复备份：ID 相同且不重复占盘（只保留一个快照文件）。"""
    db = tmp_path / "a.db"
    _make_db(db)
    d1 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    d2 = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    assert d1["snapshot_id"] == d2["snapshot_id"]
    files = list((tmp_path / "snaps").glob("stockfu-*.db"))
    assert len(files) == 1


def test_validate_snapshot_rejects_missing_or_modified_file(tmp_path):
    """validate：文件丢失/内容被改/descriptor 伪造 → 拒绝。"""
    db = tmp_path / "a.db"
    _make_db(db)
    d = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    validate_snapshot(d)                       # 原样通过

    broken = dict(d)
    broken["path"] = "no-such-file.db"
    with pytest.raises(ValueError, match="不存在"):
        validate_snapshot(broken)

    path = tmp_path / "snaps" / d["path"]
    with open(path, "ab") as f:
        f.write(b"tamper")
    with pytest.raises(ValueError, match="不一致"):
        validate_snapshot(d)


def test_snapshot_engine_reads_snapshot_and_is_readonly(tmp_path):
    """snapshot_engine 打开快照只读查询，且写操作被拒（mode=ro + query_only）。"""
    db = tmp_path / "a.db"
    _make_db(db, close=100.0)
    d = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    try:
        eng = snapshot_engine(d)
        with eng.connect() as conn:
            rows = conn.execute(text(
                "select close from quote_snapshot where asset_code='600001' "
                "order by quote_date")).fetchall()
            assert [r[0] for r in rows] == [100.0, 100.0]
            # 只读：写被拒
            with pytest.raises(OperationalError):
                conn.execute(text(
                    "insert into quote_snapshot values ('X','2024-01-01',1.0)"))
        # memoize：同一 descriptor 返回同一引擎对象
        assert snapshot_engine(d) is eng
    finally:
        clear_snapshot_engines()


def test_snapshot_engine_rejects_missing_file(tmp_path):
    d = {"snapshot_id": "sha256:" + "0" * 64, "path": str(tmp_path / "nope.db")}
    try:
        with pytest.raises(ValueError, match="不存在"):
            snapshot_engine(d)
    finally:
        clear_snapshot_engines()


def test_descriptor_from_file_rebuilds_same_id(tmp_path):
    """descriptor_from_file 从快照文件重算 descriptor，内容 hash 与原一致。"""
    db = tmp_path / "a.db"
    _make_db(db, close=100.0)
    d = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    rebuilt = descriptor_from_file(d["path"])
    assert rebuilt["snapshot_id"] == d["snapshot_id"]
    assert rebuilt["tables"]["quote_snapshot"]["rows"] == 3
    assert rebuilt["tables"]["quote_snapshot"]["max_date"] == "2024-01-03"


def test_v2_reads_isolated_to_snapshot_not_main_db(tmp_path):
    """阻塞①端到端：use_read_engine(快照) 下，取数全程读快照而非主库。

    主库不存在的合成代码 ZZTEST 仅写进快照——能在 use_read_engine 块内查到，
    即证明 listing/calendar/直接查询都路由到了快照引擎（read_engine 身份切换 +
    ZZTEST 唯一性双重证据）。
    """
    from datetime import date

    import stockfu.db as db
    from sqlalchemy import text

    from stockfu.backtest.engine import _trade_calendar_days
    from stockfu.backtest.v2_engine import _load_listing_and_industry
    from stockfu.db import read_engine, use_read_engine

    live = tmp_path / "live.db"
    con = sqlite3.connect(live)
    con.executescript("""
        create table quote_snapshot (asset_code text, quote_date text, close real);
        create table stock_basic (code text, listing_date text, industry text);
        insert into quote_snapshot values ('ZZTEST','2024-01-02',12345.0);
        insert into stock_basic values ('ZZTEST','2000-01-01','TESTIND');
    """)
    con.commit()
    con.close()
    snap = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(live))
    try:
        assert read_engine() is db.engine          # 块外：全局主库
        with use_read_engine(snapshot_engine(snap)):
            snap_eng = snapshot_engine(snap)       # memoize：同一对象
            assert read_engine() is snap_eng
            assert read_engine() is not db.engine  # 块内：切到快照
            # 直接查询：主库没有 ZZTEST，查到 12345.0 即证明读快照
            with read_engine().connect() as conn:
                v = conn.execute(text(
                    "select close from quote_snapshot where asset_code='ZZTEST'"
                )).scalar()
            assert v == 12345.0
            # _load_listing_and_industry 走 read_engine → 快照 stock_basic
            listing, industry = _load_listing_and_industry(["ZZTEST"])
            assert listing.get("ZZTEST") == date(2000, 1, 1)
            assert industry.get("ZZTEST") == "TESTIND"
            # 日历来自快照 quote_snapshot（has_read_engine_override → 跳过 akshare）
            assert date(2024, 1, 2) in _trade_calendar_days(
                date(2024, 1, 1), date(2024, 1, 31))
        assert read_engine() is db.engine          # 退出块：恢复主库
    finally:
        clear_snapshot_engines()



# ----------------------------------------------------------- 阻塞①：候选池必须读快照


def test_default_universe_reads_snapshot_not_live(monkeypatch, tmp_path):
    """阻塞①函数级反例：live 库只有 600001、快照只有 000001（内容相反）。

    use_read_engine(快照) 块内 default_universe 必须只返回快照中的 000001；
    修复前它硬编码读 live engine，会返回 600001。
    """
    from datetime import date

    import stockfu.db as db
    from sqlalchemy import create_engine

    from stockfu.backtest.v2_run import default_universe
    from stockfu.db import use_read_engine

    live = tmp_path / "live.db"
    _make_quotes_db(live, ["600001"])
    snap = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(live))
    # 快照创建后把 live 库改成只含相反代码 000001：live 与快照内容相反。
    con = sqlite3.connect(live)
    con.execute("delete from quote_snapshot where asset_code='600001'")
    con.execute("insert into quote_snapshot values ('000001','2024-01-02',10.0)")
    con.commit()
    con.close()
    try:
        with use_read_engine(snapshot_engine(snap)):
            assert default_universe(
                date(2024, 1, 1), date(2024, 1, 31)) == ["600001"]
        # 无 override：read_engine() 回落全局 engine——patch 成 live 库，
        # 必须读到 live 内容（快照只影响 override 块内）。
        monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{live}"))
        assert default_universe(
            date(2024, 1, 1), date(2024, 1, 31)) == ["000001"]
    finally:
        clear_snapshot_engines()


def test_run_codes_none_uses_snapshot_universe(monkeypatch, tmp_path):
    """阻塞①公共入口反例：run(codes=None) 的候选池必须来自快照而非 live。"""
    from datetime import date

    from stockfu.backtest import v2_engine as eng, v2_run

    live = tmp_path / "live.db"
    _make_quotes_db(live, ["600001"])
    snap = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(live))
    con = sqlite3.connect(live)
    con.execute("delete from quote_snapshot where asset_code='600001'")
    con.execute("insert into quote_snapshot values ('000001','2024-01-02',10.0)")
    con.commit()
    con.close()
    captured = {}
    monkeypatch.setattr(eng, "resolve_snapshot", lambda **kw: snap)
    monkeypatch.setattr(v2_run, "run_v2_backtest",
                        lambda cfg: captured.update(cfg=cfg))
    try:
        v2_run.run("dividend_low_vol_v2",
                   eval_start=date(2024, 1, 1), eval_end=date(2024, 1, 31))
    finally:
        clear_snapshot_engines()
    assert captured["cfg"].codes == ["600001"], \
        f"run(codes=None) 候选池必须来自快照: {captured['cfg'].codes}"


def test_cli_omitted_codes_uses_snapshot_universe(monkeypatch, tmp_path):
    """阻塞①CLI 反例：省略 --codes 时候选池必须来自快照而非 live 主库。"""
    import main
    from stockfu.backtest import v2_engine as eng, v2_run

    live = tmp_path / "live.db"
    _make_quotes_db(live, ["600001"])
    snap = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(live))
    con = sqlite3.connect(live)
    con.execute("delete from quote_snapshot where asset_code='600001'")
    con.execute("insert into quote_snapshot values ('000001','2024-01-02',10.0)")
    con.commit()
    con.close()
    monkeypatch.setattr("stockfu.db.init_db", lambda: None)   # 不建真实主库 schema
    monkeypatch.setattr(eng, "resolve_snapshot", lambda **kw: snap)
    captured = {}
    import types

    def _fake_run(*a, **kw):
        captured.update(kw)
        return types.SimpleNamespace(
            metrics={}, formal_summary={"n_days": 0},
            manifest={"benchmark_code": "sh000300", "run_id": "x",
                      "risk_metrics": None, "data_coverage": None},
            first_trade_date=None, last_trade_date=None, trades=[],
            observation_summary={"missing_rate": None}, score_diagnostics={})

    monkeypatch.setattr(v2_run, "run", _fake_run)
    try:
        main.run_v2_backtest_cli(
            "dividend_low_vol_v2", None, None, 100_000.0, None, None, None,
            None, None, None, None, 20)
    finally:
        clear_snapshot_engines()
    assert captured["codes"] == ["600001"], \
        f"CLI 省略 --codes 候选池必须来自快照: {captured['codes']}"


# ----------------------------------------------------------- 阻塞②：CLI fail-closed 预检


def test_cli_canonical_dirty_fails_before_any_side_effect(monkeypatch):
    """阻塞②CLI 反例：dirty + canonical=True 时，init_db/resolve_snapshot/
    候选池/run 全部不得发生（预检必须先于一切副作用）。"""
    import main
    from stockfu.backtest import v2_engine as eng

    monkeypatch.setattr(
        eng, "git_revision", lambda: {"commit": "x" * 40, "dirty": True})
    called: list[str] = []
    monkeypatch.setattr("stockfu.db.init_db",
                        lambda: called.append("init_db"))
    monkeypatch.setattr(eng, "resolve_snapshot",
                        lambda **kw: called.append("resolve") or {})
    monkeypatch.setattr("stockfu.backtest.v2_run.run",
                        lambda **kw: called.append("run"))
    with pytest.raises(ValueError, match="干净工作树"):
        main.run_v2_backtest_cli(
            "dividend_low_vol_v2", None, None, 100_000.0, None, None, None,
            None, None, None, None, 20, canonical=True)
    assert called == [], \
        f"canonical+dirty 预检前不得有任何副作用: {called}"


# ----------------------------------------------------------- 阻塞④：快照不可变


def test_snapshot_file_readonly_after_create(tmp_path):
    """阻塞④：create_data_snapshot 落盘后必须移除全部写位（另一进程无法原地改/替换）。"""
    import stat as stat_mod

    db = tmp_path / "a.db"
    _make_db(db)
    target = tmp_path / "snaps"
    d = create_data_snapshot(str(target), src_path=str(db))
    p = target / Path(d["path"]).name
    assert stat_mod.S_IMODE(p.stat().st_mode) & 0o222 == 0, \
        f"快照文件必须无写位: {oct(stat_mod.S_IMODE(p.stat().st_mode))}"
    # 幂等复用既有快照时保持只读。
    d2 = create_data_snapshot(str(target), src_path=str(db))
    p2 = target / Path(d2["path"]).name
    assert stat_mod.S_IMODE(p2.stat().st_mode) & 0o222 == 0
    # 只读文件仍可读：descriptor 重算一致。
    assert descriptor_from_file(d["path"])["snapshot_id"] == d["snapshot_id"]


def test_descriptor_calendar_source_is_quote_snapshot(tmp_path):
    """阻塞④：descriptor 的 calendar_source 必须记录真实日历来源
    （quote_snapshot.distinct_quote_date），不得误记为 akshare。"""
    db = tmp_path / "a.db"
    _make_db(db)
    d = create_data_snapshot(str(tmp_path / "snaps"), src_path=str(db))
    assert d["calendar_source"] == "quote_snapshot.distinct_quote_date"
    rebuilt = descriptor_from_file(d["path"])
    assert rebuilt["calendar_source"] == "quote_snapshot.distinct_quote_date"
