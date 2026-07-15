"""算子库 + 策略 seed(幂等 upsert)。

由 db.init_db() 与 db._ensure_tables()(进程级 guard,首次)调用,确保 operator/strategy
表非空——升级用户 server 启动也能自动补齐,无需重新 --init-db。

策略 YAML 内联于此(DB 为运行时真源);strategies/*.yaml 为可编辑模板(供用户参考/导入)。
数学/汇总算子 params_schema 从类 PARAMS_SCHEMA 抽取。
(回测侧 LLM 算子已下线;实盘 AI 4 顾问走 ai/skills 独立链路,不经此 seed。)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from sqlmodel import select

from stockfu.models import Operator, Strategy

log = logging.getLogger(__name__)

_OP_NAMES = {
    "momentum": "动量", "mean_reversion": "均值回归", "value": "���值",
    "trend_strength": "趋势强度", "weighted_sum": "加权汇总", "risk_veto": "风险一票否决",
    "macd_cross": "MACD金叉死叉",
    "monthly_bollinger": "月线布林带",
    "weekly_bollinger": "周线布林带",
}

# 策略清单: strategy_id。name + config 从 strategies/{id}.yaml 读(单一真源)。
# 注意:active 由 app_config('active_strategy_id') 单 key 指针决定,此处不再设默认活跃。
_STRATEGIES = [
    "pure_factor",
    "macd_cross",
    "momentum_breakout",
    "dual_bollinger",
    "bollinger_reversion",
]

_STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


def _load_strategy_yaml(strategy_id: str) -> tuple[str, str]:
    """从 strategies/{id}.yaml 读 (name, yaml_text)。文件是策略单一真源。"""
    text = (_STRATEGIES_DIR / f"{strategy_id}.yaml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(text) or {}
    return cfg.get("name", strategy_id), text


def seed_operators_and_strategies() -> int:
    """幂等 upsert 算子库 + 策略,返回处理行数。"""
    from stockfu.db import session_scope
    from stockfu.ai.operators import REGISTRY, discover_and_register

    discover_and_register()
    # 清理已下线的回测 LLM 算子 + 依赖它的策略(operator 表删 type='llm' 后,
    # 其 operator_result 孤儿由下方 cleanup_operator_results 自动清)。
    _cleanup_legacy_llm()
    n = 0
    with session_scope() as s:
        # 数学 + 汇总算子: params_schema 从类 PARAMS_SCHEMA
        for op_id, cls in REGISTRY.items():
            _upsert_operator(
                s, operator_id=op_id, name=_OP_NAMES.get(op_id, op_id), type=cls.type,
                module=cls.__module__, params_schema=getattr(cls, "PARAMS_SCHEMA", {}),
                prompt="", constitution_ref="",
            )
            n += 1
        # 策略(从 strategies/*.yaml 读,name+config ���自文件单一真源)
        for sid in _STRATEGIES:
            name, yaml_text = _load_strategy_yaml(sid)
            _upsert_strategy(s, sid, name, yaml_text)
            n += 1
        s.commit()

        # 一致性校验:DB operator 表里的 id 若不在注册表 → 残留告警。
        # 注册表是运行真源;seed 已把注册表算子全 upsert 进 DB,
        # 故只可能"DB 有 / 注册表无"(旧算子删除后残留),反向不会发生。
        db_ids = set(s.exec(select(Operator.operator_id)).all())
        orphan_ops = db_ids - set(REGISTRY.keys())
        if orphan_ops:
            log.warning("operator 表有 %d 个算子不在注册表(残留待清理): %s",
                        len(orphan_ops), sorted(orphan_ops))

    # active 指针:首次 seed 写默认值(pure_factor,首个纯 math 策略)。
    # 单 key 物理保证唯一 active(取代旧的 strategy.is_active 列;该列已移除)��
    from stockfu.db import has_app_config, set_app_config
    if not has_app_config("active_strategy_id"):
        set_app_config("active_strategy_id", "pure_factor")
    # 仓位调整层(独立于策略,复刻 active_strategy_id 模式)
    if not has_app_config("active_rebalancer_id"):
        set_app_config("active_rebalancer_id", "pass_through")
    if not has_app_config("rebalancer_params"):
        # max_gross: 总仓安全阀(engine 层对所有 rebalancer 生效),默认留 10% 现金;
        # max_w: top_n_picker 单仓上限(对齐策略层 max_w=0.10)。
        set_app_config("rebalancer_params", json.dumps({"max_gross": 0.90, "max_w": 0.10}))

    # 清理 operator_result 孤儿缓存(算子已不在 operator 表的历史缓存)
    cleaned = cleanup_operator_results()
    if cleaned:
        log.info("清理 operator_result 孤儿缓存 %d 行", cleaned)
    return n


def _cleanup_legacy_llm() -> None:
    """清理已下线的回测 LLM 算子 + 依赖它的策略(幂等)。

    回测侧 LLM 算子(operators/llm/)下线后,operator 表的 4 行 LLM(trend/contrarian/risk/
    valuation)与 strategy 表的 classic_4advisors/hybrid 成为残留。本函数显式删除,
    并把指向它们的 active_strategy_id 指针拨回 pure_factor。operator_result 里这些
    算子的历史缓存随后由 cleanup_operator_results() 按 operator_id 孤儿规则清掉。
    """
    from sqlalchemy import text

    from stockfu.db import engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM operator WHERE type = 'llm'"))
        conn.execute(text(
            "DELETE FROM strategy WHERE strategy_id IN ('classic_4advisors', 'hybrid')"
        ))
        conn.execute(text(
            "UPDATE app_config SET value = 'pure_factor' "
            "WHERE key = 'active_strategy_id' "
            "AND value IN ('classic_4advisors', 'hybrid')"
        ))


def cleanup_operator_results() -> int:
    """删除 operator_result 里 operator_id 已不在 operator 表的孤儿缓存(幂等)。

    算子从注册表移除 → 不再被 seed upsert → 不在 operator 表 → 其历史缓存成孤儿。
    active 策略用到的算子必在 operator 表,不会被误删(回测结果不变,仅清空间)。

    注:此 DELETE 仅按 operator_id 过滤(无 asset_code 前导),复合唯一键用不上 →
    走全表扫(5.6M 行,数秒~十几秒)。这是罕见 init 期维护操作,可接受;
    operator_id 单列索引已作为冗余删除(复合键覆盖全部热路径查询)。
    """
    from sqlalchemy import text

    from stockfu.db import engine
    with engine.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM operator_result "
            "WHERE operator_id NOT IN (SELECT operator_id FROM operator)"
        ))
        return result.rowcount or 0


def _upsert_operator(s, *, operator_id, name, type, module, params_schema,
                     prompt, constitution_ref) -> None:
    existing = s.get(Operator, operator_id)
    ps_json = json.dumps(params_schema, ensure_ascii=False) if params_schema else ""
    if existing:
        existing.name = name
        existing.type = type
        existing.module = module
        existing.params_schema = ps_json
        existing.constitution_ref = constitution_ref
        # 不覆盖 prompt(保留用户热改)与 active
    else:
        s.add(Operator(
            operator_id=operator_id, name=name, type=type, module=module,
            params_schema=ps_json, prompt=prompt, constitution_ref=constitution_ref,
            active=True,
        ))


def _upsert_strategy(s, sid, name, yaml_text) -> None:
    """首次从 yaml 写入;已存在则完全保留 DB(config+name),让用户热改/active 指针生效。active 由 app_config 决定。

    改 strategies/*.yaml 想重新同步:删该 strategy 行后重启,或重新 --init-db。
    """
    if s.get(Strategy, sid):
        return
    s.add(Strategy(strategy_id=sid, name=name, config=yaml_text))
