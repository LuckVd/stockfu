"""Baostock 任意查询必须复用全局代理会话的完整容错闭环。"""
from types import SimpleNamespace

import pytest

from stockfu.data import baostock_proxy as proxy


class _Session:
    active = True

    def __init__(self):
        self.labels: list[str] = []

    def run(self, fn, *, label="", **_kwargs):
        self.labels.append(label)
        return fn()


def test_query_uses_active_proxy_session_run(monkeypatch):
    session = _Session()
    monkeypatch.setattr(proxy, "ensure_baostock_login", lambda: True)
    monkeypatch.setattr(proxy, "get_global_session", lambda: session)

    result = proxy.run_baostock_query(
        lambda: SimpleNamespace(error_code="0", error_msg="", value=7), label="dividend:600519:2025"
    )

    assert result.value == 7
    assert session.labels == ["dividend:600519:2025"]


def test_error_response_becomes_session_retry_signal(monkeypatch):
    session = _Session()
    monkeypatch.setattr(proxy, "ensure_baostock_login", lambda: True)
    monkeypatch.setattr(proxy, "get_global_session", lambda: session)

    with pytest.raises(RuntimeError, match="10002004"):
        proxy.run_baostock_query(
            lambda: SimpleNamespace(error_code="10002004", error_msg="接收数据异常"),
            label="dividend:600519:2025",
        )

    # 真正的 BaostockProxySession.run 会捕获这个异常并剔除/轮换代理。
    assert session.labels == ["dividend:600519:2025"]
