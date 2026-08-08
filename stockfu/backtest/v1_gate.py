"""V1 回测入口闸门。

V1 策略代码和历史产物仍保留用于兼容读取；新回测统一走 V2。
"""

V1_BACKTEST_ENABLED = False
V1_BACKTEST_DISABLED_MESSAGE = (
    "V1 回测引擎已禁用；请使用 `python3 main.py --backtest-v2 ALPHA_ID`"
)


def ensure_v1_backtest_enabled() -> None:
    """拒绝启动新的 V1 回测，避免误用旧口径。"""
    if not V1_BACKTEST_ENABLED:
        raise RuntimeError(V1_BACKTEST_DISABLED_MESSAGE)
