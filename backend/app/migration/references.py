"""引用结构演进：规范树合并与引用映射。

合并把若干成员植株归并到一棵规范树：
- 只有同一园区、且没有同树同年季节志冲突的成员可以合并；
- 引用映射 ``member_id -> canonical_id`` 让既有季节志、比较、简报
  在新规则下按规范树归一，但成员对象在旧规则视图中仍然保留；
- 已冻结比较/简报的业务结论按规范树归一后保持不变（由双读验证）；
- 规范树是 v2-only 结构，回滚时若已有事实依赖它则必须保留并登记原因。
"""

from __future__ import annotations

from typing import Any

from ..errors import ConflictError, PreconditionError, ValidationError


def validate_merge(
    *,
    canonical: dict[str, Any],
    members: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    if not members:
        raise ValidationError("合并至少需要一棵成员植株", field_name="member_ids")
    target_plot = str(canonical["plot_id"])
    for member in members:
        if member["id"] == canonical["id"]:
            raise ValidationError("规范树不能同时作为成员", field_name="member_ids")
        if member["plot_id"] != target_plot:
            raise PreconditionError(
                "merge_cross_plot",
                "只能合并同一园区内的植株",
                member_id=member["id"],
            )
    # 同一规范树不允许出现同树同年的多份季节志（否则合并会改变唯一约束语义）。
    by_tree: dict[str, list[dict[str, Any]]] = {}
    all_tree_ids = {canonical["id"], *(item["id"] for item in members)}
    for observation in observations:
        if observation["tree_id"] in all_tree_ids:
            by_tree.setdefault(observation["tree_id"], []).append(observation)
    seen_seasons: dict[str, str] = {}
    for tree_id, records in by_tree.items():
        for record in records:
            season = record["season"]
            owner = seen_seasons.get(season)
            if owner is not None and owner != tree_id:
                raise ConflictError(
                    "merge_season_conflict",
                    "成员植株在同一年份都有季节志，合并且改变唯一性结论",
                    season=season,
                    conflicting_trees=sorted([owner, tree_id]),
                )
            seen_seasons[season] = tree_id


def build_reference_map(
    *,
    canonical_id: str,
    member_ids: list[str],
) -> dict[str, str]:
    return {member_id: canonical_id for member_id in member_ids}


def apply_merge_to_tree(
    canonical: dict[str, Any],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    merged_from = sorted(
        set(canonical.get("merged_from", []))
        | {item["id"] for item in members}
    )
    merged = {
        **canonical,
        "schema_version": 2,
        "merged_from": merged_from,
        "revision": int(canonical["revision"]) + 1,
    }
    return merged


def merge_has_new_rule_facts(
    *,
    merged_canonical: dict[str, Any],
    upgraded_observations: list[dict[str, Any]],
) -> list[str]:
    """返回该合并产生的、v1 无法表达的新规则事实类型。

    规范树本身的 merged_from 是 v2-only 引用结构；如果被合并的季节志
    还含有半级置信度或休眠芽阶段，则它们也是新规则事实。回滚时这些
    事实决定该批次必须保留。
    """

    facts: list[str] = []
    if merged_canonical.get("merged_from"):
        facts.append("canonical_tree_reference")
    for observation in upgraded_observations:
        for entry in observation.get("entries", []):
            confidence = float(entry["confidence"])
            if abs(confidence - round(confidence)) > 1e-9:
                facts.append("half_step_confidence")
            if entry["stage"] == "dormant_bud":
                facts.append("dormant_bud_stage")
    return sorted(set(facts))


def normalize_reference_map(raw: dict[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in raw.items()}
