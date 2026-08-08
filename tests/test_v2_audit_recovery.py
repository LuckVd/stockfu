"""V2 audit artifact 恢复硬化（阻塞 2）的单元测试。

直接驱动 v2_engine._verify_audit_file：重算链式 checksum + 校验 offset/count，
拒绝缺失/篡改/截断，丢弃未提交尾部。链式口径与 _flush_audit 一致
（fingerprint({"prev", "line\\n"}, "v2.audit")）。不依赖主库。
"""
from __future__ import annotations

import json
from pathlib import Path

from stockfu.backtest.v2_engine import _verify_audit_file
from stockfu.scoring.contracts import fingerprint


def _row(i: int) -> dict:
    return {"date": f"2024-01-{i + 1:02d}", "period": "formal", "score": i * 10.0}


def _write_audit(path: Path, records: list[dict]) -> tuple[str, int]:
    """按 _flush_audit 口径写 artifact，返回 (末位 checksum, 末位字节 offset)。"""
    running = ""
    offset = 0
    parts: list[str] = []
    for row in records:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        running = fingerprint({"prev": running, "line": line}, prefix="v2.audit")
        offset += len(line.encode("utf-8"))
        parts.append(line)
    path.write_text("".join(parts), encoding="utf-8")
    return running, offset


def test_verify_returns_committed_records(tmp_path):
    p = tmp_path / "a.audit.jsonl"
    recs = [_row(i) for i in range(5)]
    checksum, offset = _write_audit(p, recs)
    out = _verify_audit_file(str(p), 5, checksum, offset)
    assert out == recs
    # 无尾部 → 不改文件
    assert p.read_text(encoding="utf-8").count("\n") == 5


def test_verify_truncates_uncommitted_tail(tmp_path):
    """伪造/崩溃半截写的尾部行被截断，前缀保留。"""
    p = tmp_path / "a.audit.jsonl"
    recs = [_row(i) for i in range(5)]
    checksum, offset = _write_audit(p, recs)
    # 追加两行未提交尾部
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"forged": 1}) + "\n")
        f.write(json.dumps({"forged": 2}) + "\n")
    out = _verify_audit_file(str(p), 5, checksum, offset)
    assert out == recs
    # 文件被截回 5 行
    lines = p.read_text(encoding="utf-8").split("\n")
    lines = [ln for ln in lines if ln]
    assert len(lines) == 5


def test_verify_rejects_within_prefix_tamper(tmp_path):
    """已提交前缀内某行被改 → 链式 checksum 不符 → 硬失败。"""
    p = tmp_path / "a.audit.jsonl"
    recs = [_row(i) for i in range(5)]
    checksum, offset = _write_audit(p, recs)
    lines = p.read_text(encoding="utf-8").split("\n")
    lines[2] = json.dumps({"tampered": True})          # 改第 3 行
    p.write_text("\n".join(lines), encoding="utf-8")
    try:
        _verify_audit_file(str(p), 5, checksum, offset)
    except ValueError as e:
        assert "链式 checksum" in str(e)
    else:
        raise AssertionError("篡改已提交前缀应被拒绝")


def test_verify_rejects_missing_file(tmp_path):
    p = tmp_path / "missing.audit.jsonl"
    try:
        _verify_audit_file(str(p), 5, "x", 10)
    except ValueError as e:
        assert "缺失" in str(e)
    else:
        raise AssertionError("audit 文件缺失应被拒绝")


def test_verify_zero_days_clears_stale(tmp_path):
    """expected_n==0：校验零行，并清掉任何陈旧内容。"""
    p = tmp_path / "a.audit.jsonl"
    p.write_text(json.dumps({"stale": True}) + "\n", encoding="utf-8")
    out = _verify_audit_file(str(p), 0, "", 0)
    assert out == []
    assert p.read_text(encoding="utf-8") == ""
    # 文件本不存在也允许
    out2 = _verify_audit_file(str(tmp_path / "none.audit.jsonl"), 0, "", 0)
    assert out2 == []


def test_verify_rejects_truncated_prefix(tmp_path):
    """文件行数少于声明 → 硬失败（已提交记录被删/截断）。"""
    p = tmp_path / "a.audit.jsonl"
    recs = [_row(i) for i in range(3)]
    checksum, offset = _write_audit(p, recs)
    try:
        _verify_audit_file(str(p), 7, checksum, offset)   # 声明 7 行但只有 3
    except ValueError as e:
        assert "行数不足" in str(e)
    else:
        raise AssertionError("行数不足应被拒绝")


def test_verify_rejects_offset_mismatch(tmp_path):
    """链 OK 但 offset 与声明不符 → 硬失败（位置漂移）。"""
    p = tmp_path / "a.audit.jsonl"
    recs = [_row(i) for i in range(5)]
    checksum, _ = _write_audit(p, recs)
    try:
        _verify_audit_file(str(p), 5, checksum, 999999)   # 错误 offset
    except ValueError as e:
        assert "offset" in str(e)
    else:
        raise AssertionError("offset 不符应被拒绝")
