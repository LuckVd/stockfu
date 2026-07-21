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

    pool: FreeProxyPool = field(default_factory=FreeProxyPool)
    active: bool = False
    proxy_url: str = "direct"
    fail_streak: int = 0
    rotates: int = 0
    dropped: int = 0
    logins: int = 0

    def __enter__(self) -> "BaostockProxySession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ----- 生命周期 -----
    def start(self) -> str:
        """并发确认 baostock 代理 → 再 enable 最快可用 IP 并 login → 才允许拉数。"""
        self.pool.socket_timeout = self.socket_timeout
        seeds: list[ProxyEndpoint] = []
        if self.seed_local_clash:
            seed = local_clash_socks(self.clash_host, self.clash_port)
            if seed:
                seeds.append(seed)
                print(f"  [proxy] seed local clash {seed}", flush=True)

        if self.use_free_pool:
            n = self.pool.bootstrap(
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
                self.proxy_url = "none"
                return False
            ep = self.pool.pop()
            if ep is None:
                self.proxy_url = "none"
                return False
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
                return True
            # login 失败：剔除当前，继续下一个
            self.pool.remove(ep, reason="login_fail")
            self.dropped += 1
            self.pool.disable()
            _force_close_baostock_socket()
            if self.sleep_after_rotate > 0:
                time.sleep(self.sleep_after_rotate)

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
                val = fn()
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
