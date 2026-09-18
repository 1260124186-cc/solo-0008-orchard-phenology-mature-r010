"""双读引擎：同一对象在旧规则与新规则下必须得到同一业务结论。

职责：
- 把 v1 载荷升级为 v2、再降级回 v1，做往返等价检查；
- 对单对象与整库分别计算 v1/v2 业务指纹；
- 识别不兼容对象（非法精度、新阶段出现在旧对象等），它们不进入切换；
- 对授权决定做双读等式；
- 应用引用（规范树）映射并保证归一日然。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .phenology_v1 import V1_RULESET
from .phenology_v2 import V2_CHANGE_SET, V2_RULESET
from .semantics import (
    business_state_fingerprint,
    comparison_conclusion,
    fingerprint,
    observation_conclusion,
    brief_conclusion,
)


CONTAINER_FOR_KIND = {
    "plot": "plots",
    "tree": "trees",
    "observation": "observations",
    "comparison": "comparisons",
    "brief": "briefs",
}
KIND_FOR_CONTAINER = {value: key for key, value in CONTAINER_FOR_KIND.items()}


@dataclass(slots=True)
class ObjectRead:
    kind: str
    object_id: str
    compatible: bool
    legacy_fingerprint: str
    new_fingerprint: str
    reasons: list[str] = field(default_factory=list)
    upgraded_payload: dict[str, Any] | None = None

    @property
    def business_equal(self) -> bool:
        return self.compatible and self.legacy_fingerprint == self.new_fingerprint


class DualReadEngine:
    """按变更集执行 v1/v2 双向投影与比较。"""

    def __init__(self, change_set: ChangeSetLike = V2_CHANGE_SET) -> None:
        self.change_set = change_set
        self.legacy = V1_RULESET
        self.new = V2_RULESET

    # ------------------------------------------------------------------
    # 单对象
    # ------------------------------------------------------------------
    def read_object(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> ObjectRead:
        reference_map = reference_map or {}
        reasons: list[str] = []
        upgraded = self.upgrade_payload(kind, payload, reasons=reasons)

        # 两侧使用同一引用映射：旧载荷与新投影在“是否为同一业务对象”的
        # 归一口径下比较。未合并对象的映射为空，行为与逐字相等完全一致；
        # 成员树则两侧都归一到规范树，结论一致。
        legacy_fp = self.fingerprint_object(
            kind,
            payload,
            ruleset=self.legacy,
            reference_map=reference_map,
        )
        new_fp = self.fingerprint_object(
            kind,
            upgraded,
            ruleset=self.new,
            reference_map=reference_map,
        )
        compatible = not reasons
        if compatible and legacy_fp != new_fp:
            reasons.append("新旧规则业务指纹不一致")
        return ObjectRead(
            kind=kind,
            object_id=str(payload.get("id")),
            compatible=compatible and legacy_fp == new_fp,
            legacy_fingerprint=legacy_fp,
            new_fingerprint=new_fp,
            reasons=sorted(set(reasons)),
            upgraded_payload=upgraded,
        )

    def fingerprint_object(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        ruleset: Any,
        reference_map: dict[str, str] | None = None,
    ) -> str:
        reference_map = reference_map or {}
        catalog = ruleset.catalog
        scale = ruleset.confidence_scale
        if kind == "observation":
            fact = observation_conclusion(
                payload,
                catalog=catalog,
                confidence_scale=scale,
            )
        elif kind == "comparison":
            fact = comparison_conclusion(
                payload,
                catalog=catalog,
                confidence_scale=scale,
                observations_by_id={},
                reference_map=reference_map,
            )
        elif kind == "brief":
            fact = brief_conclusion(
                payload,
                catalog=catalog,
                confidence_scale=scale,
                reference_map=reference_map,
            )
        elif kind == "tree":
            fact = self._tree_fact(payload, reference_map=reference_map)
        elif kind == "plot":
            fact = self._plot_fact(payload)
        else:
            fact = payload
        return fingerprint(fact)

    @staticmethod
    def _tree_fact(
        tree: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        reference_map = reference_map or {}
        tree_id = str(tree.get("id"))
        return {
            # 成员树经引用映射归一到规范树：合并前后“同一业务植株”结论一致。
            "id": reference_map.get(tree_id, tree_id),
            "plot_id": tree.get("plot_id"),
            "code": tree.get("code"),
            "cultivar": tree.get("cultivar"),
            "planting_year": tree.get("planting_year"),
            "status": tree.get("status"),
            "merged_from": sorted(tree.get("merged_from", [])) or None,
        }

    @staticmethod
    def _plot_fact(plot: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": plot.get("id"),
            "code": plot.get("code"),
            "status": plot.get("status"),
            "planting_year": plot.get("planting_year"),
        }

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------
    def upgrade_payload(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        reasons = reasons if reasons is not None else []
        upgraded = copy.deepcopy(payload)
        upgraded["schema_version"] = self.new.version
        if kind == "observation":
            for entry in upgraded.get("entries", []):
                stage = str(entry.get("stage"))
                if stage not in self.new.catalog.order:
                    reasons.append(f"条目使用了 v2 未知阶段：{stage}")
                confidence = entry.get("confidence")
                upgraded_confidence = _as_v2_confidence(
                    confidence,
                    ruleset=self.new,
                    reasons=reasons,
                )
                if upgraded_confidence is not None:
                    entry["confidence"] = upgraded_confidence
        elif kind == "comparison":
            for item in upgraded.get("stage_offsets", []):
                stage = str(item.get("stage"))
                if stage not in self.new.catalog.order:
                    reasons.append(f"比较偏移使用了 v2 未知阶段：{stage}")
                gap = item.get("confidence_gap")
                if gap is not None:
                    item["confidence_gap"] = round(float(gap) / 2.0, 1)
        elif kind == "brief":
            for observation in upgraded.get("observations", []):
                for entry in observation.get("entries", []):
                    stage = str(entry.get("stage"))
                    if stage not in self.new.catalog.order:
                        reasons.append(f"简报冻结条目使用了 v2 未知阶段：{stage}")
                    confidence = entry.get("confidence")
                    upgraded_confidence = _as_v2_confidence(
                        confidence,
                        ruleset=self.new,
                        reasons=reasons,
                    )
                    if upgraded_confidence is not None:
                        entry["confidence"] = upgraded_confidence
        return upgraded

    def downgrade_payload(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        """v2 -> v1。凡含 v2-only 事实（半级、休眠芽、合并成员）都不可降级，
        必须保留在新规则下并登记原因。"""

        reasons = reasons if reasons is not None else []
        downgraded = copy.deepcopy(payload)
        downgraded["schema_version"] = self.legacy.version
        if kind == "observation":
            for entry in downgraded.get("entries", []):
                stage = str(entry.get("stage"))
                if stage not in self.legacy.catalog.order:
                    reasons.append(f"含 v1 不具备的新阶段：{stage}")
                confidence = entry.get("confidence")
                if confidence is not None:
                    number = float(confidence)
                    if not self.legacy.is_valid_confidence(number):
                        reasons.append(
                            f"含 v1 无法表达的半级置信度：{number}"
                        )
                    entry["confidence"] = int(number)
        elif kind == "comparison":
            for item in downgraded.get("stage_offsets", []):
                if str(item.get("stage")) not in self.legacy.catalog.order:
                    reasons.append("比较包含 v1 不具备的新阶段")
                gap = item.get("confidence_gap")
                if gap is not None:
                    restored = round(float(gap) * 2.0)
                    if abs(restored - float(gap) * 2.0) > 1e-9:
                        reasons.append("比较置信度差无法还原为旧整数")
                    item["confidence_gap"] = int(restored)
        elif kind == "brief":
            for observation in downgraded.get("observations", []):
                for entry in observation.get("entries", []):
                    if str(entry.get("stage")) not in self.legacy.catalog.order:
                        reasons.append("简报冻结了 v1 不具备的新阶段")
                    confidence = entry.get("confidence")
                    if confidence is not None and not self.legacy.is_valid_confidence(
                        float(confidence)
                    ):
                        reasons.append("简报冻结了 v1 无法表达的半级置信度")
        elif kind == "tree":
            if downgraded.get("merged_from"):
                reasons.append("规范树是 v2-only 引用结构")
        return downgraded

    def round_trip_equal(
        self,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        upgraded = self.upgrade_payload(kind, payload, reasons=reasons)
        downgraded = self.downgrade_payload(kind, upgraded, reasons=reasons)
        original = {key: value for key, value in payload.items() if key != "schema_version"}
        restored = {
            key: value for key, value in downgraded.items() if key != "schema_version"
        }
        # 数值按数值比较（4 与 4.0 是同一业务值），其余按规范化 JSON 比较。
        if not _loose_equal(original, restored):
            reasons.append("v1→v2→v1 往返后载荷不一致")
        return not reasons, reasons

    # ------------------------------------------------------------------
    # 整库
    # ------------------------------------------------------------------
    def upgrade_state(
        self,
        state: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        reference_map = reference_map or {}
        projected = {
            "revision": state.get("revision", 0),
            "schema_version": self.new.version,
            "plots": {},
            "trees": {},
            "observations": {},
            "comparisons": {},
            "briefs": {},
            "events": list(state.get("events", [])),
        }
        for container in ("plots", "trees", "observations", "comparisons", "briefs"):
            kind = KIND_FOR_CONTAINER[container]
            for identifier, payload in state.get(container, {}).items():
                projected[container][identifier] = self.upgrade_payload(kind, payload)
        return projected

    def state_fingerprints(
        self,
        state: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """同时给出旧规则与新规则对整库的业务指纹。"""

        reference_map = reference_map or {}
        legacy = business_state_fingerprint(
            state,
            catalog=self.legacy.catalog,
            confidence_scale=self.legacy.confidence_scale,
            reference_map={},
        )
        upgraded = self.upgrade_state(state, reference_map=reference_map)
        new = business_state_fingerprint(
            upgraded,
            catalog=self.new.catalog,
            confidence_scale=self.new.confidence_scale,
            reference_map=reference_map,
        )
        return {
            "legacy": fingerprint(legacy),
            "new": fingerprint(new),
            "legacy_by_kind": legacy,
            "new_by_kind": new,
        }

    def compare_states(
        self,
        legacy_state: dict[str, Any],
        new_state: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        reference_map = reference_map or {}
        legacy = business_state_fingerprint(
            legacy_state,
            catalog=self.legacy.catalog,
            confidence_scale=self.legacy.confidence_scale,
        )
        new = business_state_fingerprint(
            new_state,
            catalog=self.new.catalog,
            confidence_scale=self.new.confidence_scale,
            reference_map=reference_map,
        )
        mismatches = sorted(
            kind
            for kind in ("observations", "comparisons", "briefs")
            if legacy.get(kind) != new.get(kind)
        )
        return {
            "equal": not mismatches,
            "mismatched_kinds": mismatches,
            "legacy": legacy,
            "new": new,
        }


def _as_v2_confidence(
    value: Any,
    *,
    ruleset: Any,
    reasons: list[str],
) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        reasons.append("置信度不是数值")
        return None
    # v1 整数 -> v2 等价值（4 -> 4.0）。
    if not ruleset.is_valid_confidence(number):
        reasons.append(f"置信度不在 v2 合法刻度上：{value}")
    return round(number, 1)


def _loose_equal(left: Any, right: Any) -> bool:
    """数值容忍的递归相等：4 与 4.0 视为相同，字典/列表按结构比较。"""

    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(_loose_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_loose_equal(a, b) for a, b in zip(left, right))
    return left == right


ChangeSetLike = Any
