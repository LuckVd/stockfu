"""正式回测的固定样本区间定义。

研究回测必须同时保留：全量基线、2013--2019 子样本和 2020--2026
子样本。这里的 ``eval_end`` 写成 2026-12-31 是有意的：引擎会根据
数据快照的实际末日截断，并在 manifest.data_coverage 中披露实际终点。
这样未来补齐当年数据时，区间身份不需要改名，旧产物也不会被覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable


BACKTEST_DATA_START = date(2013, 1, 1)
BACKTEST_DATA_END = date(2026, 12, 31)
DEFAULT_OBSERVATION_COUNT = 271


@dataclass(frozen=True)
class BacktestSegment:
    """一个独立的 formal 回测请求区间。"""

    segment_id: str
    label: str
    eval_start: date
    eval_end: date

    def history_origin(self, *, years: int = 5) -> date:
        """返回不早于当前研究数据起点的独立预热起点。"""
        try:
            candidate = self.eval_start.replace(year=self.eval_start.year - years)
        except ValueError:
            # 仅覆盖 2 月 29 日，避免 replace 在闰年边界抛错。
            candidate = self.eval_start.replace(
                year=self.eval_start.year - years, day=self.eval_start.day - 1
            )
        return max(candidate, BACKTEST_DATA_START)

    def to_dict(self) -> dict[str, str]:
        return {
            "segment_id": self.segment_id,
            "label": self.label,
            "eval_start": self.eval_start.isoformat(),
            "eval_end": self.eval_end.isoformat(),
            "history_origin": self.history_origin().isoformat(),
        }


FULL_SEGMENT = BacktestSegment(
    "full", "全量", BACKTEST_DATA_START, BACKTEST_DATA_END
)
EARLY_SEGMENT = BacktestSegment(
    "2013-2019", "2013–2019", date(2013, 1, 1), date(2019, 12, 31)
)
RECENT_SEGMENT = BacktestSegment(
    "2020-2026", "2020–2026", date(2020, 1, 1), BACKTEST_DATA_END
)

FORMAL_BACKTEST_SEGMENTS: tuple[BacktestSegment, ...] = (
    FULL_SEGMENT,
    EARLY_SEGMENT,
    RECENT_SEGMENT,
)
_SEGMENTS_BY_ID = {segment.segment_id: segment for segment in FORMAL_BACKTEST_SEGMENTS}


def resolve_segments(selection: str | Iterable[str] | None = None) -> tuple[BacktestSegment, ...]:
    """解析区间选择；省略或 ``all`` 必须返回三段且保持固定顺序。"""
    if selection is None:
        return FORMAL_BACKTEST_SEGMENTS
    if isinstance(selection, str):
        raw = selection.strip()
        if not raw or raw.lower() in {"all", "全部", "三段"}:
            return FORMAL_BACKTEST_SEGMENTS
        names = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        names = [str(part).strip() for part in selection if str(part).strip()]
    if not names:
        return FORMAL_BACKTEST_SEGMENTS

    unknown = sorted(set(names) - set(_SEGMENTS_BY_ID))
    if unknown:
        raise ValueError(
            f"未知回测区间: {unknown}; 可选: {', '.join(_SEGMENTS_BY_ID)} 或 all"
        )
    selected = set(names)
    return tuple(segment for segment in FORMAL_BACKTEST_SEGMENTS if segment.segment_id in selected)


def is_complete_segment_set(segments: Iterable[BacktestSegment]) -> bool:
    """判断选择是否覆盖正式三段，供 canonical 编排门禁使用。"""
    return {segment.segment_id for segment in segments} == set(_SEGMENTS_BY_ID)
