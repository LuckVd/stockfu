"""仓位调整层(Rebalancer)基类:跨标的、看当前组合全集 → 每个标的的最终目标仓位。

定位:和算子层(operators/)、策略层(strategies/)平级的**基础架构层**,非策略附属。
配置走 app_config(active_rebalancer_id + rebalancer_params),不进策略 YAML。

与算子的区别(不复用算子 REGISTRY / 不缓存):
  - 算子 = per-(code,as_of) 纯市场函数 → 可缓存进 operator_result。
  - rebalancer = 跨标的 + 依赖每日变化的 current → 非 per-(code,as_of) 纯函数
    → 不进缓存、不进 operator 表。本体仍是纯函数(desired+current+meta → final)。

全集语义:输入覆盖 desired ∪ current;输出 final 必须覆盖同一全集。
「current 有 desired 无」(策略未覆盖的持仓)维持还是清仓,**框架不规定** —— 各方案自定。
默认实现 PassThrough/CapAndRank 都 → None(维持,不主动清)。
"""
from __future__ import annotations

from typing import Any


class Rebalancer:
    """仓位调整层基类。子类设 rebalancer_id 并实现 adjust。"""
    rebalancer_id: str = ""

    def adjust(self,
               desired: dict[str, float | None],
               current: dict[str, float],
               meta: dict[str, dict[str, Any]],
               equity: float,
               params: dict) -> dict[str, float | None]:
        """desired ∪ current 全集 → 每个标的的最终目标仓位(交给 PositionManager.should_act)。

        desired: 策略层给的目标仓位(None=维持),可能含当前未持仓 code(新建仓)
        current: 当前持仓全集 {code: 占总资产比例},可能含策略未覆盖 code
        meta:    {code: {score, confidence, signal, risk_vetoed}}(排序/决策用)
        equity:  当前总资产(备用,如绝对现金底线)
        params:  app_config rebalancer_params 解析出的参数(如 max_gross)
        返回:    {code: 最终目标仓位或 None},覆盖 desired ∪ current 全集
        """
        raise NotImplementedError(f"{type(self).__name__}.adjust 未实现")
