"""新规则（v2）：新增休眠芽阶段、半级置信度精度、细化权限与规范引用。

这是迁移的“右半边”。四类规则演进都在这里声明，且每一类都必须保证：
对任何只使用旧能力的对象，v2 推出的业务结论与 v1 完全一致。

1. 阶段定义：在落瓣期与坐果期之间加入可选阶段 ``dormant_bud``（休眠芽期）。
   它不是完成必需阶段，rank=55，落在 50 与 60 之间，因此原有阶段的相对
   顺序和日期约束不变。
2. 字段精度：置信度由 1..5 的整数扩展为 1.0..5.0、步长 0.5。旧整数值全部
   是新刻度的合法值，折算到统一刻度后结论相同；共同阶段置信度差按新精度
   保留一位小数。
3. 权限范围：引入 ``plot/<id>/tree/<id>`` 形式的层级资源范围，旧的 ``*``
   与扁平资源范围继续被承认，双读阶段对每一条授权决定求等式。
4. 引用结构：植株合并后以“规范树”为引用目标，成员树标识通过引用映射归一；
   季节志、比较和简报按规范树归一后业务结论不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .phenology_v1 import V1_LABELS, V1_ORDER, V1_REQUIRED
from .semantics import StageCatalog


RULESET_VERSION = 2
CHANGE_SET_ID = "phenology-v2-stage-precision-scope-reference"

# 新增阶段插在 petal_fall(50) 与 fruit_set(60) 之间。
V2_ORDER: dict[str, int] = {
    **{key: rank for key, rank in V1_ORDER.items() if rank <= 50},
    "dormant_bud": 55,
    **{key: rank for key, rank in V1_ORDER.items() if rank >= 60},
}
V2_LABELS = {
    **V1_LABELS,
    "dormant_bud": "休眠芽期",
}
# 休眠芽期为可选阶段，完成必需阶段集合保持不变。
V2_REQUIRED = V1_REQUIRED

V2_CONFIDENCE_SCALE = 2.0

V2_CATALOG = StageCatalog(
    order=dict(V2_ORDER),
    labels=dict(V2_LABELS),
    required=V2_REQUIRED,
)

from .phenology_v1 import RuleSet  # noqa: E402

V2_RULESET = RuleSet(
    version=2,
    catalog=V2_CATALOG,
    confidence_scale=V2_CONFIDENCE_SCALE,
    confidence_min=1.0,
    confidence_max=5.0,
    confidence_step=0.5,
    stage_keys=frozenset(V2_ORDER),
)


@dataclass(frozen=True, slots=True)
class StageChange:
    key: str
    label: str
    rank: int
    required_for_completion: bool


@dataclass(frozen=True, slots=True)
class PrecisionChange:
    field_path: str
    old_step: float
    new_step: float
    minimum: float
    maximum: float
    preserves: str


@dataclass(frozen=True, slots=True)
class ScopeChange:
    syntax: str
    legacy_syntax: str
    decision_guarantee: str


@dataclass(frozen=True, slots=True)
class ReferenceChange:
    kind: str
    canonical_form: str
    member_form: str
    guarantee: str


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_set_id: str
    from_version: int
    to_version: int
    description: str
    stages: tuple[StageChange, ...]
    precision: tuple[PrecisionChange, ...]
    scopes: tuple[ScopeChange, ...]
    references: tuple[ReferenceChange, ...]
    affected_kinds: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, Any]:
        return {
            "change_set_id": self.change_set_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "description": self.description,
            "stages": [
                {
                    "key": item.key,
                    "label": item.label,
                    "rank": item.rank,
                    "required_for_completion": item.required_for_completion,
                }
                for item in self.stages
            ],
            "precision": [
                {
                    "field_path": item.field_path,
                    "old_step": item.old_step,
                    "new_step": item.new_step,
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                    "preserves": item.preserves,
                }
                for item in self.precision
            ],
            "scopes": [
                {
                    "syntax": item.syntax,
                    "legacy_syntax": item.legacy_syntax,
                    "decision_guarantee": item.decision_guarantee,
                }
                for item in self.scopes
            ],
            "references": [
                {
                    "kind": item.kind,
                    "canonical_form": item.canonical_form,
                    "member_form": item.member_form,
                    "guarantee": item.guarantee,
                }
                for item in self.references
            ],
        }


V2_CHANGE_SET = ChangeSet(
    change_set_id=CHANGE_SET_ID,
    from_version=1,
    to_version=2,
    description=(
        "新增可选休眠芽期；置信度扩展为 0.5 步长；授权支持层级资源范围；"
        "植株引用支持规范树合并。所有旧能力对象的业务结论保持不变。"
    ),
    stages=(
        StageChange(
            key="dormant_bud",
            label="休眠芽期",
            rank=55,
            required_for_completion=False,
        ),
    ),
    precision=(
        PrecisionChange(
            field_path="observation.entries[].confidence",
            old_step=1.0,
            new_step=0.5,
            minimum=1.0,
            maximum=5.0,
            preserves="旧整数 1..5 在新刻度中全部合法且折算值相同",
        ),
    ),
    scopes=(
        ScopeChange(
            syntax="plot/<plot_id>/tree/<tree_id>",
            legacy_syntax="* 或扁平 resource_id",
            decision_guarantee=(
                "旧授权在 v2 评估器下得到完全相同的允许/拒绝决定"
            ),
        ),
    ),
    references=(
        ReferenceChange(
            kind="tree",
            canonical_form="tree(canonical_id)，merged_from 记录成员",
            member_form="tree(member_id) 经 reference_map 归一到 canonical",
            guarantee=(
                "成员树季节志、比较与简报按规范树归一后业务结论不变"
            ),
        ),
    ),
    affected_kinds=("plot", "tree", "observation", "comparison", "brief"),
)
