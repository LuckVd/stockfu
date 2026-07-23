"""baostock 拉取保障：免费代理池 + 串行单 IP + 失败即切换。

用法（回补 / 批量拉数）::

    from stockfu.data.baostock_proxy import BaostockProxySession

    with BaostockProxySession() as sess:
        triple = sess.fetch_kline_triple(code, start, end)
        # 失败自动剔除当前代理并换下一个再试

设计：
  1. 启动时拉免费代理 → TCP 探测 → 池
  2. 同时可选塞入本机 Clash SOCKS 作种子
  3. 同一时刻只用一个代理（串行）；login 成功后拉数
  4. 黑名单 / 网络错 / 空结果（可判定）→ 剔除当前 IP → 换下一个 → 重登
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from stockfu.data.free_proxy_pool import (
    FreeProxyPool,
    ProxyEndpoint,
    local_clash_socks,
)

T = TypeVar("T")

# baostock 明确表示「这 IP 废了」的错误码
_FATAL_LOGIN_CODES = {
    "10001011",  # 黑名单
    "10001005",  # 登陆数上限
    "10001002",  # 用户名密码错误（匿名账号异常）
    "10002002",  # 连接失败
    "10002003",  # 连接超时
    "10002004",  # 接收时连接断开
    "10002005",
    "10002006",
    "10002007",  # 接收错误
    "10002008",
    "10002001",
}

_NET_ERROR_MARKERS = (
    "Connection reset",
    "Broken pipe",
    "timed out",
    "Timeout",
    "Network is unreachable",
    "Connection refused",
    "ProxyError",
    "GeneralProxyError",
    "SOCKS",
    "服务器连接失败",
    "接收数据异常",
    "you don't login",
)


@dataclass
class BaostockProxySession:
    """一次批量拉数会话：代理池生命周期 = 本 session。"""

    # 启动参数
    use_free_pool: bool = True
    seed_local_clash: bool = True
    clash_host: str = "127.0.0.1"
    clash_port: int = 7891
    max_per_kind: int = 300
    probe_limit: int = 180
    probe_workers: int = 50
    tcp_timeout: float = 5.0
    socket_timeout: float = 12.0
    # baostock.login 硬超时（线程 join；超时必失败并换 IP，避免卡死）
    login_timeout: float = 15.0
    # bootstrap 最多尝试多少个代理 login（防止全池 15s×N 拖太久）
    max_login_tries: int = 12
    # 并发 login 校验参数
    login_workers: int = 12
    login_probe_limit: int = 36
    login_need: int = 3  # 至少确认 N 个可用再开拉
    # 单只票最多换代理次数
    max_rotate_per_call: int = 8
    # 当前代理连续失败多少次强制换
    max_fail_streak: int = 2
    sleep_after_rotate: float = 0.3
    # 单次 fetch（triple 三查）硬超时；坏代理会卡在 baostock 接收重试循环，
    # socket_timeout 救不了 → 线程级超时兜底，超时即判坏代理换 IP
    fetch_timeout: float = 60.0

    pool: FreeProxyPool = field(default_factory=FreeProxyPool)
    active: bool = False
    proxy_url: str = "direct"
    fail_streak: int = 0
    rotates: int = 0
    dropped: int = 0
    logins: int = 0
    # re-bootstrap / 常驻健康刷新状态
    _bs_params: dict = field(default_factory=dict)
    last_bootstrap_ts: float = 0.0
    bootstrap_count: int = 0
    # 直连兜底状态:代理池 + rebootstrap 全耗尽后,回落直连 baostock(IP 解封后可用)
    _direct_fallback_max: int = 3
    _direct_fallback_cooldown: float = 300.0
    _direct_tries: int = 0
    _direct_last_ts: float = 0.0
    _direct_since_ts: float = 0.0  # >0 表示当前正用直连

    def __enter__(self) -> "BaostockProxySession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ----- 生命周期 -----
    def start(self) -> str:
        """并发确认 baostock 代理 → 再 enable 最快可用 IP 并 login → 才允许拉数。"""
        self.pool.socket_timeout = self.socket_timeout
        self.pool.dead_ttl = float(os.environ.get("BAOSTOCK_DEAD_TTL", "1800"))
        self.fetch_timeout = float(os.environ.get("BAOSTOCK_FETCH_TIMEOUT", "60"))
        self._direct_fallback_max = int(os.environ.get("BAOSTOCK_DIRECT_FALLBACK_MAX", "3"))
        self._direct_fallback_cooldown = float(os.environ.get("BAOSTOCK_DIRECT_FALLBACK_COOLDOWN", "300"))
        seeds: list[ProxyEndpoint] = []
        if self.seed_local_clash:
            seed = local_clash_socks(self.clash_host, self.clash_port)
            if seed:
                seeds.append(seed)
                print(f"  [proxy] seed local clash {seed}", flush=True)

        if self.use_free_pool:
            bs_kwargs = dict(
                max_per_kind=self.max_per_kind,
                probe_limit=self.probe_limit,
                workers=self.probe_workers,
                tcp_timeout=self.tcp_timeout,
                seeds=seeds or None,
                login_verify=True,
                login_workers=self.login_workers,
                login_timeout=self.login_timeout,
                login_probe_limit=self.login_probe_limit,
                login_need=self.login_need,
            )
            self._bs_params = bs_kwargs
            n = self.pool.bootstrap(**bs_kwargs)
            self.last_bootstrap_ts = time.time()
            self.bootstrap_count = 1
            if n == 0 and seeds:
                # 并发校验全挂：退回种子再串行 login 试一次
                print("  [proxy] concurrent login none; fallback seed serial", flush=True)
                self.pool.alive = list(seeds)
            elif n == 0:
                raise RuntimeError(
                    f"baostock proxy pool: no proxy passed concurrent login "
                    f"(dead={len(self.pool.dead)})"
                )
            else:
                print(
                    f"  [proxy] concurrent verified={n} "
                    f"(will use fastest first)",
                    flush=True,
                )
        elif seeds:
            self.pool.alive = list(seeds)
            self.pool.candidates = list(seeds)
        else:
            # 直连
            self.proxy_url = "direct"
            self.active = True
            register_session(self)
            if not self._login():
                raise RuntimeError("baostock login failed (direct)")
            return self.proxy_url

        # alive 已是 login 校验过的；enable 最快一个并主进程再 login 一次
        if not self._switch_to_next(reason="verified-pool"):
            raise RuntimeError(
                f"baostock proxy pool empty or all login failed "
                f"(dead={len(self.pool.dead)})"
            )
        self.active = True
        register_session(self)
        print(
            f"=== baostock session ready proxy={self.proxy_url} "
            f"pool_left={self.pool.remaining()} verified_pool ===",
            flush=True,
        )
        return self.proxy_url
    def stop(self) -> None:
        global _global_session
        try:
            import baostock as bs
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        from stockfu.data.baostock_source import BaostockSource
        BaostockSource._logged_in = False
        self.pool.disable()
        self.active = False
        with _global_lock:
            if _global_session is self:
                _global_session = None
        print(
            f"=== baostock session stop rotates={self.rotates} "
            f"dropped={self.dropped} logins={self.logins} ===",
            flush=True,
        )

    # ----- 直连兜底:代理池 + rebootstrap 全耗尽后的最后手段 -----
    def _try_direct_fallback(self, reason: str = "") -> bool:
        """代理池 + rebootstrap 全耗尽后直连 baostock(IP 解封后可用)。

        成功: proxy_url='direct', 后续 query 直连到 session 结束(长通道由
              maybe_refresh 在代理池回血后切回代理池)。
        失败: proxy_url='none', 返回 False(调用方据此中止/放弃该 code)。
        受 env BAOSTOCK_DIRECT_FALLBACK + 计数/冷却保护,避免 IP 再被封时无限硬撞。
        """
        env_on = (os.environ.get("BAOSTOCK_DIRECT_FALLBACK", "1").strip().lower()
                  not in ("0", "off", "no", "false"))
        if not env_on:
            self.proxy_url = "none"
            return False
        if self._direct_tries >= self._direct_fallback_max:
            self.proxy_url = "none"
            return False
        now = time.time()
        if now - self._direct_last_ts < self._direct_fallback_cooldown:
            self.proxy_url = "none"
            return False
        self._direct_tries += 1
        self._direct_last_ts = now
        print(f"  [direct-fallback] try direct ({reason}) "
              f"tries={self._direct_tries}/{self._direct_fallback_max}", flush=True)
        # 关键:先还原 monkeypatch + 强关残留 SOCKS socket,
        # 否则 _login 内的 bs.login() 仍走上一条坏代理
        self.pool.disable()                    # → _restore_baostock_proxy()
        _force_close_baostock_socket()         # 关 ctx.default_socket
        self.proxy_url = "direct"
        if self._login():                      # 复用带 login_timeout 硬超时的 login
            self._direct_since_ts = now
            print("  [direct-fallback] direct login ok — stay direct until pool recovers",
                  flush=True)
            return True
        self.proxy_url = "none"
        return False

    # ----- 代理切换 -----
    def _switch_to_next(self, reason: str = "") -> bool:
        """剔除逻辑在调用方 remove 后执行；此处 pop → enable → login。"""
        tries = 0
        while True:
            if tries >= self.max_login_tries:
                print(
                    f"  [proxy] max_login_tries={self.max_login_tries} exhausted",
                    flush=True,
                )
                return self._try_direct_fallback(reason=f"{reason}:max_login_tries")
            ep = self.pool.pop()
            if ep is None:
                # 池空：冷却内重拉一轮（长回补不再因池薄中途夭折）
                if self._maybe_rebootstrap_if_allowed(reason=reason or "pool_empty"):
                    continue
                return self._try_direct_fallback(reason=f"{reason}:pool_empty")
            try:
                self.proxy_url = self.pool.enable(ep)
            except Exception as e:  # noqa: BLE001
                self.pool.remove(ep, reason=f"enable: {e}")
                self.dropped += 1
                continue
            self.rotates += 1
            tries += 1
            print(
                f"  → switch proxy #{self.rotates} {self.proxy_url}  "
                f"({reason}) try={tries}/{self.max_login_tries}",
                flush=True,
            )
            if self._login():
                self.fail_streak = 0
                self._direct_tries = 0  # 回到代理池，清空直连计数
                return True
            # login 失败：剔除当前，继续下一个
            self.pool.remove(ep, reason="login_fail")
            self.dropped += 1
            self.pool.disable()
            _force_close_baostock_socket()
            if self.sleep_after_rotate > 0:
                time.sleep(self.sleep_after_rotate)

    # ----- 池自愈：耗尽重拉 / 常驻健康刷新 -----
    def _rebootstrap(self) -> int:
        """用记录的参数重新拉+探测+校验一轮代理。返回新确认可用数。

        保持当前 login 与已 enable 的代理不动，只补 bench（login 校验在子进程
        独立进行，不打扰主进程 socket 注入）。先清过期 dead，让重拉能捡回
        瞬时抖动掉的 IP。
        """
        if not self._bs_params:
            return 0
        self.pool._prune_dead()
        self.bootstrap_count += 1
        self.last_bootstrap_ts = time.time()
        n = self.pool.bootstrap(**self._bs_params)
        print(
            f"  [rebootstrap #{self.bootstrap_count}] verified={n} "
            f"pool_left={self.pool.remaining()} dead={len(self.pool.dead)}",
            flush=True,
        )
        return n

    def _maybe_rebootstrap_if_allowed(self, reason: str = "") -> bool:
        """冷却 + 上限内才重拉，避免空池时无限 hammer。"""
        min_interval = float(os.environ.get("BAOSTOCK_REBOOTSTRAP_MIN_INTERVAL", "60"))
        max_count = int(os.environ.get("BAOSTOCK_REBOOTSTRAP_MAX", "8"))
        if self.bootstrap_count >= max_count:
            print(
                f"  [rebootstrap] max={max_count} reached, give up ({reason})",
                flush=True,
            )
            return False
        if time.time() - self.last_bootstrap_ts < min_interval:
            print(
                f"  [rebootstrap] cooldown {min_interval:.0f}s, give up ({reason})",
                flush=True,
            )
            return False
        return self._rebootstrap() > 0

    def maybe_refresh(self, *, force: bool = False) -> bool:
        """常驻通道健康检查：池过薄或老化则补拉（保持当前 login）。

        覆盖 web / `--schedule` 长进程：原实现启动后池只缩不补。
        env BAOSTOCK_MIN_ALIVE(默认 2) / BAOSTOCK_MAX_AGE(默认 1800s)。
        """
        if not self.active or not self._bs_params:
            return False
        min_alive = int(os.environ.get("BAOSTOCK_MIN_ALIVE", "2"))
        max_age = float(os.environ.get("BAOSTOCK_MAX_AGE", "1800"))
        thin = self.pool.remaining() < min_alive
        stale = (time.time() - self.last_bootstrap_ts) > max_age
        if not (force or thin or stale):
            return False
        # 直连兜底期间:代理池回血则尝试切回(直连非长久之计,IP 可能再被封)
        if (self._direct_since_ts > 0
                and self.pool.remaining() >= min_alive
                and (time.time() - self._direct_since_ts) > self._direct_fallback_cooldown):
            print("  [direct-fallback] pool replenished; switch back to proxy",
                  flush=True)
            self._direct_tries = 0
            if self._switch_to_next(reason="recover-from-direct"):
                return True
        print(
            f"  [proxy] refresh (thin={thin} stale={stale} "
            f"alive={self.pool.remaining()})",
            flush=True,
        )
        return self._rebootstrap() > 0

    def _login(self) -> bool:
        """强制 logout+login；``login_timeout`` 秒硬超时（子线程 join）。

        baostock.login 在坏代理上可能 CPU 空转/不响应 socket timeout，
        必须用线程超时兜底，否则整进程卡死。
        """
        from stockfu.data.baostock_source import BaostockSource

        self.logins += 1
        box: dict[str, Any] = {}

        def _worker() -> None:
            _set_raw_login(True)
            try:
                import baostock as bs
                try:
                    bs.logout()
                except Exception:  # noqa: BLE001
                    pass
                BaostockSource._logged_in = False
                lg = bs.login()
                code = str(getattr(lg, "error_code", "1") or "1")
                msg = getattr(lg, "error_msg", "") or ""
                box["code"] = code
                box["msg"] = msg
                box["ok"] = code == "0"
            except Exception as e:  # noqa: BLE001
                box["exc"] = e
                box["ok"] = False
            finally:
                _set_raw_login(False)

        t = threading.Thread(target=_worker, name="baostock-login", daemon=True)
        t0 = time.time()
        t.start()
        t.join(self.login_timeout)
        elapsed = time.time() - t0

        if t.is_alive():
            # 硬超时：关底层 socket，期望 worker 尽快退出；标记失败换代理
            _force_close_baostock_socket()
            BaostockSource._logged_in = False
            print(
                f"  [login TIMEOUT] proxy={self.proxy_url} "
                f">{self.login_timeout}s elapsed={elapsed:.1f}s → drop",
                flush=True,
            )
            return False

        if box.get("exc") is not None:
            BaostockSource._logged_in = False
            e = box["exc"]
            print(f"  [login exc] {type(e).__name__}: {e}", flush=True)
            return False

        ok = bool(box.get("ok"))
        BaostockSource._logged_in = ok
        if ok:
            print(
                f"  [login ok] proxy={self.proxy_url}  {elapsed:.1f}s",
                flush=True,
            )
        else:
            print(
                f"  [login fail] proxy={self.proxy_url} "
                f"code={box.get('code')} msg={box.get('msg')}  {elapsed:.1f}s",
                flush=True,
            )
        return ok

    def mark_bad_and_rotate(self, reason: str) -> bool:
        """当前代理不可用：剔除并切换。返回是否还有可用代理。"""
        cur = self.pool.current
        if cur is not None:
            self.pool.remove(cur, reason=reason)
            self.dropped += 1
        self.pool.disable()
        from stockfu.data.baostock_source import BaostockSource
        BaostockSource._logged_in = False
        try:
            import baostock as bs
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        if self.sleep_after_rotate > 0:
            time.sleep(self.sleep_after_rotate)
        return self._switch_to_next(reason=reason)

    # ----- 判定 -----
    @staticmethod
    def is_fatal_error(exc_or_msg: Any) -> bool:
        text = str(exc_or_msg or "")
        for m in _NET_ERROR_MARKERS:
            if m.lower() in text.lower():
                return True
        # error_code embedded
        for code in _FATAL_LOGIN_CODES:
            if code in text:
                return True
        return False

    def _call_with_timeout(self, fn: Callable[[], T], label: str) -> T:
        """守护线程跑 fn；``fetch_timeout`` 秒不返回即判坏代理：关 socket + 抛错 → run() 换 IP。

        baostock 在坏代理上会卡在内部接收重试循环（狂打"接收数据异常"），
        ``socket_timeout`` 救不了 → 必须线程级硬超时，否则 run() 永远到不了换代理分支。
        """
        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["val"] = fn()
            except Exception as e:  # noqa: BLE001
                box["exc"] = e

        t = threading.Thread(target=_worker, name=f"fetch-{label}"[:40], daemon=True)
        t.start()
        t.join(self.fetch_timeout)
        if t.is_alive():
            # 硬超时：关底层 socket 打断卡住的 recv；标记未登录 → run() 走异常分支换代理
            _force_close_baostock_socket()
            from stockfu.data.baostock_source import BaostockSource
            BaostockSource._logged_in = False
            print(
                f"  [fetch TIMEOUT] {label} >{self.fetch_timeout:.0f}s "
                f"proxy={self.proxy_url} → drop",
                flush=True,
            )
            raise RuntimeError(f"fetch timeout {label} proxy={self.proxy_url}")
        if "exc" in box:
            raise box["exc"]
        return box["val"]  # type: ignore[return-value]

    # ----- 带自动切换的调用 -----
    def run(
        self,
        fn: Callable[[], T],
        *,
        empty_is_fail: bool = False,
        is_empty: Callable[[T], bool] | None = None,
        label: str = "",
    ) -> T:
        """串行执行 fn；失败/空结果则换代理重试。

        empty_is_fail: True 时若结果判空则视为需要切换（用于「必有数据」场景）。
        """
        last_exc: Exception | None = None
        last_val: T | None = None
        for attempt in range(1, self.max_rotate_per_call + 1):
            try:
                val = self._call_with_timeout(fn, label)
                last_val = val
                if empty_is_fail and is_empty and is_empty(val):
                    self.fail_streak += 1
                    reason = f"empty#{self.fail_streak} {label}"
                    if self.fail_streak >= self.max_fail_streak:
                        if not self.mark_bad_and_rotate(reason):
                            return val
                        continue
                    # 先尝试同代理 re-login
                    if not self._login():
                        if not self.mark_bad_and_rotate(reason + "+relogin_fail"):
                            return val
                    continue
                # 成功
                self.fail_streak = 0
                return val
            except Exception as e:  # noqa: BLE001
                last_exc = e
                self.fail_streak += 1
                reason = f"{type(e).__name__}: {e}"
                print(
                    f"  [fetch err] {label} attempt={attempt} {reason}",
                    flush=True,
                )
                if not self.mark_bad_and_rotate(reason[:120]):
                    break
        if last_exc is not None and last_val is None:
            raise last_exc
        return last_val  # type: ignore[return-value]

    def fetch_kline_triple(
        self, code: str, start: str, end: str | None = None,
    ) -> dict[str, list]:
        """拉三复权；空结果 / 异常自动换代理。"""
        from stockfu.data.baostock_source import BaostockSource

        def _once() -> dict[str, list]:
            # session 已注入代理；用 raw login 标志避免再次 bootstrap
            src = BaostockSource()
            _set_raw_login(True)
            try:
                if not BaostockSource._logged_in and not self._login():
                    raise RuntimeError("baostock not logged in")
            finally:
                _set_raw_login(False)
            triple = src.get_kline_triple(code, start, end)
            return triple

        def _empty(t: dict[str, list]) -> bool:
            return not any(t.values())

        return self.run(
            _once,
            empty_is_fail=True,
            is_empty=_empty,
            label=f"triple:{code}",
        )


def make_session_from_env() -> BaostockProxySession:
    """环境变量覆盖默认。

    BAOSTOCK_PROXY_MODE=free|clash|direct
      free   — 免费代理池（默认）+ 可选 clash 种子
      clash  — 仅本机 SOCKS
      direct — 直连
    """
    mode = (os.environ.get("BAOSTOCK_PROXY_MODE") or "free").strip().lower()
    clash_host = os.environ.get("BAOSTOCK_SOCKS_HOST", "127.0.0.1")
    clash_port = int(os.environ.get("BAOSTOCK_SOCKS_PORT", "7891"))
    # 全局路径默认缩小探测规模，避免 --fetch 启动过久
    probe_limit = int(os.environ.get("BAOSTOCK_PROBE_LIMIT", "120"))
    max_per_kind = int(os.environ.get("BAOSTOCK_MAX_PER_KIND", "200"))
    login_timeout = float(os.environ.get("BAOSTOCK_LOGIN_TIMEOUT", "12"))
    max_login_tries = int(os.environ.get("BAOSTOCK_MAX_LOGIN_TRIES", "12"))
    login_workers = int(os.environ.get("BAOSTOCK_LOGIN_WORKERS", "12"))
    login_need = int(os.environ.get("BAOSTOCK_LOGIN_NEED", "3"))
    login_probe_limit = int(os.environ.get("BAOSTOCK_LOGIN_PROBE_LIMIT", "36"))
    common = dict(
        probe_limit=probe_limit,
        max_per_kind=max_per_kind,
        login_timeout=login_timeout,
        max_login_tries=max_login_tries,
        login_workers=login_workers,
        login_need=login_need,
        login_probe_limit=login_probe_limit,
    )
    if mode in ("direct", "none", "off"):
        return BaostockProxySession(
            use_free_pool=False, seed_local_clash=False, **common,
        )
    if mode in ("clash", "socks", "local"):
        return BaostockProxySession(
            use_free_pool=False,
            seed_local_clash=True,
            clash_host=clash_host,
            clash_port=clash_port,
            **common,
        )
    # free (default)
    return BaostockProxySession(
        use_free_pool=True,
        seed_local_clash=True,
        clash_host=clash_host,
        clash_port=clash_port,
        **common,
    )


# ---------------------------------------------------------------------------
# 进程级全局通道：BaostockSource / --fetch / 情绪 / 分红 全部走这里
# ---------------------------------------------------------------------------
import threading

_global_lock = threading.RLock()
_global_session: BaostockProxySession | None = None
# session 内部 raw login 时置位，避免 _ensure_login ↔ ensure_baostock_login 递归
_raw_login_depth: int = 0


def _set_raw_login(on: bool) -> None:
    global _raw_login_depth
    if on:
        _raw_login_depth += 1
    else:
        _raw_login_depth = max(0, _raw_login_depth - 1)


def in_raw_login() -> bool:
    return _raw_login_depth > 0


def get_global_session() -> BaostockProxySession | None:
    return _global_session


def register_session(sess: BaostockProxySession) -> None:
    """backfill 等显式 session 启动时注册为全局（共用同一 socket 注入）。"""
    global _global_session
    with _global_lock:
        _global_session = sess


def ensure_baostock_login(force: bool = False) -> bool:
    """任意 baostock 调用入口：懒启动免费代理池并保证 login。

    - 默认 BAOSTOCK_PROXY_MODE=free
    - force=True：同代理重登；失败则剔除并换下一个
    """
    global _global_session
    from stockfu.data.baostock_source import BaostockSource

    with _global_lock:
        if in_raw_login():
            # 已在 session._login 内部
            return BaostockSource._logged_in

        mode = (os.environ.get("BAOSTOCK_PROXY_MODE") or "free").strip().lower()
        if mode in ("direct", "none", "off"):
            return _direct_login(force=force)

        if _global_session is None or not _global_session.active:
            print(
                f"=== baostock global channel boot mode={mode} ===",
                flush=True,
            )
            sess = make_session_from_env()
            try:
                sess.start()
            except Exception as e:  # noqa: BLE001
                print(f"  [baostock channel] start fail: {e}", flush=True)
                return False
            _global_session = sess
            return BaostockSource._logged_in

        sess = _global_session
        if BaostockSource._logged_in and not force:
            sess.maybe_refresh()  # 常驻通道：池薄/老化则补拉（不动当前 login）
            return True

        # 重登当前代理
        if sess._login():
            return True
        # 当前废了 → 换下一个
        return sess.mark_bad_and_rotate("ensure_relogin_fail")


def rotate_baostock_proxy(reason: str = "query_fail") -> bool:
    """查询失败时由调用方触发：剔除当前并换 IP。"""
    with _global_lock:
        sess = _global_session
        if sess is None or not sess.active:
            return ensure_baostock_login(force=True)
        return sess.mark_bad_and_rotate(reason)


def warm_baostock_channel() -> bool:
    """--fetch 开头预热，避免第一次 PE 查询才拉代理超时。"""
    return ensure_baostock_login(force=False)


def _force_close_baostock_socket() -> None:
    """强关 baostock 全局 socket，打断卡住的 recv/login。"""
    try:
        import baostock.common.context as ctx
        sock = getattr(ctx, "default_socket", None)
        if sock is not None:
            try:
                sock.shutdown(2)
            except Exception:  # noqa: BLE001
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                setattr(ctx, "default_socket", None)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _direct_login(force: bool = False) -> bool:
    """直连模式 login（同样带硬超时）。"""
    from stockfu.data.baostock_source import BaostockSource

    try:
        import baostock as bs  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    if BaostockSource._logged_in and not force:
        return True

    # 复用 session 的线程超时逻辑：临时对象
    tmp = BaostockProxySession(use_free_pool=False, seed_local_clash=False)
    tmp.proxy_url = "direct"
    tmp.login_timeout = float(os.environ.get("BAOSTOCK_LOGIN_TIMEOUT", "15"))
    return tmp._login()
