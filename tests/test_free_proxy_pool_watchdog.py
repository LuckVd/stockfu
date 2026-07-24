"""回归：baostock login probe 的进程级硬看门狗。

背景：坏代理会让 baostock.login() 卡在内部"接收数据异常"重试循环，
sock.settimeout 只管单段 socket、管不住库内部循环，worker 永久卡死；
ProcessPoolExecutor.shutdown(wait=False) 丢弃后变孤儿空转烧 CPU（实测一晚
20+ 小时）。看门狗到 wall-clock deadline 用 os._exit 强杀自己。

单进程内用 fake baostock + stub os._exit 复现：卡死时看门狗到点必触发，
正常返回时绝不触发。
"""
import sys
import threading
import time
import types

import pytest

import stockfu.data.free_proxy_pool as fpp


class _FakeSock:
    def set_proxy(self, *a, **k): ...
    def settimeout(self, t): ...
    def connect(self, addr): ...
    def close(self): ...


def _install_fake_baostock(login_impl) -> None:
    """注入一组假的 baostock/socks 模块到 sys.modules，login 走 login_impl。"""
    bs = types.ModuleType("baostock")
    bs.login = login_impl
    bs.logout = lambda: None

    cons = types.ModuleType("baostock.common.contants")
    cons.BAOSTOCK_SERVER_IP = "1.2.3.4"
    cons.BAOSTOCK_SERVER_PORT = 10030

    ctx = types.ModuleType("baostock.common.context")
    ctx.default_socket = None

    su = types.ModuleType("baostock.util.socketutil")

    class _SocketUtil:
        connect = staticmethod(lambda *a, **k: None)

    su.SocketUtil = _SocketUtil

    socks = types.ModuleType("socks")
    socks.socksocket = _FakeSock

    for name, mod in {
        "baostock": bs,
        "baostock.common": types.ModuleType("baostock.common"),
        "baostock.common.contants": cons,
        "baostock.common.context": ctx,
        "baostock.util": types.ModuleType("baostock.util"),
        "baostock.util.socketutil": su,
        "socks": socks,
    }.items():
        sys.modules[name] = mod


@pytest.fixture
def restore_modules():
    snap = dict(sys.modules)
    yield
    sys.modules.clear()
    sys.modules.update(snap)


def test_watchdog_kills_stuck_login(monkeypatch, restore_modules):
    """login 永久阻塞时，看门狗到 deadline 调 os._exit(2)。"""
    monkeypatch.setattr(fpp, "_LOGIN_PROBE_WALL_FLOOR", 0.5)
    exit_calls: list[int] = []
    fired = threading.Event()
    monkeypatch.setattr(fpp.os, "_exit", lambda code=0: (exit_calls.append(code), fired.set()))

    def stuck_login():
        # 模拟 baostock 内部重试循环永不返回；os._exit 触发后退出(代替进程被杀)
        while not fired.is_set():
            time.sleep(0.02)
        raise SystemExit("watchdog fired")

    _install_fake_baostock(stuck_login)

    t0 = time.time()
    # SystemExit 是 BaseException，不被 probe 的 except Exception 捕获，向上冒泡
    with pytest.raises(SystemExit):
        fpp._mp_baostock_login_probe(("socks5", "h", 1, 0.0))
    elapsed = time.time() - t0

    assert exit_calls, "os._exit 未被调用(看门狗未触发)"
    assert all(c == 2 for c in exit_calls), exit_calls
    assert elapsed < 3.0, f"看门狗触发过晚: {elapsed:.1f}s"


def test_watchdog_not_fired_on_normal_login(monkeypatch, restore_modules):
    """login 正常返回时，finally 取消看门狗，绝不调 os._exit。"""
    monkeypatch.setattr(fpp, "_LOGIN_PROBE_WALL_FLOOR", 0.3)
    exit_calls: list[int] = []
    monkeypatch.setattr(fpp.os, "_exit", lambda code=0: exit_calls.append(code))

    def ok_login():
        class _R:
            error_code = "0"
            error_msg = ""

        return _R()

    _install_fake_baostock(ok_login)

    ok, _ms, _err = fpp._mp_baostock_login_probe(("socks5", "h", 1, 0.1))

    assert ok is True
    assert exit_calls == [], f"正常路径不应触发 os._exit: {exit_calls}"
