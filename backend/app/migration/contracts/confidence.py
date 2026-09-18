"""v1/v2 置信度精度换算。

v1 使用 1..5 的整数等级，v2 使用 0.00..1.00 的两位小数。映射必须可逆：
只有能无损往返回同一 v1 整数等级的对象，才被认为兼容切换。
"""

from __future__ import annotations

from typing import Any

# v1 等级 -> v2 两位小数分值（均匀映射到 0.0/0.25/0.5/0.75/1.0）。
V1_TO_V2_CONFIDENCE: dict[int, float] = {
    1: 0.00,
    2: 0.25,
    3: 0.50,
    4: 0.75,
    5: 1.00,
}
# 以“百分位整数”为键，避免浮点比较误差：0.25 -> 25。
_SCORE_POINTS_TO_V1: dict[int, int] = {
    round(score * 100): level for level, score in V1_TO_V2_CONFIDENCE.items()
}

COMPATIBLE_V2_SCORE_POINTS = frozenset(_SCORE_POINTS_TO_V1)


def v1_confidence_to_v2_score(level: Any) -> float:
    """把 v1 整数等级换算为 v2 分值。"""
    level_int = int(level)
    if level_int not in V1_TO_V2_CONFIDENCE:
        raise ValueError(f"v1 置信度等级越界：{level!r}")
    return V1_TO_V2_CONFIDENCE[level_int]


def is_compatible_v2_score(score: Any) -> bool:
    """v2 分值是否落在可逆映射点上。"""
    try:
        points = score_points(score)
    except (TypeError, ValueError):
        return False
    return points in COMPATIBLE_V2_SCORE_POINTS


def v2_score_to_v1_confidence(score: Any) -> int:
    """把可逆映射点上的 v2 分值还原为 v1 等级。"""
    points = score_points(score)
    if points not in _SCORE_POINTS_TO_V1:
        raise ValueError(f"v2 置信度无法无损还原为 v1 等级：{score!r}")
    return _SCORE_POINTS_TO_V1[points]


def score_points(score: Any) -> int:
    """把 0.00..1.00 的分值规范化为两位小数的百分位整数。"""
    value = float(score)
    if value < 0 or value > 1:
        raise ValueError(f"v2 置信度超出 0..1：{score!r}")
    points = round(value * 100)
    if abs(value * 100 - points) > 1e-9:
        raise ValueError(f"v2 置信度最多两位小数：{score!r}")
    return points
