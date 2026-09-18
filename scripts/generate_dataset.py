#!/usr/bin/env python3
"""生成可重复的规模化物候数据集。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.persistence import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成果园物候规模数据集")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--plots", type=int, default=100)
    parser.add_argument("--trees-per-plot", type=int, default=5)
    parser.add_argument("--observations-per-tree", type=int, default=1)
    parser.add_argument("--comparisons", type=int, default=0)
    args = parser.parse_args()

    if args.plots < 1 or args.plots > 100_000:
        parser.error("--plots 必须在 1 到 100000 之间")
    if args.trees_per_plot < 1 or args.trees_per_plot > 1000:
        parser.error("--trees-per-plot 必须在 1 到 1000 之间")
    if args.observations_per_tree < 0 or args.observations_per_tree > 100:
        parser.error("--observations-per-tree 必须在 0 到 100 之间")
    if args.comparisons < 0 or args.comparisons > 100_000:
        parser.error("--comparisons 必须在 0 到 100000 之间")

    data_dir = args.data_dir.resolve()
    database = Database(data_dir / "atlas.sqlite3")
    database.initialize()
    timestamp = "2026-01-01T00:00:00+00:00"
    observation_ids: list[str] = []

    with database.transaction(immediate=True) as connection:
        for plot_index in range(1, args.plots + 1):
            plot_id = f"plot_bench_{plot_index:06d}"
            plot_code = f"BN-{10_000 + plot_index:05d}"
            _upsert(
                connection,
                "plot",
                plot_id,
                {
                    "id": plot_id,
                    "schema_version": 1,
                    "code": plot_code,
                    "name": f"规模园区 {plot_index}",
                    "locality": f"测试区域 {plot_index % 97}",
                    "cultivar_focus": f"测试品种 {plot_index % 53}",
                    "steward": f"档案组 {plot_index % 31}",
                    "planting_year": 2000 + (plot_index % 20),
                    "note": "规模数据",
                    "status": "confirmed",
                    "revision": 1,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "confirmed_at": timestamp,
                },
                timestamp,
            )
            for tree_index in range(1, args.trees_per_plot + 1):
                tree_id = f"tree_bench_{plot_index:06d}_{tree_index:03d}"
                tree_code = f"{plot_code}-T{tree_index:03d}"
                _upsert(
                    connection,
                    "tree",
                    tree_id,
                    {
                        "id": tree_id,
                        "schema_version": 1,
                        "plot_id": plot_id,
                        "code": tree_code,
                        "cultivar": f"测试品种 {plot_index % 53}",
                        "rootstock": "杜梨",
                        "planting_year": 2000 + (plot_index % 20),
                        "status": "active",
                        "note": "规模数据",
                        "revision": 1,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                    timestamp,
                )
                for season_index in range(args.observations_per_tree):
                    season = str(2020 + season_index)
                    observation_id = (
                        f"season_bench_{plot_index:06d}_{tree_index:03d}_{season}"
                    )
                    observation_ids.append(observation_id)
                    entries = _season_entries(season)
                    _upsert(
                        connection,
                        "observation",
                        observation_id,
                        {
                            "id": observation_id,
                            "schema_version": 1,
                            "tree_id": tree_id,
                            "plot_id": plot_id,
                            "season": season,
                            "observer": f"观察员 {plot_index % 41}",
                            "note": "规模数据",
                            "status": "completed",
                            "entries": entries,
                            "revision": 1,
                            "created_at": timestamp,
                            "updated_at": timestamp,
                            "completed_at": timestamp,
                        },
                        timestamp,
                    )

        for comparison_index in range(1, args.comparisons + 1):
            if len(observation_ids) < 2:
                break
            left = observation_ids[(comparison_index - 1) % len(observation_ids)]
            right = observation_ids[comparison_index % len(observation_ids)]
            if left == right:
                continue
            left_row = connection.execute(
                "SELECT payload FROM entities WHERE kind = 'observation' AND id = ?",
                (left,),
            ).fetchone()
            right_row = connection.execute(
                "SELECT payload FROM entities WHERE kind = 'observation' AND id = ?",
                (right,),
            ).fetchone()
            if left_row is None or right_row is None:
                continue
            left_payload = json.loads(left_row["payload"])
            right_payload = json.loads(right_row["payload"])
            left_tree = _fetch_tree(connection, left_payload["tree_id"])
            right_tree = _fetch_tree(connection, right_payload["tree_id"])
            offsets, summary = _comparison_values(
                left_payload, right_payload, left_tree, right_tree
            )
            comparison_id = f"atlas_bench_{comparison_index:06d}"
            _upsert(
                connection,
                "comparison",
                comparison_id,
                {
                    "id": comparison_id,
                    "schema_version": 1,
                    "title": f"规模比较 {comparison_index}",
                    "season": left_payload["season"],
                    "left_observation_id": left,
                    "right_observation_id": right,
                    "left_tree_id": left_payload["tree_id"],
                    "right_tree_id": right_payload["tree_id"],
                    "left_label": _tree_label(left_tree),
                    "right_label": _tree_label(right_tree),
                    "stage_offsets": offsets,
                    "summary": summary,
                    "created_at": timestamp,
                },
                timestamp,
            )
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES ('state_revision', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(args.plots * (1 + args.trees_per_plot)),),
        )
        event_id = f"evt_generate_{timestamp}"
        connection.execute(
            """
            INSERT OR IGNORE INTO audit_events
            (event_id, actor_id, action, resource_kind, resource_id,
             revision, details, created_at)
            VALUES (?, 'generator', 'generate', 'dataset', 'scale', ?, ?, ?)
            """,
            (
                event_id,
                args.plots * (1 + args.trees_per_plot),
                json.dumps(
                    {
                        "plots": args.plots,
                        "trees_per_plot": args.trees_per_plot,
                        "observations_per_tree": args.observations_per_tree,
                        "comparisons": args.comparisons,
                    },
                    ensure_ascii=False,
                ),
                timestamp,
            ),
        )

    print(
        json.dumps(
            {
                "database": str(database.path),
                "plots": args.plots,
                "trees": args.plots * args.trees_per_plot,
                "observations": len(observation_ids),
                "comparisons": min(args.comparisons, max(0, len(observation_ids) - 1)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _season_entries(season: str) -> list[dict[str, Any]]:
    start = date(int(season), 3, 10)
    stages = ("bud_burst", "full_bloom", "fruit_set", "harvest")
    offsets = (0, 22, 40, 170)
    return [
        {
            "id": f"entry_{stage}_{start.isoformat()}",
            "stage": stage,
            "observed_on": (start + timedelta(days=offset)).isoformat(),
            "confidence": 4,
            "note": "规模数据",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        for stage, offset in zip(stages, offsets, strict=True)
    ]


def _upsert(
    connection: Any,
    kind: str,
    identifier: str,
    payload: dict[str, Any],
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO entities
        (kind, id, payload, revision, created_at, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(kind, id) DO UPDATE SET
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        (
            kind,
            identifier,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO entity_versions
        (kind, id, revision, payload, actor_id, action, created_at)
        VALUES (?, ?, 1, ?, 'generator', 'generate', ?)
        """,
        (
            kind,
            identifier,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            timestamp,
        ),
    )


def _fetch_tree(
    connection: sqlite3.Connection,
    tree_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT payload FROM entities WHERE kind = 'tree' AND id = ?",
        (tree_id,),
    ).fetchone()
    return json.loads(row["payload"]) if row is not None else None


def _tree_label(tree: dict[str, Any] | None) -> str:
    if tree is None:
        return "已移除植株"
    return f"{tree['code']} · {tree['cultivar']}"


def _comparison_values(
    left: dict[str, Any],
    right: dict[str, Any],
    left_tree: dict[str, Any] | None,
    right_tree: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """与业务域完全一致地计算偏移与摘要，避免规模数据违反领域规则。"""
    from app.domain.comparison_rules import (
        build_summary,
        calculate_stage_offsets,
    )

    offsets = calculate_stage_offsets(left, right)
    summary = build_summary(
        "规模比较",
        left,
        right,
        left_tree,
        right_tree,
        offsets,
    )
    return offsets, summary


if __name__ == "__main__":
    raise SystemExit(main())
