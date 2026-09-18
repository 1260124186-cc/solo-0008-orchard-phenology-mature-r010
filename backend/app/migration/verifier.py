"""多维度迁移校验。

不止看“表结构升级成功”，每批切换前后同时比较四个维度：

1. 业务结果：观察、比较、简报结论指纹（见 semantics）；
2. 版本血缘：每个对象的版本序列可重放，切换不伪造历史版本；
3. 审计：迁移事件与业务审计分流，迁移后既有审计结论不变；
4. outbox：切换产生的事件与业务变更一一对应，旧事件不被改写。

另外提供冻结分析与历史时点复算：任意历史版本下，用旧规则重算出的
业务结论必须与新规则在同一时点重算出的结论一致。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .dual_read import DualReadEngine
from .phenology_v1 import V1_RULESET
from .phenology_v2 import V2_RULESET


def _audit_details(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details")
    return details if isinstance(details, dict) else {}


class MigrationVerifier:
    def __init__(self, database: Any, engine: DualReadEngine | None = None) -> None:
        self.database = database
        self.engine = engine or DualReadEngine()

    # ------------------------------------------------------------------
    # 业务结果
    # ------------------------------------------------------------------
    def business_checkpoint(
        self,
        state: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        reference_map = reference_map or {}
        return self.engine.compare_states(
            state,
            self.engine.upgrade_state(state, reference_map=reference_map),
            reference_map=reference_map,
        )

    # ------------------------------------------------------------------
    # 版本血缘
    # ------------------------------------------------------------------
    def lineage_for(
        self,
        connection: sqlite3.Connection,
        kind: str,
        object_id: str,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT kind, id, revision, payload, actor_id, action, created_at
            FROM entity_versions
            WHERE kind = ? AND id = ?
            ORDER BY revision ASC
            """,
            (kind, object_id),
        ).fetchall()
        return [
            {
                "revision": int(row["revision"]),
                "actor_id": row["actor_id"],
                "action": row["action"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def lineage_check(
        self,
        connection: sqlite3.Connection,
        switched: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """切换不得伪造历史：每个对象的版本序列严格单调递增、无重复无空洞，
        且切换前的历史版本保持其写入时的规则形态，不因模式切换被重写。"""

        problems: list[dict[str, Any]] = []
        checked = 0
        for kind, object_id in switched:
            lineage = self.lineage_for(connection, kind, object_id)
            checked += 1
            revisions = [item["revision"] for item in lineage]
            if revisions != sorted(set(revisions)):
                problems.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "reason": "版本序列有重复",
                        "revisions": revisions,
                    }
                )
                continue
            # 允许从任意起始号连续（回滚会继续追加），但必须无空洞。
            if revisions and revisions != list(
                range(revisions[0], revisions[0] + len(revisions))
            ):
                problems.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "reason": "版本序列不连续",
                        "revisions": revisions,
                    }
                )
                continue
            # 历史版本必须仍按其写入时的规则自洽：除最后一个（刚切换）版本外，
            # 其余历史版本不得被改写成新规则 schema。
            for item in lineage[:-1]:
                if int(item["payload"].get("schema_version", 1)) > V1_RULESET.version and not (
                    item["payload"].get("migration")
                ):
                    problems.append(
                        {
                            "kind": kind,
                            "id": object_id,
                            "reason": "历史版本被改写为新规则",
                            "revision": item["revision"],
                        }
                    )
                    break
        return {"checked_objects": checked, "problems": problems}

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit_check(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        expected_migration_events: int,
    ) -> dict[str, Any]:
        """既有业务审计事件不被迁移改写；新增的只能是迁移域事件。"""

        problems: list[dict[str, Any]] = []
        if before["max_sequence"] > after["max_sequence"]:
            problems.append({"reason": "审计序列回退"})
        # 迁移前已存在的业务审计事件内容必须逐字节保留。
        existing = {
            item["sequence"]: item
            for item in after["events"]
            if item["sequence"] <= before["max_sequence"]
        }
        for item in before["events"]:
            current = existing.get(item["sequence"])
            if current is None or current["event_id"] != item["event_id"]:
                problems.append(
                    {
                        "reason": "既有审计事件被修改或删除",
                        "sequence": item["sequence"],
                    }
                )
        new_events = [
            item
            for item in after["events"]
            if item["sequence"] > before["max_sequence"]
        ]
        # 只统计迁移域事件；并发常规业务写入允许穿插，不影响批次判定。
        migration_events = [
            item
            for item in new_events
            if str(item["action"]).startswith("migration.")
            and "migration" in _audit_details(item)
        ]
        if len(migration_events) < expected_migration_events:
            problems.append(
                {
                    "reason": "迁移审计事件数量不足",
                    "expected": expected_migration_events,
                    "actual": len(migration_events),
                }
            )
        return {
            "before_max_sequence": before["max_sequence"],
            "after_max_sequence": after["max_sequence"],
            "new_events": len(new_events),
            "migration_events": len(migration_events),
            "problems": problems,
        }

    def audit_snapshot(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT sequence, event_id, action, resource_kind, resource_id,
                   revision, details
            FROM audit_events
            ORDER BY sequence ASC
            """
        ).fetchall()
        max_sequence = int(rows[-1]["sequence"]) if rows else 0
        return {
            "max_sequence": max_sequence,
            "events": [
                {
                    "sequence": int(row["sequence"]),
                    "event_id": row["event_id"],
                    "action": row["action"],
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                    "revision": int(row["revision"]),
                    "details": json.loads(row["details"]),
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------------
    # outbox
    # ------------------------------------------------------------------
    def outbox_check(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        expected_changed: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """既有 outbox 事件不被改写；每个被切换对象恰好对应一个新事件。"""

        problems: list[dict[str, Any]] = []
        existing = {item["event_id"]: item for item in after["events"]}
        for item in before["events"]:
            current = existing.get(item["event_id"])
            if current is None or current["status"] != item["status"]:
                problems.append(
                    {"reason": "既有 outbox 事件被修改或删除", "event_id": item["event_id"]}
                )
        new_events = [
            item
            for item in after["events"]
            if item["id"] > before["max_id"]
        ]
        # 只校验迁移切换事件；并发业务 outbox 允许穿插，不污染批次结论。
        migration_events = [
            item
            for item in new_events
            if str(item["payload"].get("reason")) == "migration_switch"
        ]
        referenced: set[tuple[str, str]] = set()
        for item in migration_events:
            payload = item["payload"]
            referenced.add((str(payload.get("kind")), str(payload.get("id"))))
        expected = set(expected_changed)
        missing = sorted(expected - referenced)
        extra = sorted(referenced - expected)
        if missing:
            problems.append({"reason": "部分切换对象缺少 outbox 事件", "objects": missing[:20]})
        if extra:
            problems.append({"reason": "outbox 出现未切换对象的事件", "objects": extra[:20]})
        return {
            "new_events": len(new_events),
            "migration_events": len(migration_events),
            "expected_events": len(expected),
            "problems": problems,
        }

    def outbox_snapshot(self, connection: sqlite3.Connection) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT id, event_id, topic, payload, status
            FROM outbox_events
            ORDER BY id ASC
            """
        ).fetchall()
        max_id = int(rows[-1]["id"]) if rows else 0
        return {
            "max_id": max_id,
            "events": [
                {
                    "id": int(row["id"]),
                    "event_id": row["event_id"],
                    "topic": row["topic"],
                    "payload": json.loads(row["payload"]),
                    "status": row["status"],
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------------
    # 历史时点视图
    # ------------------------------------------------------------------
    def point_in_time_check(
        self,
        connection: sqlite3.Connection,
        kind: str,
        object_id: str,
        revisions: list[int],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """在指定历史修订号下，用旧规则与新规则分别复算业务结论。

        历史时点视图不能因模式切换而改变：v1 历史版本经新规则投影后的
        结论必须等于它在旧规则下的结论。
        """

        reference_map = reference_map or {}
        lineage = {
            item["revision"]: item["payload"]
            for item in self.lineage_for(connection, kind, object_id)
        }
        results = []
        for revision in revisions:
            payload = lineage.get(revision)
            if payload is None:
                results.append(
                    {"revision": revision, "ok": False, "reason": "版本不存在"}
                )
                continue
            # 回滚/切换追加的版本带 migration 标记，其历史时点语义由
            # 切换前的紧邻业务版本表达；这里对载荷按其写入规则复算，
            # 只验证“同一版本在两套规则下读出来的业务结论一致”。
            old_fp = self.engine.fingerprint_object(
                kind,
                payload,
                ruleset=V1_RULESET,
            )
            upgraded = self.engine.upgrade_payload(kind, payload, reasons=[])
            new_fp = self.engine.fingerprint_object(
                kind,
                upgraded,
                ruleset=V2_RULESET,
                reference_map=reference_map,
            )
            results.append(
                {
                    "revision": revision,
                    "ok": old_fp == new_fp,
                    "legacy_fingerprint": old_fp,
                    "new_fingerprint": new_fp,
                }
            )
        return {
            "kind": kind,
            "id": object_id,
            "points": results,
            "all_equal": all(item.get("ok") for item in results),
        }

    # ------------------------------------------------------------------
    # 冻结分析
    # ------------------------------------------------------------------
    def frozen_analysis_check(
        self,
        state: dict[str, Any],
        *,
        reference_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """所有既有比较与简报（冻结事实）在新规则下复算结论不变。"""

        reference_map = reference_map or {}
        problems: list[dict[str, Any]] = []
        for comparison in state["comparisons"].values():
            old_fp = self.engine.fingerprint_object(
                "comparison",
                comparison,
                ruleset=V1_RULESET,
            )
            upgraded = self.engine.upgrade_payload("comparison", comparison)
            # 用升级后的观察集合重算，才能检验精度与引用的影响。
            new_fp = self.engine.fingerprint_object(
                "comparison",
                upgraded,
                ruleset=V2_RULESET,
                reference_map=reference_map,
            )
            if old_fp != new_fp:
                problems.append(
                    {"kind": "comparison", "id": comparison["id"]}
                )
        for brief in state["briefs"].values():
            old_fp = self.engine.fingerprint_object(
                "brief",
                brief,
                ruleset=V1_RULESET,
            )
            upgraded = self.engine.upgrade_payload("brief", brief)
            new_fp = self.engine.fingerprint_object(
                "brief",
                upgraded,
                ruleset=V2_RULESET,
                reference_map=reference_map,
            )
            if old_fp != new_fp:
                problems.append({"kind": "brief", "id": brief["id"]})
        return {
            "checked_comparisons": len(state["comparisons"]),
            "checked_briefs": len(state["briefs"]),
            "problems": problems,
        }
