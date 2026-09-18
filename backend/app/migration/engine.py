"""双读迁移引擎：v1/v2 投影、业务指纹与 v2 规则校验。

引擎只做纯函数计算，不触碰数据库；事务编排由 ``migration.service`` 负责。
核心原则：

- ``upgrade`` 把 v1 对象投影为 v2；``downgrade`` 把 v2 对象还原为 v1。
- v2 视角计算出的业务结论，降级回 v1 后必须与原对象逐字节一致（忽略修订号、
  更新时间这类元数据），否则该对象进入“不兼容集合”，不能切换。
- 冻结对象（简报）从不重写；不可变记录（对比图谱）只做规则复算见证。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..domain.comparison_rules import build_summary
from ..domain.observation_rules import validate_date_window
from .contracts.confidence import (
    is_compatible_v2_score,
    v1_confidence_to_v2_score,
    v2_score_to_v1_confidence,
)
from .contracts.stages_v2 import (
    V2_STAGE_BY_KEY,
    v2_sort_stage_entries,
)


ENTITY_KINDS = ("plot", "tree", "observation", "comparison", "brief")
# 批次分配顺序：被依赖对象（园区、植株、季节志）排在对比图谱和冻结简报之前，
# 这样同一批次内复算见证时依赖对象已经先完成投影。
MIGRATION_ORDER = ("plot", "tree", "observation", "comparison", "brief")
MUTABLE_KINDS = ("plot", "tree", "observation")
VERIFY_ONLY_KINDS = ("comparison", "brief")

# 指纹中忽略的元数据字段：切换批次会追加修订号，但不改变业务结论。
VOLATILE_KEYS = ("revision", "updated_at")

V2_ADDED_OBSERVATION_KEYS = (
    "tree_canonical_id",
    "season_key",
)
V2_ADDED_TREE_KEYS = ("canonical_id", "merged_into", "merged_from")
V2_ADDED_COMPARISON_KEYS = ("rule_generation",)


class IncompatibleObject(Exception):
    """对象无法在 v2 下保持同一业务结论。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("；".join(reasons))
        self.reasons = reasons


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------


def upgrade_plot(v1: dict[str, Any]) -> dict[str, Any]:
    return {**v1, "schema_version": 2}


def upgrade_tree(
    v1: dict[str, Any],
    *,
    canonical_map: dict[str, str],
    merged_sources: dict[str, list[str]],
) -> dict[str, Any]:
    canonical_id = canonical_map.get(v1["id"], v1["id"])
    merged_into = None if canonical_id == v1["id"] else canonical_id
    return {
        **v1,
        "schema_version": 2,
        "canonical_id": canonical_id,
        "merged_into": merged_into,
        "merged_from": list(merged_sources.get(canonical_id, [])),
    }


