"""两份季节志的确定性对齐规则。"""

from __future__ import annotations

from datetime import date
from statistics import mean
from typing import Any

from ..errors import PreconditionError, ValidationError
from .plot_rules import new_identifier, now_iso
from .stages import STAGE_BY_KEY, sort_stage_entries, stage_label


def create_comparison_record(
    payload: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
    left_tree: dict[str, Any] | None,
    right_tree: dict[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    left_id = str(payload.get("left_observation_id") or "").strip()
    right_id = str(payload.get("right_observation_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValidationError("请填写对比图谱标题", field_name="title")
    if len(title) > 100:
        raise ValidationError("对比图谱标题最多 100 个字符", field_name="title")
    if left_id == right_id:
        raise ValidationError("请选择两份不同的季节志", field_name="right_observation_id")
    ensure_comparable(left, right)
    offsets = calculate_stage_offsets(left, right)
    if not offsets:
        raise PreconditionError(
            "no_common_stage",
            "两份季节志没有可比较的共同阶段",
        )
    summary = build_summary(title, left, right, left_tree, right_tree, offsets)
    return {
        "id": new_identifier("atlas"),
        "schema_version": 1,
        "title": title,
        "season": left["season"],
        "left_observation_id": left["id"],
        "right_observation_id": right["id"],
        "left_tree_id": left["tree_id"],
        "right_tree_id": right["tree_id"],
        "left_label": label_for(left_tree),
        "right_label": label_for(right_tree),
        "stage_offsets": offsets,
        "summary": summary,
        "created_at": timestamp,
    }


def ensure_comparable(left: dict[str, Any], right: dict[str, Any]) -> None:
    if left["status"] != "completed" or right["status"] != "completed":
        raise PreconditionError(
            "season_not_completed",
            "只有已完成的季节志可以生成对比图谱",
        )
    if left["season"] != right["season"]:
        raise ValidationError(
            "两份季节志必须属于同一年份",
            field_name="season",
            details={"left": left["season"], "right": right["season"]},
        )


def calculate_stage_offsets(
    left: dict[str, Any],
    right: dict[str, Any],
) -> list[dict[str, Any]]:
    left_entries = {
        item["stage"]: item for item in sort_stage_entries(left.get("entries", []))
    }
    right_entries = {
        item["stage"]: item for item in sort_stage_entries(right.get("entries", []))
    }
    common = set(left_entries) & set(right_entries)
    ordered_common = sorted(common, key=_rank_of)
    result: list[dict[str, Any]] = []
    for key in ordered_common:
        left_entry = left_entries[key]
        right_entry = right_entries[key]
        left_date = date.fromisoformat(left_entry["observed_on"])
        right_date = date.fromisoformat(right_entry["observed_on"])
        offset = (right_date - left_date).days
        result.append(
            {
                "stage": key,
                "label": stage_label(key),
                "rank": _rank_of(key),
                "left_date": left_date.isoformat(),
                "right_date": right_date.isoformat(),
                "offset_days": offset,
                "confidence_gap": abs(
                    int(left_entry["confidence"]) - int(right_entry["confidence"])
                ),
            }
        )
    return result


def _rank_of(key: str) -> int:
    from .stages import stage_rank

    rank = stage_rank(key)
    return rank if rank is not None else 10 ** 9


def build_summary(
    title: str,
    left: dict[str, Any],
    right: dict[str, Any],
    left_tree: dict[str, Any] | None,
    right_tree: dict[str, Any] | None,
    offsets: list[dict[str, Any]],
) -> dict[str, Any]:
    values = [int(item["offset_days"]) for item in offsets]
    average = round(mean(values), 1)
    earliest = min(offsets, key=lambda item: item["offset_days"])
    latest = max(offsets, key=lambda item: item["offset_days"])
    consistent = max(values) - min(values) <= 10
    direction = "接近同步"
    if average <= -5:
        direction = "右侧植株整体偏早"
    elif average >= 5:
        direction = "右侧植株整体偏晚"
    if consistent:
        stability = "阶段偏移较为集中"
    else:
        stability = "阶段偏移差异较大，建议复核记录"
    sentence = (
        f"{left_tree_label(left_tree)} 与 {right_tree_label(right_tree)} 在 "
        f"{left['season']} 年共有 {len(offsets)} 个阶段可比较，"
        f"平均偏移 {average:+.1f} 天，{direction}；{stability}。"
    )
    return {
        "title": title,
        "common_stage_count": len(offsets),
        "average_offset_days": average,
        "minimum_offset_days": int(earliest["offset_days"]),
        "maximum_offset_days": int(latest["offset_days"]),
        "earliest_stage": earliest["label"],
        "latest_stage": latest["label"],
        "direction": direction,
        "stability": stability,
        "sentence": sentence,
    }


def label_for(tree: dict[str, Any] | None) -> str:
    if tree is None:
        return "已移除植株"
    return f"{tree['code']} · {tree['cultivar']}"


def left_tree_label(tree: dict[str, Any] | None) -> str:
    return label_for(tree)


def right_tree_label(tree: dict[str, Any] | None) -> str:
    return label_for(tree)


def comparison_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "title": record["title"],
        "season": record["season"],
        "left_observation_id": record["left_observation_id"],
        "right_observation_id": record["right_observation_id"],
        "left_label": record["left_label"],
        "right_label": record["right_label"],
        "stage_offsets": record["stage_offsets"],
        "summary": record["summary"],
        "created_at": record["created_at"],
    }
