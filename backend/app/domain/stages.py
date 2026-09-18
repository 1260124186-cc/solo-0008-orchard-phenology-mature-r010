"""固定物候阶段定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    label: str
    rank: int
    required_for_completion: bool


STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("bud_swell", "芽膨大期", 10, False),
    StageDefinition("bud_burst", "萌芽期", 20, True),
    StageDefinition("first_bloom", "初花期", 30, False),
    StageDefinition("full_bloom", "盛花期", 40, True),
    StageDefinition("petal_fall", "落瓣期", 50, False),
    StageDefinition("fruit_set", "坐果期", 60, True),
    StageDefinition("fruit_growth", "果实膨大期", 70, False),
    StageDefinition("harvest", "采收期", 80, True),
    StageDefinition("leaf_fall", "落叶期", 90, False),
)

STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


def active_stages() -> tuple[StageDefinition, ...]:
    """返回当前生效世代的阶段字典（第 2 代新增休眠期）。"""
    from ..migration.runtime import RUNTIME

    if RUNTIME.generation >= 2:
        from ..migration.contracts.stages_v2 import V2_STAGES

        return tuple(
            StageDefinition(
                item.key,
                item.label,
                item.rank,
                item.required_for_completion,
            )
            for item in V2_STAGES
        )
    return STAGES


def stage_rank(key: str) -> int | None:
    from ..migration.runtime import RUNTIME

    if RUNTIME.generation >= 2:
        from ..migration.contracts.stages_v2 import V2_STAGE_BY_KEY

        definition = V2_STAGE_BY_KEY.get(key)
        return definition.rank if definition else None
    definition = STAGE_BY_KEY.get(key)
    return definition.rank if definition else None


def stage_label(key: str) -> str:
    from ..migration.runtime import RUNTIME

    label = RUNTIME.stage_label(key)
    if label is not None:
        return label
    return key


def stage_definition(key: str) -> StageDefinition:
    normalized = str(key or "").strip().lower()
    from ..migration.runtime import RUNTIME

    if RUNTIME.generation >= 2:
        from ..migration.contracts.stages_v2 import (
            V2_STAGE_BY_KEY,
            v2_stage_definition,
        )

        if normalized in V2_STAGE_BY_KEY:
            definition = V2_STAGE_BY_KEY[normalized]
            return StageDefinition(
                definition.key,
                definition.label,
                definition.rank,
                definition.required_for_completion,
            )
        return v2_stage_definition(normalized)  # 抛出带 v2 字典的校验错误
    try:
        return STAGE_BY_KEY[normalized]
    except KeyError as exc:
        raise ValidationError(
            "未知的物候阶段",
            field_name="stage",
            details={"stage": key, "allowed": list(STAGE_BY_KEY)},
        ) from exc


def sort_stage_entries(entries: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda item: (
            stage_rank(str(item.get("stage"))) or 10 ** 9,
            str(item.get("observed_on", "")),
        ),
    )


def required_stage_keys() -> set[str]:
    return {stage.key for stage in active_stages() if stage.required_for_completion}


def stage_labels() -> dict[str, str]:
    return {stage.key: stage.label for stage in active_stages()}
