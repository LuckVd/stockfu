"""公网免费代理池（HTTP CONNECT / SOCKS4 / SOCKS5）— 给 baostock 裸 TCP 用。

baostock 连 ``public-api.baostock.com:10030``，不认 HTTP_PROXY。
HTTP 代理经 PySocks ``socks.HTTP``（CONNECT 隧道）可承载该 TCP。

流程：
  1. 启动时从公开列表拉取候选
  2. **并发** TCP 隧道探测 → baostock:10030
  3. **并发** 子进程真实 baostock.login 校验（通过才进可用池）
  4. 确认后再 enable + 拉数；失败立即剔除并换下一个已校验 IP
"""
from __future__ import annotations

import random
import socket
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Literal
from urllib.request import Request, urlopen

ProxyKind = Literal["http", "socks4", "socks5"]

BAOSTOCK_HOST = "public-api.baostock.com"
BAOSTOCK_PORT = 10030

_DEFAULT_SOURCES: dict[ProxyKind, list[str]] = {
    "http": [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=8000&country=all&ssl=all&anonymity=all",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    ],
    "socks5": [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=8000&country=all",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ],
    "socks4": [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=8000&country=all",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    ],
}

# 仅补丁 baostock 建连（禁止全局 socket.socket 劫持，否则东财/腾讯也走免费代理）
_orig_bs_connect = None
_orig_bs_get_socket = None
_active_proxy: ProxyEndpoint | None = None
_active_timeout: float = 12.0
_socket_lock = threading.RLock()


@dataclass(frozen=True)
class ProxyEndpoint:
    kind: ProxyKind
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"{self.kind}://{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.url


@dataclass
class FreeProxyPool:
    """内存代理池：候选 → TCP 探测 → 可用队列；失败剔除。"""

    candidates: list[ProxyEndpoint] = field(default_factory=list)
    alive: list[ProxyEndpoint] = field(default_factory=list)
    dead: set[str] = field(default_factory=set)  # url 集合
    current: ProxyEndpoint | None = None
    socket_timeout: float = 12.0

    # ----- 拉取 -----
    def fetch(
        self,
        kinds: Iterable[ProxyKind] = ("http", "socks5", "socks4"),
        *,
        per_source_timeout: float = 12.0,
        max_per_kind: int = 300,
    ) -> int:
        seen: set[tuple[str, str, int]] = set()
        out: list[ProxyEndpoint] = []
        for kind in kinds:
            n_kind = 0
            for url in _DEFAULT_SOURCES.get(kind, []):
                if n_kind >= max_per_kind:
                    break
                try:
                    body = _http_get(url, timeout=per_source_timeout)
                except Exception as e:  # noqa: BLE001
                    print(
                        f"  [proxy-fetch fail] {kind} {url[:56]}… "
                        f"{type(e).__name__}: {e}",
                        flush=True,
                    )
                    continue
                for line in body.splitlines():
                    ep = _parse_line(line, kind)
                    if not ep or ep.url in self.dead:
                        continue
                    key = (ep.kind, ep.host, ep.port)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(ep)
                    n_kind += 1
                    if n_kind >= max_per_kind:
                        break
            print(f"  [proxy-fetch] {kind}: +{n_kind}", flush=True)
        random.shuffle(out)
        self.candidates = out
        return len(out)

    def add_seed(self, *endpoints: ProxyEndpoint) -> None:
        """预置种子（如本机 Clash SOCKS），插到候选最前。"""
        for ep in reversed(endpoints):
            if ep.url in self.dead:
                continue
            self.candidates = [ep] + [c for c in self.candidates if c.url != ep.url]

    # ----- 探测 -----
    def probe_tcp(
        self,
        *,
        limit: int | None = 180,
        workers: int = 50,
        timeout: float = 5.0,
        target_host: str = BAOSTOCK_HOST,
        target_port: int = BAOSTOCK_PORT,
        progress_every: int = 40,
    ) -> int:
        pool = [c for c in self.candidates if c.url not in self.dead]
        if limit is not None:
            pool = pool[:limit]
        if not pool:
            self.alive = []
            return 0
        alive: list[ProxyEndpoint] = []
        t0 = time.time()
        done = 0
        print(
            f"=== proxy probe n={len(pool)} workers={workers} "
            f"timeout={timeout}s → {target_host}:{target_port} ===",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_tcp_tunnel_ok, ep, target_host, target_port, timeout): ep
                for ep in pool
            }
            for fut in as_completed(futs):
                ep = futs[fut]
                done += 1
                try:
                    ok, ms = fut.result()
                except Exception:  # noqa: BLE001
                    ok, ms = False, 0.0
                if ok:
                    alive.append(ep)
                    print(f"  ✓ TCP {ep}  {ms:.0f}ms", flush=True)
                if progress_every and done % progress_every == 0:
                    print(
                        f"  … {done}/{len(pool)} tcp_ok={len(alive)} "
                        f"{time.time() - t0:.1f}s",
                        flush=True,
                    )
        # 暂存 TCP 通的，login 校验后再写入 self.alive
        random.shuffle(alive)
        self.alive = alive
        print(
            f"=== proxy TCP probe done tcp_ok={len(alive)}/{len(pool)} "
            f"{time.time() - t0:.1f}s ===",
            flush=True,
        )
        return len(alive)

    def probe_baostock_login(
        self,
        *,
        limit: int | None = 36,
        workers: int = 12,
        login_timeout: float = 12.0,
        need: int = 3,
    ) -> int:
        """对 TCP 通的代理 **并发** 做 baostock.login 实检（子进程隔离）。

        只有 login 成功的才进入 self.alive（按耗时升序，快的优先）。
        need: 凑够这么多个已确认即可提前结束等待剩余任务（仍会收已完成的）。
        """
        pool = [c for c in self.alive if c.url not in self.dead]
        if limit is not None:
            pool = pool[:limit]
        if not pool:
            self.alive = []
            return 0

        # 本机 clash 等种子优先测
        args_list = [
            (ep.kind, ep.host, ep.port, float(login_timeout)) for ep in pool
        ]
        verified: list[tuple[float, ProxyEndpoint]] = []
        t0 = time.time()
        print(
            f"=== proxy baostock.login 并发校验 n={len(pool)} "
            f"workers={workers} timeout={login_timeout}s need>={need} ===",
            flush=True,
        )
        # 进程池：每个子进程独立 baostock 全局状态，可真并发 login
        ex = ProcessPoolExecutor(max_workers=max(1, min(workers, len(pool))))
        try:
            futs = {
                ex.submit(_mp_baostock_login_probe, a): a for a in args_list
            }
            done = 0
            for fut in as_completed(futs):
                done += 1
                kind, host, port, _to = futs[fut]
                ep = ProxyEndpoint(kind=kind, host=host, port=int(port))  # type: ignore[arg-type]
                try:
                    ok, ms, err = fut.result()
                except Exception as e:  # noqa: BLE001
                    ok, ms, err = False, 0.0, f"{type(e).__name__}: {e}"
                if ok:
                    verified.append((ms, ep))
                    print(
                        f"  ✓ LOGIN {ep}  {ms:.0f}ms  "
                        f"({len(verified)} ok / {done} done)",
                        flush=True,
                    )
                else:
                    self.dead.add(ep.url)
                    if done <= 8 or done % 10 == 0:
                        print(
                            f"  ✗ LOGIN {ep}  {ms:.0f}ms  {err}",
                            flush=True,
                        )
                # 已够用：不再等剩余（shutdown wait=False）
                if len(verified) >= need:
                    print(
                        f"  [proxy] need={need} met, stop waiting more",
                        flush=True,
                    )
                    break
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)

        verified.sort(key=lambda x: x[0])
        self.alive = [ep for _, ep in verified]
        if verified:
            print(
                f"=== proxy login 校验完成 verified={len(self.alive)} "
                f"in {time.time() - t0:.1f}s fastest={verified[0][0]:.0f}ms ===",
                flush=True,
            )
        else:
            print(
                f"=== proxy login 校验完成 verified=0 "
                f"in {time.time() - t0:.1f}s ===",
                flush=True,
            )
        return len(self.alive)

    def bootstrap(
        self,
        *,
        kinds: Iterable[ProxyKind] = ("http", "socks5", "socks4"),
        max_per_kind: int = 300,
        probe_limit: int = 180,
        workers: int = 50,
        tcp_timeout: float = 5.0,
        seeds: list[ProxyEndpoint] | None = None,
        login_verify: bool = True,
        login_workers: int = 12,
        login_timeout: float = 12.0,
        login_probe_limit: int = 36,
        login_need: int = 3,
    ) -> int:
        """拉列表 → 并发 TCP →（可选）并发 baostock.login 校验。

        返回 **已确认可 login** 的代理数（login_verify=True 时）。
        """
        print("=== free proxy pool bootstrap ===", flush=True)
        self.fetch(kinds=kinds, max_per_kind=max_per_kind)
        if seeds:
            self.add_seed(*seeds)
        n_tcp = self.probe_tcp(
            limit=probe_limit, workers=workers, timeout=tcp_timeout,
        )
        if n_tcp == 0:
            return 0
        if not login_verify:
            return n_tcp
        return self.probe_baostock_login(
            limit=login_probe_limit,
            workers=login_workers,
            login_timeout=login_timeout,
            need=login_need,
        )

    # ----- 队列操作 -----
    def pop(self) -> ProxyEndpoint | None:
        """取下一个可用代理（不自动 enable）。"""
        while self.alive:
            ep = self.alive.pop(0)
            if ep.url in self.dead:
                continue
            self.current = ep
            return ep
        self.current = None
        return None

    def remove(self, ep: ProxyEndpoint | None, reason: str = "") -> None:
        """立即剔除不可用 IP。"""
        if ep is None:
            return
        self.dead.add(ep.url)
        self.alive = [a for a in self.alive if a.url != ep.url]
        if self.current and self.current.url == ep.url:
            self.current = None
        msg = f"  ✗ drop proxy {ep}"
        if reason:
            msg += f"  ({reason})"
        msg += f"  remaining={len(self.alive)}"
        print(msg, flush=True)

    def remaining(self) -> int:
        return len(self.alive)

    # ----- 仅 baostock 走代理 -----
    def enable(self, ep: ProxyEndpoint) -> str:
        """只改 baostock 建连路径走代理，**不**劫持全局 socket.socket。

        否则 requests/东财/腾讯也会走免费代理 → 行情抓取大面积超时。
        baostock 登录后复用 default_socket，后续 query 仍经该隧道。
        """
        with _socket_lock:
            _install_baostock_proxy(ep, self.socket_timeout)
            self.current = ep
            return ep.url

    def disable(self) -> None:
        with _socket_lock:
            _restore_baostock_proxy()
            self.current = None


def local_clash_socks(
    host: str = "127.0.0.1", port: int = 7891,
) -> ProxyEndpoint | None:
    """若本机 Clash/mihomo SOCKS 端口在听，返回种子端点。"""
    try:
        s = socket.create_connection((host, port), timeout=0.5)
        s.close()
        return ProxyEndpoint(kind="socks5", host=host, port=port)
    except OSError:
        return None


def _http_get(url: str, timeout: float = 12.0) -> str:
    req = Request(url, headers={"User-Agent": "stockfu-proxy-pool/1.1"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="ignore")


def _parse_line(line: str, default_kind: ProxyKind) -> ProxyEndpoint | None:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    kind = default_kind
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower().strip()
        if scheme in ("http", "https"):
            kind = "http"
        elif scheme in ("socks5", "socks5h"):
            kind = "socks5"
        elif scheme in ("socks4", "socks4a"):
            kind = "socks4"
        else:
            return None
        line = rest
    if "@" in line:
        line = line.rsplit("@", 1)[-1]
    if line.count(":") != 1:
        return None
    host, port_s = line.split(":", 1)
    host = host.strip().strip("[]")
    try:
        port = int(port_s.strip())
    except ValueError:
        return None
    if not host or not (1 <= port <= 65535):
        return None
    return ProxyEndpoint(kind=kind, host=host, port=port)


def _tcp_tunnel_ok(
    ep: ProxyEndpoint,
    target_host: str,
    target_port: int,
    timeout: float,
) -> tuple[bool, float]:
    t0 = time.time()
    try:
        import socks

        s = socks.socksocket()
        s.set_proxy(_socks_type(ep.kind), ep.host, ep.port)
        s.settimeout(timeout)
        s.connect((target_host, target_port))
        s.close()
        return True, (time.time() - t0) * 1000
    except Exception:  # noqa: BLE001
        return False, (time.time() - t0) * 1000


def _socks_type(kind: ProxyKind):
    import socks

    return {
        "http": socks.HTTP,
        "socks4": socks.SOCKS4,
        "socks5": socks.SOCKS5,
    }[kind]


def _open_proxied_tcp(
    ep: ProxyEndpoint, host: str, port: int, timeout: float,
):
    """经 HTTP CONNECT / SOCKS 建到 host:port 的 TCP。"""
    import socks

    s = socks.socksocket()
    s.set_proxy(_socks_type(ep.kind), ep.host, ep.port)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


def _mp_baostock_login_probe(args: tuple) -> tuple[bool, float, str]:
    """子进程：经代理 login baostock，返回 (ok, ms, err)。

    必须是顶层函数以便 ProcessPoolExecutor pickle。
    args = (kind, host, port, login_timeout)
    """
    kind, host, port, login_timeout = args
    t0 = time.time()
    try:
        import baostock as bs
        import baostock.common.contants as cons
        import baostock.common.context as ctx
        import baostock.util.socketutil as su
        import socks

        ep = ProxyEndpoint(kind=kind, host=host, port=int(port))  # type: ignore[arg-type]
        timeout = float(login_timeout)

        def _connect(self):  # noqa: ANN001
            try:
                sock = socks.socksocket()
                sock.set_proxy(_socks_type(ep.kind), ep.host, ep.port)
                sock.settimeout(timeout)
                sock.connect((cons.BAOSTOCK_SERVER_IP, cons.BAOSTOCK_SERVER_PORT))
                setattr(ctx, "default_socket", sock)
            except Exception as e:  # noqa: BLE001
                setattr(ctx, "default_socket", None)
                raise e

        su.SocketUtil.connect = _connect  # type: ignore[method-assign]

        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        lg = bs.login()
        code = str(getattr(lg, "error_code", "1") or "1")
        msg = str(getattr(lg, "error_msg", "") or "")
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
        ms = (time.time() - t0) * 1000
        if code == "0":
            return True, ms, "ok"
        return False, ms, f"code={code} {msg}"
    except Exception as e:  # noqa: BLE001
        return False, (time.time() - t0) * 1000, f"{type(e).__name__}: {e}"


def _install_baostock_proxy(ep: ProxyEndpoint, timeout: float) -> None:
    """Monkeypatch baostock.util.socketutil 的 connect / get_default_socket。"""
    global _orig_bs_connect, _orig_bs_get_socket, _active_proxy, _active_timeout

    import baostock.common.contants as cons
    import baostock.common.context as ctx
    import baostock.util.socketutil as su

    _active_proxy = ep
    _active_timeout = timeout

    if _orig_bs_connect is None:
        _orig_bs_connect = su.SocketUtil.connect
        _orig_bs_get_socket = su.get_default_socket

    def _connect(self):  # noqa: ANN001
        try:
            sock = _open_proxied_tcp(
                ep, cons.BAOSTOCK_SERVER_IP, cons.BAOSTOCK_SERVER_PORT, timeout,
            )
            setattr(ctx, "default_socket", sock)
        except Exception:  # noqa: BLE001
            print("服务器连接失败，请稍后再试。")
            setattr(ctx, "default_socket", None)

    def _get_default_socket():
        try:
            return _open_proxied_tcp(
                ep, cons.BAOSTOCK_SERVER_IP, cons.BAOSTOCK_SERVER_PORT, timeout,
            )
        except Exception:  # noqa: BLE001
            print("服务器连接失败，请稍后再试。")
            return None

    su.SocketUtil.connect = _connect  # type: ignore[method-assign]
    su.get_default_socket = _get_default_socket  # type: ignore[assignment]


def _restore_baostock_proxy() -> None:
    global _orig_bs_connect, _orig_bs_get_socket, _active_proxy
    try:
        import baostock.util.socketutil as su
        if _orig_bs_connect is not None:
            su.SocketUtil.connect = _orig_bs_connect  # type: ignore[method-assign]
            su.get_default_socket = _orig_bs_get_socket  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass
    _orig_bs_connect = None
    _orig_bs_get_socket = None
    _active_proxy = None
