"""旧规则（v1）：现网固定九阶段、整数置信度。

这是迁移的“左半边”。它以纯函数方式描述现网规则，既用于双读比较，
也用于历史时点复算，不能在迁移期间被改动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .semantics import StageCatalog


RULESET_VERSION = 1

# v1：九个固定阶段，rank 间隔 10。
V1_ORDER: dict[str, int] = {
    "bud_swell": 10,
    "bud_burst": 20,
    "first_bloom": 30,
    "full_bloom": 40,
    "petal_fall": 50,
    "fruit_set": 60,
    "fruit_growth": 70,
    "harvest": 80,
    "leaf_fall": 90,
}
V1_LABELS: dict[str, str] = {
    "bud_swell": "芽膨大期",
    "bud_burst": "萌芽期",
    "first_bloom": "初花期",
    "full_bloom": "盛花期",
    "petal_fall": "落瓣期",
    "fruit_set": "坐果期",
    "fruit_growth": "果实膨大期",
    "harvest": "采收期",
    "leaf_fall": "落叶期",
}
V1_REQUIRED = frozenset(
    {"bud_burst", "full_bloom", "fruit_set", "harvest"}
)

# 旧整数刻度 1..5，折算到统一刻度时乘 2（见 semantics.entry_conclusion）。
V1_CONFIDENCE_SCALE = 2.0


@dataclass(frozen=True, slots=True)
class RuleSet:
    version: int
    catalog: StageCatalog
    confidence_scale: float
    confidence_min: float
    confidence_max: float
    confidence_step: float
    stage_keys: frozenset[str]

    def is_valid_confidence(self, value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if number < self.confidence_min or number > self.confidence_max:
            return False
        scaled = round(number / self.confidence_step)
        return abs(number - scaled * self.confidence_step) < 1e-9


V1_CATALOG = StageCatalog(
    order=dict(V1_ORDER),
    labels=dict(V1_LABELS),
    required=V1_REQUIRED,
)

V1_RULESET = RuleSet(
    version=1,
    catalog=V1_CATALOG,
    confidence_scale=V1_CONFIDENCE_SCALE,
    confidence_min=1.0,
    confidence_max=5.0,
    confidence_step=1.0,
    stage_keys=frozenset(V1_ORDER),
)
