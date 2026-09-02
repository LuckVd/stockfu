"""run_baostock_query 跨线程重入不得自死锁（2026-09-01 修复回归）。

真实结构：fetch_kline_triple → sess.run() 持 _QUERY_LOCK 后在 worker 线程执行
fn；fn 内 _klines_range → run_baostock_query → 若再次 sess.run()，worker 会在
RLock 上永久阻塞（外层持锁者是主线程，RLock 仅同线程可重入），join 超时表现
为每只票 20s/60s 超时的假性网络故障（2026-08-17 引入，行情停在 8/14 的根因）。
本测试用 FakeSess 复刻「持锁 + worker」结构，断言嵌套调用限时完成。
"""
import threading

from stockfu.data import baostock_proxy as bp


class FakeSess:
    """复刻 BaostockProxySession.run 的关键结构：持 _QUERY_LOCK + worker 线程。"""

    active = True

    def __init__(self):
        self._in_run_worker = False
        self.entered_run = 0

    def run(self, fn, *, label="", deadline=None):
        self.entered_run += 1
        with bp._QUERY_LOCK:                      # 外层（主线程）持锁
            self._in_run_worker = True
            try:
                box: dict = {}
                err: dict = {}

                def _worker():
                    try:
                        box["v"] = fn()
                    except Exception as e:  # noqa: BLE001
                        err["e"] = e

                t = threading.Thread(target=_worker)
                t.start()
                t.join(10)                        # 修复前：worker 卡锁 → 超时
                if t.is_alive():
                    raise RuntimeError("deadlock: worker 未在限时内完成")
                if err:
                    raise err["e"]
                return box.get("v")
            finally:
                self._in_run_worker = False


def test_nested_run_baostock_query_no_deadlock(monkeypatch):
    sess = FakeSess()
    monkeypatch.setattr(bp, "get_global_session", lambda: sess)

    def nested_call():
        # worker 线程内再次走 run_baostock_query（修复前在此自死锁）
        return bp.run_baostock_query(lambda: 41 + 1, ensure_login=False)

    assert bp.run_baostock_query(nested_call, ensure_login=False) == 42
    # 只有外层进入 run；重入必须走直连执行路径
    assert sess.entered_run == 1


def test_worker_flag_reset_after_exception(monkeypatch):
    sess = FakeSess()
    monkeypatch.setattr(bp, "get_global_session", lambda: sess)

    def nested_boom():
        return bp.run_baostock_query(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            ensure_login=False,
        )

    try:
        bp.run_baostock_query(nested_boom, ensure_login=False)
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("应向上传播 worker 异常")
    assert sess._in_run_worker is False, "finally 未复位重入标志"
