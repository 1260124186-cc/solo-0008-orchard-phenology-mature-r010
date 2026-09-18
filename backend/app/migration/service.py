"""分阶段、可双读、可暂停/回滚的领域迁移编排服务。

批次生命周期：

    pending → verifying → verified → applying → applied
                          ↘ failed（不影响其他批次，可重试）

每个“应用批次”在单个 SQLite 事务里切换对象，并同时提交对象版本、审计、
outbox 与台账检查点；任一步失败整批回滚，其他批次的已提交结果不受影响。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from ..domain.observation_rules import update_observation_note
from ..domain.plot_rules import update_plot_record
from ..errors import DomainError, NotFoundError, PreconditionError, ValidationError
from ..persistence.repository import (
    ENTITY_KINDS,
    _append_audit,
    _append_outbox,
    _canonical,
    _get_meta,
    _load_state,
    _set_meta,
    _upsert_entity,
)
from . import engine, store
from .runtime import GENERATION_META_KEY, RUNTIME


PLAN_PREFIX = "plan"
ENTITY_CONTAINER = {kind: container for container, kind in ENTITY_KINDS.items()}


class MigrationService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self.database = repository.database
        self.repository.migration_hook = self.before_commit
        self.recover_runtime()

    # ------------------------------------------------------------------
    # 启动恢复
    # ------------------------------------------------------------------

    def recover_runtime(self) -> dict[str, Any]:
        """进程启动时恢复迁移现场：世代、活跃计划、进行中批次。"""
        with self.database.transaction(immediate=True) as connection:
            plan_id, generation = store.finalized_generation(connection)
            RUNTIME.generation = generation
            RUNTIME.finalized_plan_id = plan_id
            active = store.active_plan(connection)
            RUNTIME.active_plan_id = active["id"] if active else None
            RUNTIME.canonical_tree = self._canonical_map(connection)
            recovered: list[str] = []
            # 进程崩溃时停在 verifying/applying 的批次没有事务边界，安全重置。
            for batch in store.list_batches(
                connection, active["id"] if active else "__none__"
            ):
                if batch["status"] in ("verifying", "applying"):
                    store.update_batch(
                        connection,
                        plan_id=active["id"],
                        seq=batch["seq"],
                        status="pending",
                        timestamp=_now(),
                        error="进程重启，中断批次已安全重置为 pending",
                    )
                    recovered.append(f"batch-{batch['seq']}")
            _set_generation_meta(connection, generation)
        return {
            "generation": RUNTIME.generation,
            "active_plan": RUNTIME.active_plan_id,
            "finalized_plan": RUNTIME.finalized_plan_id,
            "recovered_batches": recovered,
        }

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    def create_plan(
        self,
        *,
        name: str,
        actor_id: str,
        batch_size: int = 50,
        rationale: str = "",
        to_generation: int = 2,
    ) -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValidationError("迁移计划必须有名称", field_name="name")
        if batch_size < 1 or batch_size > 1000:
            raise ValidationError(
                "批次容量必须在 1 到 1000 之间",
                field_name="batch_size",
            )
        if to_generation != 2:
            raise ValidationError("当前只支持向第 2 代规则迁移", field_name="to_generation")
        timestamp = _now()
        plan_id = f"{PLAN_PREFIX}_{uuid.uuid4().hex[:16]}"
        with self.database.transaction(immediate=True) as connection:
            existing = store.active_plan(connection)
            if existing is not None:
                raise PreconditionError(
                    "migration_plan_active",
                    "已有进行中的迁移计划，请先完成或回滚",
                    plan_id=existing["id"],
                    status=existing["status"],
                )
            if store.finalized_generation(connection)[1] >= to_generation:
                raise PreconditionError(
                    "migration_already_finalized",
                    "第 2 代规则已正式切换，不能重复建立计划",
                )
            store.insert_plan(
                connection,
                plan_id=plan_id,
                name=name,
                from_generation=1,
                to_generation=to_generation,
                batch_size=batch_size,
                rationale=str(rationale or "")[:2000],
                created_by=actor_id,
                timestamp=timestamp,
            )
            state = _load_state(connection)
            ordered = _ordered_objects(state)
            seq = 0
            for index, (kind, object_id) in enumerate(ordered):
                seq = index // batch_size
                if index % batch_size == 0:
                    store.insert_batch(
                        connection,
                        plan_id=plan_id,
                        seq=seq,
                        timestamp=timestamp,
                    )
                payload = state[ENTITY_CONTAINER[kind]][object_id]
                store.upsert_object(
                    connection,
                    plan_id=plan_id,
                    kind=kind,
                    object_id=object_id,
                    status="registered",
                    timestamp=timestamp,
                    batch_seq=seq,
                    source_revision=int(payload.get("revision") or 0),
                    legacy_payload=_canonical(payload),
                )
            # 空数据也建立一个批次，容纳迁移期间的新写入。
            if not ordered:
                store.insert_batch(
                    connection,
                    plan_id=plan_id,
                    seq=0,
                    timestamp=timestamp,
                )
            for batch in store.list_batches(connection, plan_id):
                store.update_batch(
                    connection,
                    plan_id=plan_id,
                    seq=batch["seq"],
                    status="pending",
                    timestamp=timestamp,
                    expected=len(
                        store.list_objects(
                            connection, plan_id, batch_seq=batch["seq"]
                        )
                    ),
                )
            counts = store.recompute_plan_counts(connection, plan_id)
            _append_audit(
                connection,
                actor_id=actor_id,
                action="migration.plan_create",
                resource_kind="migration",
                resource_id=plan_id,
                revision=_state_revision(connection) + 1,
                details={
                    "name": name,
                    "batch_size": batch_size,
                    "total_objects": counts["total"],
                    "rationale": str(rationale or ""),
                },
                context=None,
            )
            _append_outbox(
                connection,
                topic="atlas.migration.plan_changed",
                payload={"plan_id": plan_id, "event": "created", **counts},
            )
            _bump_state_revision(connection)
            plan = store.fetch_plan(connection, plan_id)
        RUNTIME.active_plan_id = plan_id
        return plan

    def get_plan(self, plan_ref: str | dict[str, Any]) -> dict[str, Any]:
        plan_id = plan_ref["id"] if isinstance(plan_ref, dict) else plan_ref
        with self.database.read_connection() as connection:
            plan = store.fetch_plan(connection, plan_id)
            if plan is None:
                raise NotFoundError("迁移计划", plan_id)
            plan["batches"] = store.list_batches(connection, plan_id)
            plan["status_counts"] = store.count_objects_by_status(
                connection, plan_id
            )
        return plan

    def list_plans(self) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM migration_plans ORDER BY rowid"
            ).fetchall()
            items = [store._plan_view(row) for row in rows]
        return {"items": items, "total": len(items)}

    def pause(self, plan_ref: str | dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        return self._set_status(
            _plan_id(plan_ref),
            actor_id,
            from_statuses={"active"},
            to_status="paused",
            action="migration.plan_pause",
        )

    def resume(self, plan_ref: str | dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        return self._set_status(
            _plan_id(plan_ref),
            actor_id,
            from_statuses={"paused"},
            to_status="active",
            action="migration.plan_resume",
        )
    def _set_status(
        self,
        plan_id: str,
        actor_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        action: str,
    ) -> dict[str, Any]:
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in from_statuses:
                raise PreconditionError(
                    "migration_plan_state_invalid",
                    f"计划当前状态 {plan['status']} 不允许该操作",
                    status=plan["status"],
                )
            store.update_plan_status(
                connection,
                plan_id=plan_id,
                status=to_status,
                timestamp=timestamp,
            )
            _append_audit(
                connection,
                actor_id=actor_id,
                action=action,
                resource_kind="migration",
                resource_id=plan_id,
                revision=_state_revision(connection) + 1,
                details={"status": to_status},
                context=None,
            )
            _append_outbox(
                connection,
                topic="atlas.migration.plan_changed",
                payload={"plan_id": plan_id, "event": to_status},
            )
            _bump_state_revision(connection)
            plan = store.fetch_plan(connection, plan_id)
        if to_status == "active":
            RUNTIME.active_plan_id = plan_id
        else:
            RUNTIME.active_plan_id = None
        return plan

    # ------------------------------------------------------------------
    # 批次：双读验证
    # ------------------------------------------------------------------

    def verify_batch(
        self,
        plan_ref: str | dict[str, Any],
        seq: int,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_active_plan(connection, plan_id)
            batch = self._require_batch(connection, plan_id, seq)
            if batch["status"] not in {"pending", "failed"}:
                raise PreconditionError(
                    "migration_batch_not_verifiable",
                    f"批次 {seq} 当前状态 {batch['status']}，不能重新验证",
                    status=batch["status"],
                )
            store.update_batch(
                connection,
                plan_id=plan_id,
                seq=seq,
                status="verifying",
                timestamp=timestamp,
                error=None,
            )
            try:
                state = _load_state(connection)
                canonical_map, merged_sources = self._maps_for_plan(
                    connection, include_pending_merges=True
                )
                members = self._batch_members(connection, plan_id, seq)
                report = self._shadow_verify(
                    connection,
                    plan=plan,
                    state=state,
                    members=members,
                    canonical_map=canonical_map,
                    merged_sources=merged_sources,
                    timestamp=timestamp,
                    commit_projections=True,
                )
                status = "verified" if report["incompatible"] == 0 else "failed"
                store.update_batch(
                    connection,
                    plan_id=plan_id,
                    seq=seq,
                    status=status,
                    timestamp=timestamp,
                    expected=report["checked"],
                    incompatible=report["incompatible"],
                    checkpoint=_canonical(
                        {
                            "seq": seq,
                            "checked": report["checked"],
                            "incompatible": report["incompatible"],
                            "object_keys": report["object_keys"],
                            "state_revision_before": report[
                                "state_revision_before"
                            ],
                        }
                    ),
                    report=_canonical(report),
                    error=(
                        None
                        if status == "verified"
                        else f"{report['incompatible']} 个对象与 v2 规则不兼容"
                    ),
                )
                store.recompute_plan_counts(connection, plan_id)
                _append_audit(
                    connection,
                    actor_id=actor_id,
                    action="migration.batch_verify",
                    resource_kind="migration",
                    resource_id=f"{plan_id}/batch/{seq}",
                    revision=_state_revision(connection) + 1,
                    details={
                        "seq": seq,
                        "checked": report["checked"],
                        "incompatible": report["incompatible"],
                        "status": status,
                    },
                    context=None,
                )
                _append_outbox(
                    connection,
                    topic="atlas.migration.batch_changed",
                    payload={
                        "plan_id": plan_id,
                        "seq": seq,
                        "event": "verified" if status == "verified" else "failed",
                    },
                )
                _bump_state_revision(connection)
                result = store.fetch_batch(connection, plan_id, seq)
            except Exception as exc:
                store.update_batch(
                    connection,
                    plan_id=plan_id,
                    seq=seq,
                    status="failed",
                    timestamp=timestamp,
                    error=f"验证中断：{exc}",
                )
                raise
        return result

    # ------------------------------------------------------------------
    # 批次：切换应用
    # ------------------------------------------------------------------

    def apply_batch(
        self,
        plan_ref: str | dict[str, Any],
        seq: int,
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_active_plan(connection, plan_id)
            batch = self._require_batch(connection, plan_id, seq)
            if batch["status"] != "verified":
                raise PreconditionError(
                    "migration_batch_not_verified",
                    f"批次 {seq} 尚未通过双读验证（当前 {batch['status']}）",
                    status=batch["status"],
                )
            store.update_batch(
                connection,
                plan_id=plan_id,
                seq=seq,
                status="applying",
                timestamp=timestamp,
            )
            try:
                state = _load_state(connection)
                canonical_map, merged_sources = self._maps_for_plan(
                    connection, include_pending_merges=True
                )
                members = self._batch_members(connection, plan_id, seq)
                # 应用前再次双读：防止验证后对象被业务写入改动（乐观防漂移）。
                report = self._shadow_verify(
                    connection,
                    plan=plan,
                    state=state,
                    members=members,
                    canonical_map=canonical_map,
                    merged_sources=merged_sources,
                    timestamp=timestamp,
                    commit_projections=False,
                )
                if report["incompatible"]:
                    store.update_batch(
                        connection,
                        plan_id=plan_id,
                        seq=seq,
                        status="failed",
                        timestamp=timestamp,
                        incompatible=report["incompatible"],
                        report=_canonical(report),
                        error="应用前复核发现对象已漂移或不兼容",
                    )
                    raise PreconditionError(
                        "migration_batch_drifted",
                        f"批次 {seq} 在应用前复核失败，已保持未切换",
                        incompatible=report["incompatible"],
                    )

                revision_counter = _state_revision(connection)
                applied = 0
                verified_only = 0
                for member in members:
                    kind = member["kind"]
                    object_id = member["object_id"]
                    container = ENTITY_CONTAINER[kind]
                    payload = state[container].get(object_id)
                    if payload is None:
                        # 对象在迁移期间被删除（基线暂无删除接口，防御性处理）。
                        store.upsert_object(
                            connection,
                            plan_id=plan_id,
                            kind=kind,
                            object_id=object_id,
                            status="skipped",
                            timestamp=timestamp,
                            reason="对象在切换前已不存在",
                        )
                        continue
                    v2, reasons = engine.project_object(
                        kind,
                        payload,
                        state=state,
                        canonical_map=canonical_map,
                        merged_sources=merged_sources,
                    )
                    if reasons:
                        raise DomainError(
                            "migration_incompatible_object",
                            f"对象 {kind}/{object_id} 不兼容",
                            500,
                            {"reasons": reasons},
                        )
                    fingerprint_v2 = engine.business_fingerprint(kind, v2)
                    store.upsert_projection(
                        connection,
                        plan_id=plan_id,
                        kind=kind,
                        object_id=object_id,
                        generation=2,
                        payload=v2,
                        fingerprint=fingerprint_v2,
                        timestamp=timestamp,
                    )
                    if kind in engine.MUTABLE_KINDS:
                        revision_counter += 1
                        switched = {
                            **v2,
                            "revision": revision_counter,
                            "updated_at": timestamp,
                        }
                        _upsert_entity(
                            connection,
                            kind=kind,
                            identifier=object_id,
                            payload=switched,
                            actor_id=actor_id,
                            action="migration.cutover",
                            created_at=timestamp,
                        )
                        _append_audit(
                            connection,
                            actor_id=actor_id,
                            action="migration.cutover",
                            resource_kind=kind,
                            resource_id=object_id,
                            revision=revision_counter,
                            details={
                                "plan_id": plan_id,
                                "batch_seq": seq,
                                "from_generation": 1,
                                "to_generation": 2,
                            },
                            context=None,
                        )
                        _append_outbox(
                            connection,
                            topic=f"atlas.{kind}.changed",
                            payload={
                                "kind": kind,
                                "id": object_id,
                                "operation": "migration_cutover",
                                "revision": revision_counter,
                                "actor_id": actor_id,
                                "plan_id": plan_id,
                                "batch_seq": seq,
                            },
                        )
                        applied += 1
                        new_status = "applied"
                    else:
                        # 不可变记录只见证规则等价，不产生假的变更事件。
                        verified_only += 1
                        new_status = "applied"
                    store.upsert_object(
                        connection,
                        plan_id=plan_id,
                        kind=kind,
                        object_id=object_id,
                        status=new_status,
                        timestamp=timestamp,
                        projected_payload=_canonical(v2),
                        v2_fingerprint=fingerprint_v2,
                    )

                revision_counter += 1
                _set_meta(connection, "state_revision", str(revision_counter))
                checkpoint = {
                    "seq": seq,
                    "applied": applied,
                    "verified_only": verified_only,
                    "object_keys": report["object_keys"],
                    "state_revision_after": revision_counter,
                    "canonical_tree": canonical_map,
                }
                store.update_batch(
                    connection,
                    plan_id=plan_id,
                    seq=seq,
                    status="applied",
                    timestamp=timestamp,
                    applied=applied,
                    incompatible=0,
                    checkpoint=_canonical(checkpoint),
                    applied_at=timestamp,
                    error=None,
                )
                store.recompute_plan_counts(connection, plan_id)
                # 只有当合并涉及的两株树都完成切换后，该合并才正式生效。
                for op in store.list_ops(connection, plan_id, op_type="merge"):
                    if op["status"] != "requested":
                        continue
                    payload = op["payload"]
                    statuses = [
                        self._object_status(
                            connection,
                            plan_id,
                            "tree",
                            str(payload["survivor_id"]),
                        ),
                        self._object_status(
                            connection,
                            plan_id,
                            "tree",
                            str(payload["alias_id"]),
                        ),
                    ]
                    if all(status == "applied" for status in statuses):
                        store.mark_op(
                            connection,
                            op["id"],
                            status="applied",
                            batch_seq=seq,
                        )
                _append_audit(
                    connection,
                    actor_id=actor_id,
                    action="migration.batch_apply",
                    resource_kind="migration",
                    resource_id=f"{plan_id}/batch/{seq}",
                    revision=revision_counter,
                    details={
                        "seq": seq,
                        "switched": applied,
                        "verified_only": verified_only,
                    },
                    context=None,
                )
                _append_outbox(
                    connection,
                    topic="atlas.migration.batch_changed",
                    payload={
                        "plan_id": plan_id,
                        "seq": seq,
                        "event": "applied",
                        "switched": applied,
                    },
                )
                RUNTIME.canonical_tree = self._canonical_map(connection)
                result = store.fetch_batch(connection, plan_id, seq)
            except Exception:
                raise
        return result

    def _shadow_verify(
        self,
        connection: Any,
        *,
        plan: dict[str, Any],
        state: dict[str, Any],
        members: list[dict[str, Any]],
        canonical_map: dict[str, str],
        merged_sources: dict[str, list[str]],
        timestamp: str,
        commit_projections: bool,
    ) -> dict[str, Any]:
        diffs: list[dict[str, Any]] = []
        incompatible_members: list[dict[str, Any]] = []
        object_keys: list[str] = []
        for member in members:
            kind = member["kind"]
            object_id = member["object_id"]
            object_keys.append(f"{kind}/{object_id}")
            container = ENTITY_CONTAINER[kind]
            payload = state[container].get(object_id)
            if payload is None:
                continue
            v2, reasons = engine.project_object(
                kind,
                payload,
                state=state,
                canonical_map=canonical_map,
                merged_sources=merged_sources,
            )
            parity, detail = engine.v1_v2_parity(kind, payload, v2)
            fingerprint_v1 = engine.business_fingerprint(kind, payload)
            fingerprint_v2 = engine.business_fingerprint(kind, v2)
            incompatible_reasons = list(reasons)
            if not parity:
                incompatible_reasons.append("v1/v2 业务指纹不一致")
                diffs.append({"kind": kind, "id": object_id, "diff": detail})
            if incompatible_reasons:
                incompatible_members.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "reasons": incompatible_reasons,
                    }
                )
                store.upsert_object(
                    connection,
                    plan_id=plan["id"],
                    kind=kind,
                    object_id=object_id,
                    status="incompatible",
                    timestamp=timestamp,
                    reason="；".join(incompatible_reasons),
                    projected_payload=_canonical(v2),
                    v1_fingerprint=fingerprint_v1,
                    v2_fingerprint=fingerprint_v2,
                )
            else:
                if commit_projections:
                    store.upsert_projection(
                        connection,
                        plan_id=plan["id"],
                        kind=kind,
                        object_id=object_id,
                        generation=2,
                        payload=v2,
                        fingerprint=fingerprint_v2,
                        timestamp=timestamp,
                    )
                store.upsert_object(
                    connection,
                    plan_id=plan["id"],
                    kind=kind,
                    object_id=object_id,
                    status="verified",
                    timestamp=timestamp,
                    source_revision=int(payload.get("revision") or 0),
                    legacy_payload=_canonical(payload),
                    projected_payload=_canonical(v2),
                    v1_fingerprint=fingerprint_v1,
                    v2_fingerprint=fingerprint_v2,
                    reason=None,
                )
        return {
            "checked": len(object_keys),
            "incompatible": len(incompatible_members),
            "incompatible_objects": incompatible_members,
            "business_diffs": diffs,
            "object_keys": object_keys,
            "state_revision_before": state["revision"],
        }

    # ------------------------------------------------------------------
    # 回滚（仅回滚尚未切换的批次）
    # ------------------------------------------------------------------

    def rollback_plan(
        self,
        plan_ref: str | dict[str, Any],
        *,
        actor_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in {"active", "paused"}:
                raise PreconditionError(
                    "migration_plan_not_rollbackable",
                    f"计划状态 {plan['status']} 不允许回滚",
                )
            batches = store.list_batches(connection, plan_id)
            applied_seqs = [b["seq"] for b in batches if b["status"] == "applied"]
            retained: list[dict[str, Any]] = []
            for batch in batches:
                if batch["status"] == "applied":
                    retained.append(
                        {
                            "seq": batch["seq"],
                            "objects": batch["applied_objects"],
                            "reason": (
                                "批次内对象已按第 2 代规则正式提交，"
                                "对象版本、审计与 outbox 已形成依赖事实，"
                                "因此保留切换结果，不做反向重写。"
                            ),
                        }
                    )
                    continue
                # 未切换批次：清空影子投影，成员回到 registered。
                for member in store.list_objects(
                    connection, plan_id, batch_seq=batch["seq"]
                ):
                    if member["status"] in {"verified", "incompatible"}:
                        store.upsert_object(
                            connection,
                            plan_id=plan_id,
                            kind=member["kind"],
                            object_id=member["object_id"],
                            status="rolled_back",
                            timestamp=timestamp,
                            reason="计划回滚，影子验证结果作废",
                        )
                store.update_batch(
                    connection,
                    plan_id=plan_id,
                    seq=batch["seq"],
                    status="rolled_back",
                    timestamp=timestamp,
                )
            for op in store.list_ops(connection, plan_id):
                if op["status"] == "requested":
                    store.mark_op(connection, op["id"], status="reverted")
            store.update_plan_status(
                connection,
                plan_id=plan_id,
                status="rolled_back",
                timestamp=timestamp,
            )
            store.recompute_plan_counts(connection, plan_id)
            _append_audit(
                connection,
                actor_id=actor_id,
                action="migration.plan_rollback",
                resource_kind="migration",
                resource_id=plan_id,
                revision=_state_revision(connection) + 1,
                details={
                    "reason": str(reason or "")[:1000],
                    "retained_applied_batches": retained,
                },
                context=None,
            )
            _append_outbox(
                connection,
                topic="atlas.migration.plan_changed",
                payload={
                    "plan_id": plan_id,
                    "event": "rolled_back",
                    "retained_batches": applied_seqs,
                },
            )
            _bump_state_revision(connection)
            result = store.fetch_plan(connection, plan_id)
            result["retained_applied_batches"] = retained
        RUNTIME.active_plan_id = None
        return result

    # ------------------------------------------------------------------
    # 正式切换
    # ------------------------------------------------------------------

    def finalize_plan(self, plan_ref: str | dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_active_plan(connection, plan_id)
            counts = store.count_objects_by_status(connection, plan_id)
            unfinished = {
                status: count
                for status, count in counts.items()
                if status != "applied"
            }
            if unfinished:
                raise PreconditionError(
                    "migration_not_complete",
                    "仍有对象未完成切换，不能正式启用第 2 代规则",
                    pending=unfinished,
                )
            batches = store.list_batches(connection, plan_id)
            if not any(b["status"] == "applied" for b in batches):
                raise PreconditionError(
                    "migration_no_applied_batch",
                    "没有任何批次完成切换",
                )
            store.update_plan_status(
                connection,
                plan_id=plan_id,
                status="finalized",
                timestamp=timestamp,
            )
            _set_generation_meta(connection, plan["to_generation"])
            _append_audit(
                connection,
                actor_id=actor_id,
                action="migration.plan_finalize",
                resource_kind="migration",
                resource_id=plan_id,
                revision=_state_revision(connection) + 1,
                details={
                    "to_generation": plan["to_generation"],
                    "total_objects": plan["total_objects"],
                    "batch_count": len(batches),
                },
                context=None,
            )
            _append_outbox(
                connection,
                topic="atlas.migration.plan_changed",
                payload={
                    "plan_id": plan_id,
                    "event": "finalized",
                    "generation": plan["to_generation"],
                },
            )
            _bump_state_revision(connection)
            result = store.fetch_plan(connection, plan_id)
        RUNTIME.generation = 2
        RUNTIME.finalized_plan_id = plan_id
        RUNTIME.active_plan_id = None
        return result

    # ------------------------------------------------------------------
    # 迁移中的更正、合并、不兼容集合
    # ------------------------------------------------------------------

    def list_incompatible(
        self,
        plan_ref: str | dict[str, Any],
        *,
        batch_seq: int | None = None,
    ) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        with self.database.read_connection() as connection:
            self._require_plan(connection, plan_id)
            items = [
                item
                for item in store.list_objects(
                    connection,
                    plan_id,
                    batch_seq=batch_seq,
                    status="incompatible",
                )
            ]
        return {
            "items": [
                {
                    "kind": item["kind"],
                    "id": item["object_id"],
                    "batch_seq": item["batch_seq"],
                    "reasons": (item["reason"] or "").split("；")
                    if item["reason"]
                    else [],
                }
                for item in items
            ],
            "total": len(items),
        }

    def correct_object(
        self,
        plan_ref: str | dict[str, Any],
        *,
        kind: str,
        object_id: str,
        change: dict[str, Any],
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """迁移期间对对象做领域更正；新旧规则下走同一条领域校验。"""
        plan_id = _plan_id(plan_ref)
        if kind not in {"observation", "plot"}:
            raise ValidationError(
                "当前只支持对季节志说明或草稿园区做迁移更正",
                field_name="kind",
            )
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            plan = self._require_active_plan(connection, plan_id)
            state = _load_state(connection)
            container = ENTITY_CONTAINER[kind]
            record = state[container].get(object_id)
            if record is None:
                raise NotFoundError("更正对象", f"{kind}/{object_id}")
            if kind == "observation":
                updated = update_observation_note(
                    record,
                    note=str(change.get("note", "")),
                    expected_revision=expected_revision,
                )
            else:
                if record["status"] != "draft":
                    raise PreconditionError(
                        "plot_not_editable",
                        "已确认园区不能在迁移更正中改名",
                    )
                from ..domain.plot_rules import normalize_plot_payload

                normalized = normalize_plot_payload(
                    {
                        "code": change.get("code", record["code"]),
                        "name": change.get("name", record["name"]),
                        "locality": change.get("locality", record["locality"]),
                        "cultivar_focus": change.get(
                            "cultivar_focus", record["cultivar_focus"]
                        ),
                        "steward": change.get("steward", record["steward"]),
                        "planting_year": change.get(
                            "planting_year", record["planting_year"]
                        ),
                        "note": change.get("note", record["note"]),
                    }
                )
                updated = update_plot_record(
                    record,
                    normalized,
                    expected_revision=expected_revision,
                )
            state[container][object_id] = updated
            next_revision = _state_revision(connection) + 1
            _set_meta(connection, "state_revision", str(next_revision))
            _upsert_entity(
                connection,
                kind=kind,
                identifier=object_id,
                payload=updated,
                actor_id=actor_id,
                action="migration.correction",
                created_at=timestamp,
            )
            _append_audit(
                connection,
                actor_id=actor_id,
                action="migration.correction",
                resource_kind=kind,
                resource_id=object_id,
                revision=next_revision,
                details={"plan_id": plan_id, "change": change},
                context=None,
            )
            _append_outbox(
                connection,
                topic=f"atlas.{kind}.changed",
                payload={
                    "kind": kind,
                    "id": object_id,
                    "operation": "migration_correction",
                    "revision": next_revision,
                    "actor_id": actor_id,
                    "plan_id": plan_id,
                },
            )
            op_id = store.insert_op(
                connection,
                plan_id=plan_id,
                op_type="correction",
                target_kind=kind,
                target_id=object_id,
                payload={"change": change, "revision_before": expected_revision},
                created_by=actor_id,
                timestamp=timestamp,
            )
            store.mark_op(connection, op_id, status="applied")
            # 立即重做该对象的影子投影，保持双读新鲜。
            canonical_map, merged_sources = self._maps_for_plan(
                connection, include_pending_merges=True
            )
            v2, reasons = engine.project_object(
                kind,
                updated,
                state=state,
                canonical_map=canonical_map,
                merged_sources=merged_sources,
            )
            fingerprint_v2 = engine.business_fingerprint(kind, v2)
            store.upsert_projection(
                connection,
                plan_id=plan_id,
                kind=kind,
                object_id=object_id,
                generation=2,
                payload=v2,
                fingerprint=fingerprint_v2,
                timestamp=timestamp,
            )
            current = store.fetch_object(connection, plan_id, kind, object_id)
            new_object_status = (
                "registered"
                if current is None or current["status"] == "incompatible"
                else "verified"
            )
            store.upsert_object(
                connection,
                plan_id=plan_id,
                kind=kind,
                object_id=object_id,
                status=new_object_status,
                timestamp=timestamp,
                source_revision=next_revision,
                legacy_payload=_canonical(updated),
                projected_payload=_canonical(v2),
                v1_fingerprint=engine.business_fingerprint(kind, updated),
                v2_fingerprint=fingerprint_v2,
                reason="；".join(reasons) if reasons else None,
            )
            store.recompute_plan_counts(connection, plan_id)
        return {"kind": kind, "id": object_id, "revision": next_revision}

    def request_merge(
        self,
        plan_ref: str | dict[str, Any],
        *,
        survivor_id: str,
        alias_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        timestamp = _now()
        with self.database.transaction(immediate=True) as connection:
            self._require_active_plan(connection, plan_id)
            state = _load_state(connection)
            survivor = state["trees"].get(survivor_id)
            alias = state["trees"].get(alias_id)
            if survivor is None or alias is None:
                raise NotFoundError("合并植株", f"{survivor_id} / {alias_id}")
            for op in store.list_ops(connection, plan_id, op_type="merge"):
                if op["payload"].get("alias_id") == alias_id:
                    raise PreconditionError(
                        "tree_already_merging",
                        "该植株已经在一条合并记录中",
                        alias_id=alias_id,
                    )
            reasons = engine.merge_eligibility(
                survivor, alias, state["observations"]
            )
            # 合并会重写植株引用，必须在两株树切换之前登记；已切换批次不会被
            # 回头重写，因此对已 applied 的植株登记合并无法生效。
            for tree_id in (survivor_id, alias_id):
                member = store.fetch_object(connection, plan_id, "tree", tree_id)
                if member is not None and member["status"] == "applied":
                    raise PreconditionError(
                        "tree_already_cut_over",
                        "植株所在批次已经切换，不能再登记合并",
                        tree_id=tree_id,
                        batch_seq=member["batch_seq"],
                    )
            if reasons:
                raise PreconditionError(
                    "tree_merge_ineligible",
                    "植株不满足合并条件",
                    reasons=reasons,
                )
            op_id = store.insert_op(
                connection,
                plan_id=plan_id,
                op_type="merge",
                target_kind="tree",
                target_id=alias_id,
                payload={"survivor_id": survivor_id, "alias_id": alias_id},
                created_by=actor_id,
                timestamp=timestamp,
            )
            _append_audit(
                connection,
                actor_id=actor_id,
                action="migration.merge_request",
                resource_kind="tree",
                resource_id=alias_id,
                revision=_state_revision(connection) + 1,
                details={
                    "plan_id": plan_id,
                    "survivor_id": survivor_id,
                    "alias_id": alias_id,
                    "op_id": op_id,
                },
                context=None,
            )
            _append_outbox(
                connection,
                topic="atlas.tree.merge_requested",
                payload={
                    "plan_id": plan_id,
                    "survivor_id": survivor_id,
                    "alias_id": alias_id,
                },
            )
            _bump_state_revision(connection)
        return {"survivor_id": survivor_id, "alias_id": alias_id, "op_id": op_id}

    def list_corrections(self, plan_ref: str | dict[str, Any]) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        with self.database.read_connection() as connection:
            self._require_plan(connection, plan_id)
            items = store.list_ops(connection, plan_id)
        return {"items": items, "total": len(items)}

    def enqueue_next_batch_job(
        self,
        plan_ref: str | dict[str, Any],
        *,
        actor_id: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        """把下一批验证/切换排为后台任务，支持在生产式负载下推进。"""
        plan_id = _plan_id(plan_ref)
        from ..jobs.service import JobService

        with self.database.read_connection() as connection:
            self._require_active_plan(connection, plan_id)
            batch = store.next_pending_batch(connection, plan_id)
        if batch is None:
            raise PreconditionError(
                "migration_no_pending_batch",
                "没有等待处理的批次",
            )
        jobs = JobService(self.database)
        job_type = "migration.apply_batch" if apply else "migration.verify_batch"
        return jobs.enqueue(
            actor_id=actor_id,
            job_type=job_type,
            payload={"plan_id": plan_id, "seq": batch["seq"], "actor_id": actor_id},
            max_attempts=3,
            priority=50,
            idempotency_key=f"{plan_id}:{job_type}:{batch['seq']}",
        )

    # ------------------------------------------------------------------
    # 只读核对：业务结果 / 版本血缘 / 审计 / outbox
    # ------------------------------------------------------------------

    def verify_report(self, plan_ref: str | dict[str, Any]) -> dict[str, Any]:
        plan_id = _plan_id(plan_ref)
        with self.database.read_connection() as connection:
            plan = self._require_plan(connection, plan_id)
            batches = store.list_batches(connection, plan_id)
            objects = store.list_objects(connection, plan_id)
            lineage_gaps = self._lineage_check(connection, objects)
            audit_outbox = self._audit_outbox_check(connection, plan_id, objects)
            business = self._business_invariant_check(connection, plan, objects)
        applied = [b for b in batches if b["status"] == "applied"]
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "batch_summary": {
                "total": len(batches),
                "applied": len(applied),
                "failed": sum(1 for b in batches if b["status"] == "failed"),
                "pending": sum(
                    1
                    for b in batches
                    if b["status"] in {"pending", "verified"}
                ),
            },
            "object_summary": {
                "total": plan["total_objects"],
                "applied": plan["applied_objects"],
                "incompatible": plan["incompatible_objects"],
            },
            "business_result_parity": business,
            "version_lineage": lineage_gaps,
            "audit_outbox_parity": audit_outbox,
            "ok": business["ok"]
            and not lineage_gaps["gaps"]
            and audit_outbox["ok"],
        }

    def _business_invariant_check(
        self,
        connection: Any,
        plan: dict[str, Any],
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """比较业务结果：指纹必须与当前存储一致；冻结简报逐字节不变。"""
        state = _load_state(connection)
        mismatches: list[dict[str, Any]] = []
        frozen_checked = 0
        for item in objects:
            kind = item["kind"]
            object_id = item["object_id"]
            if item["status"] != "applied":
                continue
            current = state[ENTITY_CONTAINER[kind]].get(object_id)
            if current is None:
                mismatches.append({"kind": kind, "id": object_id, "reason": "缺失"})
                continue
            current_fp = engine.business_fingerprint(kind, current)
            if kind == "brief":
                frozen_checked += 1
            if item["v2_fingerprint"] and current_fp != item["v2_fingerprint"]:
                # mutable 对象切换后可能又有新写入；应与最新投影比对。
                projection = store.get_projection(
                    connection, plan["id"], kind, object_id
                )
                if projection is None or projection["fingerprint"] != current_fp:
                    mismatches.append(
                        {
                            "kind": kind,
                            "id": object_id,
                            "reason": "业务指纹偏离 v2 结论",
                        }
                    )
        return {
            "ok": not mismatches,
            "frozen_briefs_checked": frozen_checked,
            "mismatches": mismatches,
        }

    def _lineage_check(
        self,
        connection: Any,
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """版本血缘：切换前的每个修订都必须仍在 entity_versions 中可回溯。"""
        gaps: list[dict[str, Any]] = []
        checked = 0
        for item in objects:
            if item["kind"] not in engine.MUTABLE_KINDS:
                continue
            if item["status"] != "applied":
                continue
            checked += 1
            legacy = item["legacy_payload"]
            if not legacy:
                gaps.append(
                    {"kind": item["kind"], "id": item["object_id"], "reason": "缺少旧快照"}
                )
                continue
            source_revision = item["source_revision"] or 0
            row = connection.execute(
                """
                SELECT payload FROM entity_versions
                WHERE kind = ? AND id = ? AND revision = ?
                """,
                (item["kind"], item["object_id"], source_revision),
            ).fetchone()
            if row is None:
                gaps.append(
                    {
                        "kind": item["kind"],
                        "id": item["object_id"],
                        "reason": f"修订 {source_revision} 的历史版本缺失",
                    }
                )
        return {"ok": not gaps, "checked": checked, "gaps": gaps}

    def _audit_outbox_check(
        self,
        connection: Any,
        plan_id: str,
        objects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """审计与 outbox：每个切换对象都必须有配对事件，且不可变见证不产生假事件。"""
        problems: list[dict[str, Any]] = []
        switched = [
            item
            for item in objects
            if item["status"] == "applied" and item["kind"] in engine.MUTABLE_KINDS
        ]
        for item in switched:
            audit_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM audit_events
                WHERE action = 'migration.cutover'
                  AND resource_kind = ? AND resource_id = ?
                """,
                (item["kind"], item["object_id"]),
            ).fetchone()
            if int(audit_row["count"]) != 1:
                problems.append(
                    {
                        "kind": item["kind"],
                        "id": item["object_id"],
                        "reason": "缺少唯一的切换审计事件",
                        "count": int(audit_row["count"]),
                    }
                )
            outbox_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM outbox_events
                WHERE topic = ?
                  AND json_extract(payload, '$.id') = ?
                  AND json_extract(payload, '$.operation') = 'migration_cutover'
                """,
                (f"atlas.{item['kind']}.changed", item["object_id"]),
            ).fetchone()
            if int(outbox_row["count"]) != 1:
                problems.append(
                    {
                        "kind": item["kind"],
                        "id": item["object_id"],
                        "reason": "缺少唯一的切换 outbox 事件",
                        "count": int(outbox_row["count"]),
                    }
                )
        # 不可变对象不应有 cutover 审计。
        spurious = connection.execute(
            """
            SELECT resource_kind, resource_id, COUNT(*) AS count
            FROM audit_events
            WHERE action = 'migration.cutover'
              AND resource_kind IN ('comparison', 'brief')
            GROUP BY resource_kind, resource_id
            """
        ).fetchall()
        for row in spurious:
            problems.append(
                {
                    "kind": row["resource_kind"],
                    "id": row["resource_id"],
                    "reason": "不可变对象不应产生切换事件",
                    "count": int(row["count"]),
                }
            )
        return {
            "ok": not problems,
            "switched": len(switched),
            "problems": problems,
        }

    # ------------------------------------------------------------------
    # 仓储双写钩子
    # ------------------------------------------------------------------

    def before_commit(
        self,
        connection: Any,
        *,
        changes: list[tuple[str, str, dict[str, Any], str]],
        state: dict[str, Any],
        next_revision: int,
    ) -> None:
        """在业务写事务内、落库前同步维护投影。绝不允许双写改变业务结论。"""
        active = store.active_plan(connection)
        finalized_id, generation = store.finalized_generation(connection)
        if active is None and generation < 2:
            return
        canonical_map = self._canonical_map(connection)
        for kind, object_id, payload, operation in changes:
            if kind not in engine.ENTITY_KINDS or operation == "delete":
                continue
            if active is not None:
                self._shadow_upsert(
                    connection,
                    plan=active,
                    kind=kind,
                    object_id=object_id,
                    payload=payload,
                    state=state,
                    canonical_map=canonical_map,
                )
            elif generation >= 2:
                self._upgrade_live_write(
                    connection,
                    plan_id=finalized_id,
                    kind=kind,
                    object_id=object_id,
                    payload=payload,
                    state=state,
                )

    def _shadow_upsert(
        self,
        connection: Any,
        *,
        plan: dict[str, Any],
        kind: str,
        object_id: str,
        payload: dict[str, Any],
        state: dict[str, Any],
        canonical_map: dict[str, str],
    ) -> None:
        timestamp = _now()
        member = store.fetch_object(connection, plan["id"], kind, object_id)
        if member is not None and member["status"] == "applied" and kind in engine.MUTABLE_KINDS:
            raise PreconditionError(
                "migration_object_locked",
                "该对象所在批次已切换，迁移完成前不能再按旧规则写入",
                kind=kind,
                id=object_id,
            )
        v2, reasons = engine.project_object(
            kind,
            payload,
            state=state,
            canonical_map=canonical_map,
        )
        parity, _ = engine.v1_v2_parity(kind, payload, v2)
        if member is None:
            # 迁移期间新创建的对象：追加到最后一个未应用批次。
            target_batch = self._last_open_batch_seq(connection, plan["id"])
            store.upsert_object(
                connection,
                plan_id=plan["id"],
                kind=kind,
                object_id=object_id,
                status="registered",
                timestamp=timestamp,
                batch_seq=target_batch,
                source_revision=int(payload.get("revision") or 0),
                legacy_payload=_canonical(payload),
            )
        store.upsert_projection(
            connection,
            plan_id=plan["id"],
            kind=kind,
            object_id=object_id,
            generation=2,
            payload=v2,
            fingerprint=engine.business_fingerprint(kind, v2),
            timestamp=timestamp,
        )
        if reasons or not parity:
            # 业务写入产生了与 v2 不兼容的事实：拒绝该写入，保证双写同结论。
            raise PreconditionError(
                "migration_dual_write_conflict",
                "该写入无法在第 2 代规则下保持同一业务结论",
                kind=kind,
                id=object_id,
                reasons=reasons or ["v1/v2 业务指纹不一致"],
            )

    def _upgrade_live_write(
        self,
        connection: Any,
        *,
        plan_id: str | None,
        kind: str,
        object_id: str,
        payload: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        v2, reasons = engine.project_object(kind, payload, state=state)
        if reasons:
            raise PreconditionError(
                "migration_v2_rule_rejected",
                "该写入不满足已正式生效的第 2 代规则",
                kind=kind,
                id=object_id,
                reasons=reasons,
            )
        payload.clear()
        payload.update(v2)
        if plan_id:
            store.upsert_projection(
                connection,
                plan_id=plan_id,
                kind=kind,
                object_id=object_id,
                generation=2,
                payload=v2,
                fingerprint=engine.business_fingerprint(kind, v2),
                timestamp=_now(),
            )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _require_plan(
        self,
        connection: Any,
        plan_ref: str | dict[str, Any],
    ) -> dict[str, Any]:
        plan_id = plan_ref["id"] if isinstance(plan_ref, dict) else plan_ref
        plan = store.fetch_plan(connection, plan_id)
        if plan is None:
            raise NotFoundError("迁移计划", plan_id)
        return plan

    def _require_active_plan(
        self,
        connection: Any,
        plan_id: str,
    ) -> dict[str, Any]:
        plan = self._require_plan(connection, plan_id)
        if plan["status"] != "active":
            raise PreconditionError(
                "migration_plan_not_active",
                f"迁移计划处于 {plan['status']} 状态，请先恢复",
                status=plan["status"],
            )
        return plan

    def _require_batch(
        self,
        connection: Any,
        plan_id: str,
        seq: int,
    ) -> dict[str, Any]:
        batch = store.fetch_batch(connection, plan_id, seq)
        if batch is None:
            raise NotFoundError("迁移批次", f"{plan_id}/batch/{seq}")
        return batch

    def _batch_members(
        self,
        connection: Any,
        plan_id: str,
        seq: int,
    ) -> list[dict[str, Any]]:
        members = store.list_objects(connection, plan_id, batch_seq=seq)
        return [
            {"kind": item["kind"], "object_id": item["object_id"]}
            for item in members
            if item["status"] in {"registered", "verified", "incompatible"}
        ]

    @staticmethod
    def _object_status(
        connection: Any,
        plan_id: str,
        kind: str,
        object_id: str,
    ) -> str | None:
        member = store.fetch_object(connection, plan_id, kind, object_id)
        return member["status"] if member is not None else None

    def _last_open_batch_seq(
        self,
        connection: Any,
        plan_id: str,
    ) -> int:
        batches = store.list_batches(connection, plan_id)
        open_batches = [b for b in batches if b["status"] != "applied"]
        if open_batches:
            return open_batches[-1]["seq"]
        seq = (batches[-1]["seq"] + 1) if batches else 0
        store.insert_batch(connection, plan_id=plan_id, seq=seq, timestamp=_now())
        return seq

    def _maps_for_plan(
        self,
        connection: Any,
        *,
        include_pending_merges: bool = False,
    ) -> tuple[dict[str, str], dict[str, list[str]]]:
        active = store.active_plan(connection)
        merge_ops = (
            [
                op
                for op in store.list_ops(
                    connection, active["id"], op_type="merge"
                )
                if include_pending_merges or op["status"] == "applied"
            ]
            if active
            else []
        )
        state = _load_state(connection)
        return engine.build_canonical_maps(state["trees"], merge_ops)

    def _canonical_map(self, connection: Any) -> dict[str, str]:
        """驱动线上语义的规范映射，只纳入已生效（applied）的合并。"""
        canonical: dict[str, str] = {}
        state = _load_state(connection)
        for plan_row in connection.execute(
            "SELECT id, status FROM migration_plans"
        ).fetchall():
            if plan_row["status"] not in {"active", "paused", "finalized"}:
                continue
            applied_merges = [
                op
                for op in store.list_ops(
                    connection, plan_row["id"], op_type="merge"
                )
                if op["status"] == "applied"
            ]
            plan_map, _ = engine.build_canonical_maps(state["trees"], applied_merges)
            canonical.update(plan_map)
        return canonical


def _plan_id(plan_ref: str | dict[str, Any]) -> str:
    return plan_ref["id"] if isinstance(plan_ref, dict) else str(plan_ref)


def _ordered_objects(state: dict[str, Any]) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for kind in engine.MIGRATION_ORDER:
        container = ENTITY_CONTAINER[kind]
        ordered.extend((kind, identifier) for identifier in sorted(state[container]))
    return ordered


def _state_revision(connection: Any) -> int:
    return int(_get_meta(connection, "state_revision", "0") or 0)


def _bump_state_revision(connection: Any) -> None:
    _set_meta(connection, "state_revision", str(_state_revision(connection) + 1))


def _set_generation_meta(connection: Any, generation: int) -> None:
    _set_meta(connection, GENERATION_META_KEY, str(generation))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
