"""与具体规则版本无关的确定性业务语义。

业务指纹只表达“同一业务结论”，故意忽略存储精度、阶段内部序号、
授权表示方式等会随模式切换变化、但不应改变结论的细节。

v1 与 v2 两套规则都调用这里的函数，只是传入不同的阶段目录、
置信度刻度和引用映射，从而保证比较口径完全一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class StageCatalog:
    """某个规则版本下的阶段目录。"""

    order: dict[str, int]
    labels: dict[str, str]
    required: frozenset[str]

    def rank_of(self, key: str) -> int:
        return self.order[key]

    def sorted_entries(
        self,
        entries: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        def key(item: dict[str, Any]) -> tuple[int, str]:
            stage = str(item.get("stage"))
            return (self.order.get(stage, 1_000_000), str(item.get("observed_on", "")))

        return sorted(entries, key=key)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def entry_conclusion(
    entry: dict[str, Any],
    *,
    catalog: StageCatalog,
    confidence_scale: float,
) -> dict[str, Any]:
    """单条物候条目的业务结论。

    confidence_scale 把存储值折算到统一的 0.5 步长刻度：
    v1 整数 1..5 折算为 2.0..10.0；v2 本身就是 1.0..5.0 步长 0.5，
    折算为 2.0..10.0。这样旧整数 4 与新 4.0 得到同一结论。
    """

    confidence = float(entry["confidence"]) * confidence_scale
    # 统一保留一位小数，避免 4.0 与 4 这类表示差异进入指纹。
    confidence = round(confidence, 1)
    return {
        "stage": entry["stage"],
        "observed_on": entry["observed_on"],
        "confidence": confidence,
    }


def observation_conclusion(
    observation: dict[str, Any],
    *,
    catalog: StageCatalog,
    confidence_scale: float,
) -> dict[str, Any]:
    """季节志的业务结论：完成性、阶段集合与日期、阶段顺序。"""

    entries = catalog.sorted_entries(observation.get("entries", []))
    normalized_entries = [
        entry_conclusion(item, catalog=catalog, confidence_scale=confidence_scale)
        for item in entries
    ]
    present = {item["stage"] for item in entries}
    missing_required = sorted(
        set(catalog.required) - present,
        key=catalog.rank_of,
    )
    sequence_ok = _sequence_ok(entries, catalog=catalog)
    return {
        "id": observation["id"],
        "tree_id": observation["tree_id"],
        "plot_id": observation["plot_id"],
        "season": observation["season"],
        "observer": observation.get("observer"),
        "status": observation["status"],
        "stages": [item["stage"] for item in normalized_entries],
        "entries": normalized_entries,
        "required_missing": missing_required,
        "sequence_ok": sequence_ok,
        "can_complete": observation["status"] == "completed"
        or (not missing_required and sequence_ok),
    }


def _sequence_ok(
    entries: list[dict[str, Any]],
    *,
    catalog: StageCatalog,
) -> bool:
    previous_rank = -1
    previous_date: date | None = None
    for item in entries:
        stage = str(item.get("stage"))
        if stage not in catalog.order:
            return False
        rank = catalog.rank_of(stage)
        observed = date.fromisoformat(str(item["observed_on"]))
        if rank < previous_rank:
            return False
        if previous_date is not None and observed < previous_date:
            return False
        previous_rank = rank
        previous_date = observed
    return True


def comparison_conclusion(
    comparison: dict[str, Any],
    *,
    catalog: StageCatalog,
    confidence_scale: float,
    observations_by_id: dict[str, dict[str, Any]],
    reference_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """对比图谱的业务结论。

    直接重算偏移而不是信任存储值，确保精度规则（v2 的半级置信度、
    四舍五入口径）或引用合并不会让同一对季节志产生不同偏移。
    引用映射把被合并植株的季节志归一到规范植株，因此合并前后
    “同一对业务对象”的结论保持不变。
    """

    reference_map = reference_map or {}
    left_id = comparison["left_observation_id"]
    right_id = comparison["right_observation_id"]
    left = observations_by_id.get(left_id)
    right = observations_by_id.get(right_id)
    if left is None or right is None:
        # 冻结记录即使源被合并也必须可复算；退化为存储的偏移结论。
        return {
            "id": comparison["id"],
            "season": comparison.get("season"),
            "frozen_offsets": [
                {
                    "stage": item["stage"],
                    "offset_days": int(item["offset_days"]),
                }
                for item in comparison.get("stage_offsets", [])
            ],
        }

    left_entries = {
        item["stage"]: item
        for item in catalog.sorted_entries(left.get("entries", []))
    }
    right_entries = {
        item["stage"]: item
        for item in catalog.sorted_entries(right.get("entries", []))
    }
    common = sorted(
        set(left_entries) & set(right_entries),
        key=catalog.rank_of,
    )
    offsets = []
    for stage in common:
        left_date = date.fromisoformat(left_entries[stage]["observed_on"])
        right_date = date.fromisoformat(right_entries[stage]["observed_on"])
        gap = abs(
            float(left_entries[stage]["confidence"]) * confidence_scale
            - float(right_entries[stage]["confidence"]) * confidence_scale
        )
        offsets.append(
            {
                "stage": stage,
                "offset_days": (right_date - left_date).days,
                "confidence_gap": round(gap, 1),
            }
        )
    values = [item["offset_days"] for item in offsets]
    average = round(sum(values) / len(values), 1) if values else 0.0
    return {
        "id": comparison["id"],
        "season": comparison.get("season") or left["season"],
        "pair": sorted(
            [
                reference_map.get(left["tree_id"], left["tree_id"]),
                reference_map.get(right["tree_id"], right["tree_id"]),
            ]
        ),
        "common_stages": common,
        "offsets": offsets,
        "average_offset_days": average,
        "spread": (max(values) - min(values)) if values else 0,
    }


def brief_conclusion(
    brief: dict[str, Any],
    *,
    catalog: StageCatalog,
    confidence_scale: float,
    reference_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """简报的业务结论：冻结时刻的园区、植株清单和季节志摘要。

    引用合并只影响内部植株标识，规范树与成员树指向同一业务植株，
    因此冻结清单按规范标识归一后结论不变。
    """

    reference_map = reference_map or {}
    payload = brief.get("payload") or {
        "plot": brief.get("plot"),
        "trees": brief.get("trees", []),
        "observations": brief.get("observations", []),
    }
    plot = payload.get("plot") or {}
    frozen_trees = sorted(
        {
            reference_map.get(str(tree.get("id")), str(tree.get("id")))
            for tree in payload.get("trees", [])
        }
    )
    frozen_observations = []
    for item in payload.get("observations", []):
        entries = catalog.sorted_entries(item.get("entries", []))
        frozen_observations.append(
            {
                "tree": reference_map.get(
                    str(item.get("tree_id")), str(item.get("tree_id"))
                ),
                "season": item.get("season"),
                "status": item.get("status"),
                "stages": [entry["stage"] for entry in entries],
                "dates": {
                    entry["stage"]: entry["observed_on"] for entry in entries
                },
            }
        )
    frozen_observations.sort(
        key=lambda item: (item["season"], item["tree"]),
    )
    return {
        "id": brief["id"],
        "plot": plot.get("id"),
        "plot_status": plot.get("status"),
        "plot_code": plot.get("code"),
        "tree_set": frozen_trees,
        "observations": frozen_observations,
    }


def business_state_fingerprint(
    state: dict[str, Any],
    *,
    catalog: StageCatalog,
    confidence_scale: float,
    reference_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """对整库四类业务事实分别计算指纹。"""

    reference_map = reference_map or {}
    observations_by_id = state.get("observations", {})

    observation_facts = [
        observation_conclusion(
            item,
            catalog=catalog,
            confidence_scale=confidence_scale,
        )
        for item in observations_by_id.values()
    ]
    comparison_facts = [
        comparison_conclusion(
            item,
            catalog=catalog,
            confidence_scale=confidence_scale,
            observations_by_id=observations_by_id,
            reference_map=reference_map,
        )
        for item in state.get("comparisons", {}).values()
    ]
    brief_facts = [
        brief_conclusion(
            item,
            catalog=catalog,
            confidence_scale=confidence_scale,
            reference_map=reference_map,
        )
        for item in state.get("briefs", {}).values()
    ]
    return {
        "observations": fingerprint(sorted(observation_facts, key=_fact_id)),
        "comparisons": fingerprint(sorted(comparison_facts, key=_fact_id)),
        "briefs": fingerprint(sorted(brief_facts, key=_fact_id)),
    }


def _fact_id(fact: dict[str, Any]) -> str:
    return str(fact.get("id"))
