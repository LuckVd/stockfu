"""三复权价格串行回补 — **仅 baostock**(adjustflag 1/2/3 → hfq/qfq/raw)。

写入 quote_snapshot:
  *_qfq + 遗留 open/high/low/close  ← 前复权 (flag=2); 已有 qfq 默认不覆盖
  *_raw                             ← 不复权 (flag=3)
  *_hfq                             ← 后复权 (flag=1)

网络保障（BaostockProxySession）:
  1. 启动时拉公网免费代理 → TCP 探测入池（可 seed 本机 Clash SOCKS）
  2. 单 IP 串行拉取；失败/黑名单/空结果 → 立即剔除并换下一个
  3. baostock 裸 TCP 经 HTTP CONNECT / SOCKS 隧道（PySocks）

CLI:
  python3 main.py --backfill-adj-prices --start 2020-01-01 --end 2026-07-20
  python3 main.py --backfill-adj-prices --proxy-mode free   # 默认
  python3 main.py --backfill-adj-prices --proxy-mode clash
  python3 main.py --backfill-adj-prices --proxy-mode direct
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Literal

from sqlmodel import select

ProxyMode = Literal["free", "clash", "direct"]


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


def _make_session(
    proxy_mode: ProxyMode = "free",
    *,
    socks_host: str | None = None,
    socks_port: int | None = None,
    probe_limit: int | None = None,
    max_per_kind: int | None = None,
):
    from stockfu.data.baostock_proxy import BaostockProxySession

    host = socks_host or os.environ.get("BAOSTOCK_SOCKS_HOST", "127.0.0.1")
    port = int(socks_port or os.environ.get("BAOSTOCK_SOCKS_PORT", "7891"))
    mode = (proxy_mode or "free").lower()
    extra: dict = {}
    if probe_limit is not None:
        extra["probe_limit"] = probe_limit
    if max_per_kind is not None:
        extra["max_per_kind"] = max_per_kind
    if mode == "direct":
        return BaostockProxySession(
            use_free_pool=False, seed_local_clash=False, **extra,
        )
    if mode == "clash":
        return BaostockProxySession(
            use_free_pool=False,
            seed_local_clash=True,
            clash_host=host,
            clash_port=port,
            **extra,
        )
    # free：免费池 + 本机 clash 种子
    return BaostockProxySession(
        use_free_pool=True,
        seed_local_clash=True,
        clash_host=host,
        clash_port=port,
        **extra,
    )


def _complete_codes(start: str, end: str) -> set[str]:
    """断点续传用:返回 [start,end] 内 raw/hfq 已覆盖 qfq 的 code 集合(可跳过)。

    判定:该 code 在区间内有 qfq,且 raw 行数 ≥ qfq 行数、hfq 行数 ≥ qfq 行数
    (raw/hfq 至少补齐了 qfq 的每一天)。全新无 qfq 的 code 不在集合 → 会被抓。
    """
    from sqlalchemy import text

    from stockfu.db import engine

    sql = text(
        "SELECT asset_code, "
        "SUM(CASE WHEN close_qfq IS NOT NULL OR close IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN close_raw IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN close_hfq IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM quote_snapshot WHERE quote_date BETWEEN :s AND :e "
        "GROUP BY asset_code"
    )
    out: set[str] = set()
    with engine.connect() as conn:
        for code, q, r, h in conn.execute(sql, {"s": start, "e": end}):
            q, r, h = int(q or 0), int(r or 0), int(h or 0)
            if q > 0 and r >= q and h >= q:
                out.add(code)
    return out


def backfill_adj_prices(
    codes: list[str] | None = None,
    *,
    start: str = "2020-01-01",
    end: str | None = None,
    proxy_mode: ProxyMode = "free",
    resume: bool = True,
    # 兼容旧参数
    use_socks: bool | None = None,
    socks_host: str | None = None,
    socks_port: int | None = None,
    preserve_qfq: bool = True,
    progress_every: int = 5,
    sleep_sec: float = 0.15,
    probe_limit: int | None = None,
    max_per_kind: int | None = None,
    max_workers: int = 1,
    min_workers: int = 1,
    use_processes: bool = False,
) -> dict:
    """**串行** baostock 三复权回补（单代理串行；失败换代理）。

    Args:
        proxy_mode:
          - free   启动拉免费代理入池（默认），失败剔除并切换
          - clash  仅本机 SOCKS（BAOSTOCK_SOCKS_* / 7891）
          - direct 直连
        preserve_qfq: True 不覆盖已有前复权成交价, 只写 raw/hfq
        resume: True(默认) 断点续传——跳过 [start,end] 内 raw/hfq 已覆盖 qfq 的
            code, 只补缺口; False(--full) 强制全量重抓
        sleep_sec: 每只票间隔
    """
    # 旧 CLI：use_socks=False → direct；use_socks=True 且未显式 free 时保持 free
    if use_socks is False:
        proxy_mode = "direct"
    elif use_socks is True and proxy_mode == "free":
        pass  # 默认 free 已含 clash 种子

    codes = list(codes or _default_codes())
    end = end or date.today().isoformat()
    t0 = time.time()
    ok = fail = rows = skip = 0
    errors: list[tuple[str, str]] = []

    # 断点续传(默认开):跳过 [start,end] 内 raw/hfq 已覆盖 qfq 的 code; --full 关
    if resume:
        _complete = _complete_codes(start, end)
        pending = [c for c in codes if c not in _complete]
        skip = len(codes) - len(pending)
        print(f"=== resume: {skip}/{len(codes)} 已完成跳过 → {len(pending)} 待补 ===",
              flush=True)
        if not pending:
            print("=== 全部已完成,无需回补(--full 可强制重抓) ===", flush=True)
            return {
                "codes": len(codes), "ok": 0, "fail": 0, "rows": 0, "skip": skip,
                "pending": 0, "elapsed_sec": round(time.time() - t0, 1),
                "start": start, "end": end, "proxy": "n/a", "proxy_mode": proxy_mode,
                "rotates": 0, "dropped": 0, "mode": "resume-all-complete",
                "errors": [], "error_n": 0,
            }
    else:
        pending = list(codes)

    sess = _make_session(
        proxy_mode,
        socks_host=socks_host,
        socks_port=socks_port,
        probe_limit=probe_limit,
        max_per_kind=max_per_kind,
    )
    proxy_info = sess.start()

    print(
        f"=== 三复权回补(baostock 串行+代理池)  codes={len(codes)} "
        f"(pending={len(pending)} resume={resume})  {start}→{end}  "
        f"proxy_mode={proxy_mode}  proxy={proxy_info}  preserve_qfq={preserve_qfq} ===",
        flush=True,
    )

    try:
        for i, code in enumerate(pending, 1):
            try:
                triple = sess.fetch_kline_triple(code, start, end)
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
                # 会话级再尝试换代理，避免整批卡死
                try:
                    if not sess.mark_bad_and_rotate(f"code_exc:{code}"):
                        print("  [abort] baostock all exhausted (proxy pool + direct fallback)", flush=True)
                        # i 为 1-based；当前 code 已记 fail，补记后续
                        for j in range(i, len(pending)):
                            errors.append((pending[j], "baostock_all_exhausted"))
                            fail += 1
                        break
                except Exception:  # noqa: BLE001
                    pass
            if progress_every and (i % progress_every == 0 or i == len(pending)):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                print(
                    f"  [{i}/{len(pending)}] ok={ok} fail={fail} rows+={rows}  "
                    f"{rate:.2f} codes/s  proxy={sess.proxy_url}  "
                    f"pool={sess.pool.remaining()}  rotates={sess.rotates}",
                    flush=True,
                )
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    finally:
        sess.stop()

    summary = {
        "codes": len(codes),
        "ok": ok,
        "fail": fail,
        "rows": rows,
        "skip": skip,
        "pending": len(pending),
        "elapsed_sec": round(time.time() - t0, 1),
        "start": start,
        "end": end,
        "proxy": sess.proxy_url,
        "proxy_mode": proxy_mode,
        "rotates": sess.rotates,
        "dropped": sess.dropped,
        "mode": "serial-baostock-proxy-pool",
        "errors": errors[:50],
        "error_n": len(errors),
    }
    print(
        f"=== 完成 ok={ok} fail={fail} rows={rows} skip={skip} "
        f"elapsed={summary['elapsed_sec']}s proxy_mode={proxy_mode} "
        f"rotates={sess.rotates} dropped={sess.dropped} ===",
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