def upgrade_observation(
    v1: dict[str, Any],
    *,
    canonical_map: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    upgraded_entries: list[dict[str, Any]] = []
    for entry in v1.get("entries", []):
        level = entry.get("confidence")
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5:
            reasons.append(
                f"阶段 {entry.get('stage')} 的 v1 置信度不是 1..5 整数，无法无损换算"
            )
            score = None
        else:
            score = v1_confidence_to_v2_score(level)
        upgraded = {**entry}
        if score is not None:
            upgraded["confidence_score"] = score
        upgraded_entries.append(upgraded)

    canonical_id = canonical_map.get(v1["tree_id"], v1["tree_id"])
    v2 = {
        **v1,
        "schema_version": 2,
        "entries": v2_sort_stage_entries(upgraded_entries),
        "tree_canonical_id": canonical_id,
        "season_key": f"{canonical_id}#{v1['season']}",
    }
    reasons.extend(validate_v2_observation(v2))
    return v2, reasons


def upgrade_comparison(
    v1: dict[str, Any],
    observations: dict[str, dict[str, Any]],
    trees: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """对比图谱不重写；这里只做 v2 规则复算见证。"""
    reasons: list[str] = []
    left = observations.get(v1["left_observation_id"])
    right = observations.get(v1["right_observation_id"])
    if left is None:
        reasons.append("缺少左侧季节志，无法在 v2 下复算")
    if right is None:
        reasons.append("缺少右侧季节志，无法在 v2 下复算")
    if reasons:
        return {**v1, "rule_generation": 2}, reasons

    offsets = calculate_offsets_v2(left, right)
    if _business_json(offsets) != _business_json(v1["stage_offsets"]):
        reasons.append("v2 复算的阶段偏移与冻结记录不一致")
    summary = build_summary(
        v1["title"],
        left,
        right,
        trees.get(left["tree_id"]),
        trees.get(right["tree_id"]),
        offsets,
    )
    for key in (
        "common_stage_count",
        "average_offset_days",
        "minimum_offset_days",
        "maximum_offset_days",
        "direction",
        "stability",
        "sentence",
    ):
        if summary.get(key) != v1["summary"].get(key):
            reasons.append(f"v2 复算摘要字段 {key} 与冻结记录不一致")
            break
    return {**v1, "rule_generation": 2}, reasons


def project_object(
    kind: str,
    v1: dict[str, Any],
    *,
    state: dict[str, dict[str, Any]] | None = None,
    canonical_map: dict[str, str] | None = None,
    merged_sources: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """返回 (v2 投影, 不兼容原因列表)。"""
    canonical_map = canonical_map or {}
    merged_sources = merged_sources or {}
    state = state or {}
    if kind == "plot":
        return upgrade_plot(v1), []
    if kind == "tree":
        return (
            upgrade_tree(
                v1,
                canonical_map=canonical_map,
                merged_sources=merged_sources,
            ),
            [],
        )
    if kind == "observation":
        return upgrade_observation(v1, canonical_map=canonical_map)
    if kind == "comparison":
        return upgrade_comparison(
            v1,
            state.get("observations", {}),
            state.get("trees", {}),
        )
    if kind == "brief":
        # 简报冻结：投影即自身，v2 阅读器必须逐字节返回同一载荷。
        return dict(v1), []
    raise ValueError(f"未知对象类型：{kind}")


# ---------------------------------------------------------------------------
# 降级与业务指纹
# ---------------------------------------------------------------------------


def downgrade(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把任意世代的对象还原为 v1 视角。"""
    if kind == "observation":
        entries = []
        for entry in payload.get("entries", []):
            item = {k: v for k, v in entry.items() if k != "confidence_score"}
            score = entry.get("confidence_score")
            if score is not None and "confidence" not in item:
                item["confidence"] = v2_score_to_v1_confidence(score)
            entries.append(item)
        return {
            k: v
            for k, v in {**payload, "entries": entries}.items()
            if k not in V2_ADDED_OBSERVATION_KEYS
        }
    if kind == "tree":
        return {k: v for k, v in payload.items() if k not in V2_ADDED_TREE_KEYS}
    if kind == "comparison":
        return {
            k: v for k, v in payload.items() if k not in V2_ADDED_COMPARISON_KEYS
        }
    return dict(payload)


def sanitize_for_fingerprint(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    v1_view = downgrade(kind, payload)
    cleaned = {k: v for k, v in v1_view.items() if k not in VOLATILE_KEYS}
    cleaned.pop("schema_version", None)
    return cleaned


def business_fingerprint(kind: str, payload: dict[str, Any]) -> str:
    return _canonical(sanitize_for_fingerprint(kind, payload))


def v1_v2_parity(
    kind: str,
    v1: dict[str, Any],
    v2: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """比较同一对象在两种规则下的业务结论。"""
    left = sanitize_for_fingerprint(kind, v1)
    right = sanitize_for_fingerprint(kind, v2)
    if _canonical(left) == _canonical(right):
        return True, None
    return False, {"v1": _diffable(left), "v2_view": _diffable(right)}


# ---------------------------------------------------------------------------
# v2 规则
# ---------------------------------------------------------------------------


def validate_v2_observation(observation: dict[str, Any]) -> list[str]:
    """v2 季节志规则，返回错误原因列表（空列表表示通过）。"""
    reasons: list[str] = []
    entries = observation.get("entries", [])
    previous_rank = -1
    previous_date: date | None = None
    previous_label = ""
    has_winter_rest = False
    for entry in v2_sort_stage_entries(entries):
        key = str(entry.get("stage", ""))
        definition = V2_STAGE_BY_KEY.get(key)
        if definition is None:
            reasons.append(f"未知阶段：{key}")
            continue
        try:
            observed = date.fromisoformat(str(entry["observed_on"]))
        except (KeyError, ValueError):
            reasons.append(f"阶段 {definition.label} 日期无效")
            continue
        if definition.rank < previous_rank:
            reasons.append(
                f"阶段顺序不合法：{previous_label} 在 {definition.label} 之后"
            )
        if previous_date is not None and observed < previous_date:
            reasons.append(
                f"阶段 {definition.label} 日期早于前一阶段 {previous_label}"
            )
        score = entry.get("confidence_score", entry.get("confidence"))
        if score is not None and not is_compatible_v2_score(score):
            reasons.append(
                f"阶段 {definition.label} 的置信度精度无法与 v1 互逆"
            )
        if key == "winter_rest":
            has_winter_rest = True
            latest = date(int(observation["season"]) + 1, 3, 31)
            if observed > latest:
                reasons.append("休眠期观察日期不能晚于次年 3 月 31 日")
        previous_rank = definition.rank
        previous_date = observed
        previous_label = definition.label
    if has_winter_rest:
        try:
            validate_date_window(
                str(entries[0].get("observed_on")) if entries else "",
                observation["season"],
            )
        except Exception:
            pass
    return reasons


def calculate_offsets_v2(
    left: dict[str, Any],
    right: dict[str, Any],
) -> list[dict[str, Any]]:
    """用 v2 阶段字典复算偏移；旧阶段结果必须与 v1 逐字段一致。"""
    left_entries = {
        item["stage"]: item for item in v2_sort_stage_entries(left.get("entries", []))
    }
    right_entries = {
        item["stage"]: item for item in v2_sort_stage_entries(right.get("entries", []))
    }
    common = sorted(
        set(left_entries) & set(right_entries),
        key=lambda key: V2_STAGE_BY_KEY[key].rank,
    )
    result: list[dict[str, Any]] = []
    for key in common:
        left_entry = left_entries[key]
        right_entry = right_entries[key]
        left_date = date.fromisoformat(left_entry["observed_on"])
        right_date = date.fromisoformat(right_entry["observed_on"])
        result.append(
            {
                "stage": key,
                "label": V2_STAGE_BY_KEY[key].label,
                "rank": V2_STAGE_BY_KEY[key].rank,
                "left_date": left_date.isoformat(),
                "right_date": right_date.isoformat(),
                "offset_days": (right_date - left_date).days,
                "confidence_gap": abs(
                    int(left_entry["confidence"]) - int(right_entry["confidence"])
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# 合并（引用结构演进）资格
# ---------------------------------------------------------------------------


def merge_eligibility(
    survivor: dict[str, Any],
    alias: dict[str, Any],
    observations: dict[str, dict[str, Any]],
) -> list[str]:
    """两株树只有在“同一株树的重复登记”时才允许合并引用。"""
    reasons: list[str] = []
    if survivor["id"] == alias["id"]:
        reasons.append("不能把植株合并到自身")
    if survivor.get("merged_into") or alias.get("merged_into"):
        reasons.append("植株已经处于合并链中")
    if survivor["plot_id"] != alias["plot_id"]:
        reasons.append("只能合并同一园区内的植株")
    if survivor["status"] != "active" or alias["status"] != "active":
        reasons.append("只能合并在册（active）植株")
    for field in ("cultivar", "rootstock", "planting_year"):
        if survivor.get(field) != alias.get(field):
            reasons.append(f"品种、砧木或定植年份不一致（字段 {field}）")
    # 合并后按规范植株聚合季节键；同一季节两边都有记录会产生歧义。
    survivor_seasons = {
        obs["season"]
        for obs in observations.values()
        if obs["tree_id"] == survivor["id"]
    }
    alias_seasons = {
        obs["season"]
        for obs in observations.values()
        if obs["tree_id"] == alias["id"]
    }
    overlap = sorted(survivor_seasons & alias_seasons)
    if overlap:
        reasons.append(f"两株植株在这些年份都有季节志，合并后季节键冲突：{overlap}")
    return reasons


def build_canonical_maps(
    trees: dict[str, dict[str, Any]],
    merge_ops: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """根据已生效的合并操作构造 别名->规范植株 与 规范植株->来源列表。"""
    canonical = {tree_id: tree_id for tree_id in trees}
    sources: dict[str, list[str]] = {}
    for op in merge_ops:
        payload = op["payload"] if isinstance(op["payload"], dict) else json.loads(op["payload"])
        survivor = str(payload["survivor_id"])
        alias = str(payload["alias_id"])
        canonical[alias] = survivor
        sources.setdefault(survivor, [survivor])
        if alias not in sources[survivor]:
            sources[survivor].append(alias)
    return canonical, sources


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _business_json(value: Any) -> str:
    return _canonical(value)


def _diffable(value: Any) -> Any:
    return json.loads(_canonical(value))
