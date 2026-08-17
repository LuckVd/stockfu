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


ProxyMode = Literal["free", "clash", "direct"]


def _default_codes() -> list[str]:
    from stockfu.services.universe import resolve_base_codes
    return list(resolve_base_codes("all") or [])


def _apply_and_upsert(code: str, triple: dict[str, list],
                      preserve_qfq: bool = True, *, cap_date=None) -> int:
    """合并写入(三复权)——经 quote_writer 收口。preserve_qfq=True: 已有前复权则不覆盖, 只补 raw/hfq。

    cap_date 未传时取 bar 最大日；三复权回补默认带 --end，由 funnel 做 quote_date<=cap 保证。
    """
    from stockfu.db import session_scope
    from stockfu.services.quote_writer import (
        QuotePayload, WritePolicy, upsert_quote_snapshot,
    )

    by_date: dict[date, dict[str, Any]] = {}
    for adj, bars in triple.items():
        for b in bars or []:
            by_date.setdefault(b.date, {})[adj] = b
    if not by_date:
        return 0
    cap = cap_date or max(by_date.keys())
    payload = {
        d: QuotePayload(qfq=pack.get("qfq"), raw=pack.get("raw"), hfq=pack.get("hfq"))
        for d, pack in by_date.items()
    }
    with session_scope() as s:
        n = upsert_quote_snapshot(
            s, code, payload, policy=WritePolicy.MERGE_ADJ,
            cap_date=cap, preserve_qfq=preserve_qfq)
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

    判定(2026-08-17 审查 M4 改按**日期集合**对齐):该 code 在区间内有 qfq 的
    每一天,同一天 raw 与 hfq 都非空。旧实现比行数(raw≥qfq 且 hfq≥qfq)在
    「日期错位」时会误判完整——raw 的行落在别的日期、行数照样够,真实缺口
    永久跳过。全新无 qfq 的 code 不在集合 → 会被抓。
    """
    from sqlalchemy import text

    from stockfu.db import engine

    sql = text(
        "SELECT asset_code, "
        "SUM(CASE WHEN close_qfq IS NOT NULL OR close IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN (close_qfq IS NOT NULL OR close IS NOT NULL) "
        "          AND (close_raw IS NULL OR close_hfq IS NULL) THEN 1 ELSE 0 END) "
        "FROM quote_snapshot WHERE quote_date BETWEEN :s AND :e "
        "GROUP BY asset_code"
    )
    out: set[str] = set()
    with engine.connect() as conn:
        for code, q, missing in conn.execute(sql, {"s": start, "e": end}):
            if int(q or 0) > 0 and int(missing or 0) == 0:
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
    # 终点截到已收盘最近交易日(2026-08-17 审查 M2):盘中跑 --backfill-adj-prices
    # 若用裸 today/当日 --end,baostock 会返回当日未收盘 partial bar 并写进
    # raw/hfq 列(残值留存周期长)。cap 语义不 raise,断点续传兼容。
    from stockfu.services.quote_writer import latest_closed_trade_day

    cap = latest_closed_trade_day()
    end = end or cap.isoformat()
    if date.fromisoformat(end) > cap:
        print(f"  [cap] end {end} > 已收盘最近交易日 {cap},截到 {cap}(防盘中 partial)",
              flush=True)
        end = cap.isoformat()
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
                    n = _apply_and_upsert(code, triple, preserve_qfq=preserve_qfq,
                                          cap_date=end)
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
    from stockfu.ai.operator_cache import clear_operator_cache
    return clear_operator_cache("dividend_yield")


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
