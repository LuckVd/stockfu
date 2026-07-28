"""可恢复的逐项回补 checkpoint。

行情覆盖本身足以判定完成的任务（例如三复权）应继续使用其数据级校验；本模块
用于“每项均需联网但没有自然完成标志”的长任务。每项成功立即提交，进程中断后
只会重试失败或未处理项。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlmodel import select

from stockfu.db import session_scope
from stockfu.models import BackfillCheckpoint


def pending_items(
    task_key: str, scope_key: str, items: Iterable[str], *, refresh: bool = False,
) -> tuple[list[str], int]:
    """返回待处理项和已跳过成功项数。refresh=True 强制重跑全部项。"""
    ordered = list(dict.fromkeys(str(item) for item in items))
    if refresh or not ordered:
        return ordered, 0
    with session_scope() as s:
        rows = s.exec(select(BackfillCheckpoint.item_key).where(
            BackfillCheckpoint.task_key == task_key,
            BackfillCheckpoint.scope_key == scope_key,
            BackfillCheckpoint.status == "success",
        )).all()
    complete = set(rows)
    pending = [item for item in ordered if item not in complete]
    return pending, len(ordered) - len(pending)


def mark_item(
    task_key: str, scope_key: str, item_key: str, *, success: bool,
    error: str = "",
) -> None:
    """把单项结果立即落盘；失败保留以便下次自动重试。"""
    with session_scope() as s:
        row = s.exec(select(BackfillCheckpoint).where(
            BackfillCheckpoint.task_key == task_key,
            BackfillCheckpoint.scope_key == scope_key,
            BackfillCheckpoint.item_key == item_key,
        )).first()
        if row is None:
            row = BackfillCheckpoint(
                task_key=task_key, scope_key=scope_key, item_key=item_key,
            )
            s.add(row)
        row.status = "success" if success else "failed"
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = "" if success else error[:1000]
        row.updated_at = datetime.now()
        s.commit()


def checkpoint_summary(task_key: str, scope_key: str) -> dict[str, int]:
    with session_scope() as s:
        rows = s.exec(select(BackfillCheckpoint.status).where(
            BackfillCheckpoint.task_key == task_key,
            BackfillCheckpoint.scope_key == scope_key,
        )).all()
    return {"success": sum(status == "success" for status in rows),
            "failed": sum(status == "failed" for status in rows)}
