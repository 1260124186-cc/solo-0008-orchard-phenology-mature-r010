"""v2 物候阶段定义。

相对 v1（``domain.stages``）的领域演进：

1. 新增非必需阶段 ``winter_rest``（休眠期），rank 100，位于 ``leaf_fall`` 之后。
   旧阶段的 key、中文标签与 rank 全部保持不变，因此任何只引用旧阶段的业务
   结论都不会变化。
2. 置信度由 1..5 的整数精度，演进为 0.00..1.00 的两位小数精度。v1 的整数值
   按确定性映射换算到新尺度；无法无损往返的对象进入“不兼容集合”，不允许切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ...errors import ValidationError
from ...domain.stages import STAGES as V1_STAGES, StageDefinition


@dataclass(frozen=True, slots=True)
class V2StageDefinition:
    key: str
    label: str
    rank: int
    required_for_completion: bool


# 前九个阶段与 v1 完全一致；休眠期是新增的非必需阶段。
V2_STAGES: tuple[V2StageDefinition, ...] = (
    *(
        V2StageDefinition(
            stage.key,
            stage.label,
            stage.rank,
            stage.required_for_completion,
        )
        for stage in V1_STAGES
    ),
    V2StageDefinition("winter_rest", "休眠期", 100, False),
)

V2_STAGE_BY_KEY = {stage.key: stage for stage in V2_STAGES}

WINTER_REST_KEY = "winter_rest"
WINTER_REST_RANK = 100

V1_STAGE_KEYS = frozenset(stage.key for stage in V1_STAGES)
V2_STAGE_KEYS = frozenset(V2_STAGE_BY_KEY)


def v2_stage_definition(key: str) -> V2StageDefinition:
    normalized = str(key or "").strip().lower()
    try:
        return V2_STAGE_BY_KEY[normalized]
    except KeyError as exc:
        raise ValidationError(
            "未知的物候阶段",
            field_name="stage",
            details={"stage": key, "allowed": sorted(V2_STAGE_BY_KEY)},
        ) from exc


def v2_sort_stage_entries(
    entries: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda item: (
            V2_STAGE_BY_KEY.get(
                str(item.get("stage")),
                V2_STAGES[-1],
            ).rank,
            str(item.get("observed_on", "")),
        ),
    )


def v2_required_stage_keys() -> set[str]:
    return {stage.key for stage in V2_STAGES if stage.required_for_completion}
