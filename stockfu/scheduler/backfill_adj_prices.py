"""三复权价格串行回补 — **仅 baostock**(adjustflag 1/2/3 → hfq/qfq/raw)。

写入 quote_snapshot:
  *_qfq + 遗留 open/high/low/close  ← 前复权 (flag=2); 已有 qfq 默认不覆盖
  *_raw                             ← 不复权 (flag=3)
  *_hfq                             ← 后复权 (flag=1)

网络:
  baostock 是裸 TCP(public-api.baostock.com:10030), 不认 HTTP_PROXY。
  直连若被黑名单, 经 Clash SOCKS5(默认 127.0.0.1:7891) 换出口 IP。

**禁止并发**(用户约束): 单进程单线程顺序拉, 避免再触发 baostock 黑名单。

CLI:
  source /opt/clash/proxy.sh on   # 可选; 本模块自带 SOCKS monkeypatch
  python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

from sqlmodel import select

# Clash 默认 SOCKS(见 /opt/clash/proxy.sh)
_DEFAULT_SOCKS_HOST = "127.0.0.1"
_DEFAULT_SOCKS_PORT = 7891


def _enable_socks_proxy(host: str | None = None, port: int | None = None) -> str:
    """把 socket.socket 换成经 SOCKS5 的实现, 使 baostock 裸 TCP 走 Clash。

    Returns: 说明字符串。
    """
    host = host or os.environ.get("BAOSTOCK_SOCKS_HOST", _DEFAULT_SOCKS_HOST)
    port = int(port or os.environ.get("BAOSTOCK_SOCKS_PORT", _DEFAULT_SOCKS_PORT))
    try:
        import socks
        import socket
    except ImportError as e:
        raise RuntimeError(
            "需要 PySocks: pip3 install PySocks -i https://pypi.tuna.tsinghua.edu.cn/simple "
            "--break-system-packages"
        ) from e
    socks.set_default_proxy(socks.SOCKS5, host, port)
    socket.socket = socks.socksocket  # type: ignore[misc, assignment]
    return f"socks5://{host}:{port}"


def _default_codes() -> list[str]:
    from stockfu.services.universe import resolve_base_codes
    return list(resolve_base_codes("all") or [])


def _apply_and_upsert(code: str, triple: dict[str, list],
                      preserve_qfq: bool = True) -> int:
    """合并写入。preserve_qfq=True: 已有前复权则不覆盖, 只补 raw/hfq。"""
    from stockfu.db import session_scope
    from stockfu.models import QuoteSnapshot
    from stockfu.scheduler.jobs import _apply_bar_full

    by_date: dict[date, dict[str, Any]] = {}
    for adj, bars in triple.items():
        for b in bars or []:
            by_date.setdefault(b.date, {})[adj] = b
    if not by_date:
        return 0
    n = 0
    with session_scope() as s:
        existing = {
            q.quote_date: q
            for q in s.exec(
                select(QuoteSnapshot).where(QuoteSnapshot.asset_code == code)
            ).all()
        }
        dates = sorted(by_date.keys())
        prev_qfq_close = None
        for d in dates:
            pack = by_date[d]
            snap = existing.get(d) or QuoteSnapshot(asset_code=code, quote_date=d)
            is_new = d not in existing
            has_qfq = (snap.close_qfq is not None) or (
                snap.close is not None and float(snap.close or 0) > 0
            )
            if "qfq" in pack and (not preserve_qfq or not has_qfq):
                _apply_bar_full(snap, pack["qfq"], prev_qfq_close, adj="qfq")
                prev_qfq_close = pack["qfq"].close or prev_qfq_close
            elif "qfq" in pack and pack["qfq"].close:
                prev_qfq_close = pack["qfq"].close
            if "raw" in pack:
                _apply_bar_full(snap, pack["raw"], None, adj="raw")
            if "hfq" in pack:
                _apply_bar_full(snap, pack["hfq"], None, adj="hfq")
            if is_new and (snap.close is None or snap.close == 0):
                if snap.close_raw is not None:
                    snap.close = float(snap.close_raw)
                elif "qfq" in pack and pack["qfq"].close:
                    snap.close = float(pack["qfq"].close)
            if is_new:
                s.add(snap)
                existing[d] = snap
            n += 1
        s.commit()
    return n


def _fetch_triple_baostock(code: str, start: str, end: str | None) -> dict[str, list]:
    """仅 baostock 拉三套 K。调用前须已 login。"""
    from stockfu.data.baostock_source import BaostockSource

    end = end or date.today().isoformat()
    src = BaostockSource()
    if not src._ensure_login():
        src.force_relogin()
    if not src._ensure_login():
        raise RuntimeError("baostock login failed")
    triple = src.get_kline_triple(code, start, end)
    if not any(triple.values()):
        src.force_relogin()
        triple = src.get_kline_triple(code, start, end)
    return triple


def backfill_adj_prices(
    codes: list[str] | None = None,
    *,
    start: str = "2020-01-01",
    end: str | None = None,
    use_socks: bool = True,
    socks_host: str | None = None,
    socks_port: int | None = None,
    preserve_qfq: bool = True,
    progress_every: int = 5,
    sleep_sec: float = 0.15,
    # 兼容旧 CLI 参数(忽略)
    max_workers: int = 1,
    min_workers: int = 1,
    use_processes: bool = False,
) -> dict:
    """**串行** baostock 三复权回补。

    Args:
        use_socks: True 时经 Clash SOCKS5 出站(换 IP, 躲直连黑名单)
        preserve_qfq: True 不覆盖已有前复权成交价, 只写 raw/hfq
        sleep_sec: 每只票间隔, 降低封禁风险
    """
    codes = list(codes or _default_codes())
    end = end or date.today().isoformat()
    t0 = time.time()
    ok = fail = rows = 0
    errors: list[tuple[str, str]] = []

    proxy_info = "direct"
    if use_socks:
        proxy_info = _enable_socks_proxy(socks_host, socks_port)

    # 预登录一次
    from stockfu.data.baostock_source import BaostockSource
    src = BaostockSource()
    if not src.force_relogin() and not src._ensure_login():
        raise RuntimeError(f"baostock login failed (proxy={proxy_info})")

    print(
        f"=== 三复权回补(baostock 串行)  codes={len(codes)}  "
        f"{start}→{end}  proxy={proxy_info}  preserve_qfq={preserve_qfq} ===",
        flush=True,
    )

    for i, code in enumerate(codes, 1):
        try:
            triple = _fetch_triple_baostock(code, start, end)
            if not any(triple.values()):
                fail += 1
                errors.append((code, "empty"))
            else:
                n = _apply_and_upsert(code, triple, preserve_qfq=preserve_qfq)
                ok += 1
                rows += n
        except Exception as e:  # noqa: BLE001
            fail += 1
            errors.append((code, f"{type(e).__name__}: {e}"))
            # 登录态可能掉线 → 重登
            try:
                BaostockSource().force_relogin()
            except Exception:  # noqa: BLE001
                pass
        if progress_every and (i % progress_every == 0 or i == len(codes)):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            print(
                f"  [{i}/{len(codes)}] ok={ok} fail={fail} rows+={rows}  "
                f"{rate:.2f} codes/s",
                flush=True,
            )
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    try:
        BaostockSource().force_relogin()  # 实际 logout+login; 收尾 logout 即可
    except Exception:  # noqa: BLE001
        pass
    try:
        import baostock as bs
        bs.logout()
    except Exception:  # noqa: BLE001
        pass

    summary = {
        "codes": len(codes),
        "ok": ok,
        "fail": fail,
        "rows": rows,
        "elapsed_sec": round(time.time() - t0, 1),
        "start": start,
        "end": end,
        "proxy": proxy_info,
        "mode": "serial-baostock",
        "errors": errors[:50],
        "error_n": len(errors),
    }
    print(
        f"=== 完成 ok={ok} fail={fail} rows={rows} "
        f"elapsed={summary['elapsed_sec']}s proxy={proxy_info} ===",
        flush=True,
    )
    return summary


def clear_dividend_yield_cache() -> int:
    """删除错误口径的 dividend_yield 算子缓存。"""
    from sqlalchemy import text

    from stockfu.db import engine

    with engine.begin() as conn:
        r = conn.execute(
            text("DELETE FROM operator_result WHERE operator_id = 'dividend_yield'")
        )
        return int(r.rowcount or 0)


def adj_price_coverage() -> dict:
    """三复权覆盖率快检。"""
    from sqlalchemy import text

    from stockfu.db import engine

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN close_qfq IS NOT NULL OR close IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN close_raw IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN close_hfq IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM quote_snapshot"
        )).fetchone()
    n, qfq, raw, hfq = (row[0] or 0), (row[1] or 0), (row[2] or 0), (row[3] or 0)
    return {
        "rows": n,
        "has_qfq": qfq,
        "has_raw": raw,
        "has_hfq": hfq,
        "raw_pct": round(100.0 * raw / n, 2) if n else 0.0,
        "hfq_pct": round(100.0 * hfq / n, 2) if n else 0.0,
    }
