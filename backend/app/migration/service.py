"""迁移用例编排：规划、双读、分批切换、停止、回滚、更正与合并。"""

from __future__ import annotations

from typing import Any

from ..errors import ConflictError, DomainError, NotFoundError, PreconditionError
from ..persistence.repository import ENTITY_KINDS
from .dual_read import CONTAINER_FOR_KIND, DualReadEngine
from .grants import GrantRecord, revocation_targets, verify_grant_equivalence
from .ledger import MigrationLedger
from .phenology_v2 import V2_CHANGE_SET
from .references import (
    apply_merge_to_tree,
    build_reference_map,
    merge_has_new_rule_facts,
    validate_merge,
)
from .verifier import MigrationVerifier


SWITCH_REASON = "migration_switch"
ACTIVE_STATUSES = {"shadow", "running"}


class MigrationService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self.database = repository.database
        self.ledger = MigrationLedger(self.database)
        self.engine = DualReadEngine()
        self.verifier = MigrationVerifier(self.database, self.engine)

    # ------------------------------------------------------------------
    # 创建与规划（全量双读，登记不兼容集合）
    # ------------------------------------------------------------------
    def create_plan(
        self,
        *,
        actor_id: str,
        batch_size: int = 20,
        change_set_id: str = V2_CHANGE_SET.change_set_id,
    ) -> dict[str, Any]:
        change_set = self._change_set(change_set_id)
        if batch_size < 1 or batch_size > 1000:
            raise DomainError(
                "invalid_batch_size",
                "批大小必须在 1 到 1000 之间",
                422,
            )
        if self.ledger.active_run() is not None:
            raise ConflictError(
                "migration_already_active",
                "已有进行中的迁移活动，请先完成、停止或回滚",
            )
        run = self.ledger.create_run(
            change_set_id=change_set.change_set_id,
            from_version=change_set.from_version,
            to_version=change_set.to_version,
            batch_size=batch_size,
            created_by=actor_id,
        )
        run_id = run["run_id"]
        state = self.repository.read()
        reference_map = self.ledger.get_reference_map(run_id)

        incompatible: list[dict[str, Any]] = []
        total = 0
        for container, kind in ENTITY_KINDS.items():
            for object_id, payload in state[container].items():
                total += 1
                result = self.engine.read_object(
                    kind,
                    payload,
                    reference_map=reference_map,
                )
                equal, roundtrip_reasons = self.engine.round_trip_equal(kind, payload)
                reasons = sorted(set(result.reasons + roundtrip_reasons))
                status = "ready" if result.business_equal and equal else "incompatible"
                if status == "incompatible":
                    incompatible.append(
                        {"kind": kind, "id": object_id, "reasons": reasons}
                    )
                self.ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status=status,
                    legacy_fingerprint=result.legacy_fingerprint,
                    new_fingerprint=result.new_fingerprint,
                    legacy_payload=payload,
                    new_payload=result.upgraded_payload,
                    last_error="；".join(reasons) if reasons else None,
                )

        # 授权范围双读：对所有现有授权和一组覆盖通配/扁平/层级的探测求等式。
        grant_problems = self._verify_grants()
        for problem in grant_problems:
            incompatible.append(
                {
                    "kind": "grant",
                    "id": f"{problem['probe'].get('capability')}",
                    "reasons": ["新旧授权决定不一致"],
                    "detail": problem,
                }
            )

        # 冻结分析：所有既有比较与简报在新规则下必须可复算。
        frozen = self.verifier.frozen_analysis_check(
            state,
            reference_map=reference_map,
        )
        for problem in frozen["problems"]:
            incompatible.append(
                {
                    "kind": problem["kind"],
                    "id": problem["id"],
                    "reasons": ["冻结事实在新规则下结论改变"],
                }
            )

        # 规划批次（只排可切换对象，按 kind,id 稳定排序），并把批次号写回对象行。
        ready_objects = self._ready_object_keys(run_id)
        for index in range(0, len(ready_objects), batch_size):
            batch_no = index // batch_size + 1
            members = ready_objects[index : index + batch_size]
            self.ledger.plan_batch(run_id, batch_no, members)
            for kind, object_id in members:
                self.ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status="ready",
                    batch_no=batch_no,
                )

        run = self.ledger.update_run(
            run_id,
            status="shadow",
            total_objects=total,
            incompatible_objects=len(
                [item for item in incompatible if item["kind"] != "grant"]
            ),
        )
        self.ledger.record_event(
            run_id,
            event_type="migration.planned",
            actor_id=actor_id,
            payload={
                "total": total,
                "ready": len(ready_objects),
                "incompatible": incompatible,
                "grant_divergences": grant_problems,
                "frozen": frozen,
                "change_set": change_set.describe(),
            },
        )
        return self.status(run_id)

    # ------------------------------------------------------------------
    # 执行下一批
    # ------------------------------------------------------------------
    def run_next_batch(self, *, actor_id: str) -> dict[str, Any]:
        run = self._require_active()
        run_id = run["run_id"]
        if run["stop_requested"]:
            raise PreconditionError(
                "migration_stop_requested",
                "迁移已被请求停止，不能继续切换",
            )
        if run["status"] == "shadow":
            run = self.ledger.update_run(run_id, status="running")
        next_batch = self._next_planned_batch(run_id)
        if next_batch is None:
            return self.status(run_id)
        batch_no = int(next_batch["batch_no"])

        next_batch = self._next_planned_batch(run_id)
        if next_batch is None:
            return self.status(run_id)
        batch_no = int(next_batch["batch_no"])

        # 开始批次（独立事务），随后用只读连接拍“切换前”快照。
        # 不在此处持有写事务，避免与批次内每个对象的正规业务事务嵌套死锁。
        self.ledger.start_batch(run_id, batch_no)
        checkpoint: dict[str, Any] = {"batch_no": batch_no}
        with self.database.read_connection() as connection:
            audit_before = self.verifier.audit_snapshot(connection)
            outbox_before = self.verifier.outbox_snapshot(connection)

        batch_objects = self.ledger.list_objects(
            run_id,
            batch_no=batch_no,
            limit=run["batch_size"] + 1,
        )["items"]

        switched: list[tuple[str, str]] = []
        failures: list[dict[str, Any]] = []
        for item in batch_objects:
            kind = item["kind"]
            object_id = item["id"]
            try:
                self._switch_object(run_id, kind, object_id, actor_id=actor_id)
                switched.append((kind, object_id))
            except Exception as exc:  # 单对象失败被隔离，不影响同批其他对象
                failures.append({"kind": kind, "id": object_id, "reason": str(exc)})
                self.ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status="failed",
                    last_error=str(exc),
                    increment_attempts=True,
                )

        state = self.repository.read()
        reference_map = self.ledger.get_reference_map(run_id)
        business_after = self.verifier.business_checkpoint(
            state,
            reference_map=reference_map,
        )

        with self.database.read_connection() as connection:
            audit_after = self.verifier.audit_snapshot(connection)
            outbox_after = self.verifier.outbox_snapshot(connection)

        audit_result = self.verifier.audit_check(
            audit_before,
            audit_after,
            expected_migration_events=len(switched),
        )
        outbox_result = self.verifier.outbox_check(
            outbox_before,
            outbox_after,
            expected_changed=switched,
        )

        lineage_problems: list[dict[str, Any]] = []
        if switched:
            with self.database.read_connection() as connection:
                lineage_result = self.verifier.lineage_check(connection, switched)
            lineage_problems = lineage_result["problems"]

        batch_ok = (
            not failures
            and business_after["equal"]
            and not audit_result["problems"]
            and not outbox_result["problems"]
            and not lineage_problems
        )
        checkpoint.update(
            {
                "after": business_after,
                "switched": [
                    {"kind": kind, "id": object_id} for kind, object_id in switched
                ],
                "failures": failures,
                "audit": audit_result,
                "outbox": outbox_result,
                "lineage_problems": lineage_problems,
            }
        )

        if batch_ok:
            self.ledger.finish_batch(
                run_id,
                batch_no,
                status="succeeded",
                checkpoint=checkpoint,
            )
            counts = self.ledger.counts_by_status(run_id)
            self.ledger.update_run(
                run_id,
                switched_objects=counts.get("switched", 0),
                cursor=f"batch:{batch_no}",
            )
            self.ledger.record_event(
                run_id,
                event_type="migration.batch_succeeded",
                actor_id=actor_id,
                payload={"batch_no": batch_no, "switched": len(switched)},
            )
        else:
            # 任何失败只回滚这一批尚未提交的切换，已成功批次不受影响。
            self._revert_batch(run_id, batch_no, actor_id=actor_id)
            self.ledger.finish_batch(
                run_id,
                batch_no,
                status="failed",
                checkpoint=checkpoint,
                error=self._summarize_failure(
                    failures,
                    business_after,
                    audit_result,
                    outbox_result,
                    lineage_problems,
                ),
            )
            self.ledger.update_run(
                run_id,
                status="blocked",
                last_error=f"批次 {batch_no} 校验失败，已隔离",
            )
            self.ledger.record_event(
                run_id,
                event_type="migration.batch_failed",
                actor_id=actor_id,
                payload={"batch_no": batch_no, "checkpoint": checkpoint},
            )
        return self.status(run_id)

    def _switch_object(
        self,
        run_id: str,
        kind: str,
        object_id: str,
        *,
        actor_id: str,
    ) -> None:
        tracked = self.ledger.get_object(run_id, kind, object_id)
        if tracked is None or tracked["status"] not in {"ready", "failed"}:
            raise PreconditionError(
                "object_not_switchable",
                "对象不在可切换状态",
                kind=kind,
                id=object_id,
                status=tracked["status"] if tracked else None,
            )
        state = self.repository.read()
        container = CONTAINER_FOR_KIND[kind]
        current = state[container].get(object_id)
        if current is None:
            raise NotFoundError("业务对象", f"{kind}:{object_id}")

        reference_map = self.ledger.get_reference_map(run_id)
        result = self.engine.read_object(
            kind,
            current,
            reference_map=reference_map,
        )
        if not result.business_equal or result.upgraded_payload is None:
            self.ledger.upsert_object(
                run_id,
                kind=kind,
                object_id=object_id,
                status="incompatible",
                legacy_fingerprint=result.legacy_fingerprint,
                new_fingerprint=result.new_fingerprint,
                last_error="；".join(result.reasons) or "双读业务指纹不一致",
            )
            raise PreconditionError(
                "object_incompatible",
                "对象在新旧规则下业务结论不一致，已转入不兼容集合",
                kind=kind,
                id=object_id,
            )

        self.ledger.upsert_object(
            run_id,
            kind=kind,
            object_id=object_id,
            status="switching",
        )
        # 切换通过 Repository 的正规写入路径提交：业务实体、对象版本、
        # 审计与 outbox 在同一事务内落账，迁移不绕过任何一致性约束。
        # 即使投影的业务字段字节相同，schema_version 的变化也是一次正式
        # 修订：必须 bump revision，使 entity_versions 留下可追溯的切换版本。
        switched_payload = self._stamp_migration_revision(
            result.upgraded_payload,
            current_revision=int(current.get("revision") or 1),
            run_id=run_id,
        )

        def apply_to(state_view: dict[str, Any]) -> dict[str, Any]:
            state_view[container][object_id] = switched_payload
            return switched_payload

        from ..security.context import request_scope, RequestContext

        context = RequestContext(
            actor_id=actor_id,
            request_method="PUT",
            request_path=f"/api/migration/{run_id}/batches/switch",
            route_template="/api/migration/{run_id}/switch",
        )
        with request_scope(context):
            self.repository.atomic_update_migration(
                apply_to,
                reason=SWITCH_REASON,
                run_id=run_id,
                kind=kind,
                object_id=object_id,
            )

        self.ledger.upsert_object(
            run_id,
            kind=kind,
            object_id=object_id,
            status="switched",
            legacy_fingerprint=result.legacy_fingerprint,
            new_fingerprint=result.new_fingerprint,
            legacy_payload=current,
            new_payload=switched_payload,
            batch_no=tracked["batch_no"],
        )

    @staticmethod
    def _stamp_migration_revision(
        payload: dict[str, Any] | None,
        *,
        current_revision: int,
        run_id: str,
    ) -> dict[str, Any]:
        from ..domain.plot_rules import now_iso

        assert payload is not None
        stamped = dict(payload)
        stamped["revision"] = current_revision + 1
        stamped["updated_at"] = now_iso()
        stamped.setdefault("migration", {})
        stamped["migration"] = {
            **stamped.get("migration", {}),
            "run_id": run_id,
        }
        return stamped

    # ------------------------------------------------------------------
    # 停止、恢复、回滚
    # ------------------------------------------------------------------
    def request_stop(self, *, actor_id: str) -> dict[str, Any]:
        run = self._require_active_or_paused()
        self.ledger.update_run(run["run_id"], stop_requested=1, status="paused")
        self.ledger.record_event(
            run["run_id"],
            event_type="migration.stopped",
            actor_id=actor_id,
            payload={"switched_objects": run["switched_objects"]},
        )
        return self.status(run["run_id"])

    def resume(self, *, actor_id: str) -> dict[str, Any]:
        run = self.ledger.get_run(self._require_any_active_id())
        if run["status"] not in {"paused", "shadow"}:
            raise PreconditionError(
                "migration_not_paused",
                "只有已停止（暂停）的迁移可以继续",
                status=run["status"],
            )
        if run["stop_requested"] and run["status"] == "paused":
            # 允许停止后恢复继续（取消停止标记），由调用方显式 resume。
            self.ledger.update_run(
                run["run_id"],
                stop_requested=0,
                status="running",
            )
        self.ledger.record_event(
            run["run_id"],
            event_type="migration.resumed",
            actor_id=actor_id,
            payload={},
        )
        return self.status(run["run_id"])

    def rollback(self, *, actor_id: str) -> dict[str, Any]:
        """回滚尚未完成切换的批次。

        已经正式提交、且依赖新规则的事实（规范树引用、半级置信度、休眠芽）
        不被删除，而是转入 retained 集合并记录原因；其余已切换对象按
        batch_no 逆序恢复为切换前的 v1 载荷。
        """

        run = self.ledger.get_run(self._require_any_active_id())
        run_id = run["run_id"]
        self.ledger.update_run(run_id, status="rolling_back", rollback_requested=1)
        batches = self.ledger.list_batches(run_id)["items"]
        succeeded = sorted(
            (item for item in batches if item["status"] == "succeeded"),
            key=lambda item: item["batch_no"],
            reverse=True,
        )
        retained: list[dict[str, Any]] = []
        reverted_objects: list[tuple[str, str]] = []
        for batch in succeeded:
            batch_retained, batch_reverted = self._rollback_batch(
                run_id,
                int(batch["batch_no"]),
                actor_id=actor_id,
            )
            retained.extend(batch_retained)
            reverted_objects.extend(batch_reverted)

        if retained:
            self.ledger.add_retained(run_id, retained)
        counts = self.ledger.counts_by_status(run_id)
        self.ledger.update_run(
            run_id,
            status="rolled_back",
            retained_objects=len(self.ledger.list_retained(run_id)),
            completed_at=None,
        )
        self.ledger.record_event(
            run_id,
            event_type="migration.rolled_back",
            actor_id=actor_id,
            payload={
                "reverted_objects": [
                    {"kind": kind, "id": object_id}
                    for kind, object_id in reverted_objects
                ],
                "retained": retained,
            },
        )
        return self.status(run_id)

    def _rollback_batch(
        self,
        run_id: str,
        batch_no: int,
        *,
        actor_id: str,
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        items = self.ledger.list_objects(
            run_id,
            status="switched",
            batch_no=batch_no,
            limit=10_000,
        )["items"]
        retained: list[dict[str, Any]] = []
        reverted: list[tuple[str, str]] = []
        for item in items:
            kind = item["kind"]
            object_id = item["id"]
            new_payload = item["new_payload"] or {}
            downgrade_reasons: list[str] = []
            self.engine.downgrade_payload(kind, new_payload, reasons=downgrade_reasons)
            if downgrade_reasons:
                # 已正式提交且依赖新规则的事实保留，不随回滚删除。
                reason_text = "；".join(downgrade_reasons)
                retained.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "batch_no": batch_no,
                        "reason": reason_text,
                    }
                )
                self.ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status="retained",
                    retained_reason=reason_text,
                )
                self.ledger.record_event(
                    run_id,
                    event_type="migration.object_retained",
                    actor_id=actor_id,
                    payload={
                        "kind": kind,
                        "id": object_id,
                        "reason": reason_text,
                    },
                )
                continue
            legacy_payload = item["legacy_payload"]
            if legacy_payload is None:
                retained.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "batch_no": batch_no,
                        "reason": "缺少切换前快照，无法安全回滚",
                    }
                )
                continue
            self._restore_object(
                run_id,
                kind,
                object_id,
                legacy_payload,
                actor_id=actor_id,
            )
            self.ledger.upsert_object(
                run_id,
                kind=kind,
                object_id=object_id,
                status="pending",
                new_payload=None,
            )
            reverted.append((kind, object_id))
        self.ledger.finish_batch(
            run_id,
            batch_no,
            status="reverted",
            checkpoint={"reverted": len(reverted), "retained": len(retained)},
        )
        return retained, reverted

    def _revert_batch(
        self,
        run_id: str,
        batch_no: int,
        *,
        actor_id: str,
    ) -> None:
        """失败批次：恢复其中已切换对象到切换前快照（此时都未完成整体切换）。"""

        items = self.ledger.list_objects(
            run_id,
            status="switched",
            batch_no=batch_no,
            limit=10_000,
        )["items"]
        for item in items:
            if item["legacy_payload"] is not None:
                self._restore_object(
                    run_id,
                    item["kind"],
                    item["id"],
                    item["legacy_payload"],
                    actor_id=actor_id,
                )
            self.ledger.upsert_object(
                run_id,
                kind=item["kind"],
                object_id=item["id"],
                status="ready",
            )

    def _restore_object(
        self,
        run_id: str,
        kind: str,
        object_id: str,
        legacy_payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> None:
        container = CONTAINER_FOR_KIND[kind]
        # 恢复为切换前内容，但修订号在当前实体之后继续递增，
        # 保证 entity_versions 版本序列单调连续（回滚也是一次正式修订）。
        current = self.repository.read()[container].get(object_id) or {}
        restored = self._stamp_migration_revision(
            {key: value for key, value in legacy_payload.items() if key != "migration"},
            current_revision=int(current.get("revision") or legacy_payload.get("revision") or 1),
            run_id=run_id,
        )

        def apply_to(state_view: dict[str, Any]) -> dict[str, Any]:
            state_view[container][object_id] = restored
            return restored

        from ..security.context import RequestContext, request_scope

        context = RequestContext(
            actor_id=actor_id,
            request_method="PUT",
            request_path=f"/api/migration/{run_id}/rollback",
            route_template="/api/migration/{run_id}/rollback",
        )
        with request_scope(context):
            self.repository.atomic_update_migration(
                apply_to,
                reason="migration_rollback",
                run_id=run_id,
                kind=kind,
                object_id=object_id,
            )

    # ------------------------------------------------------------------
    # 完成切换（cutover）
    # ------------------------------------------------------------------
    def complete(self, *, actor_id: str) -> dict[str, Any]:
        run = self.ledger.get_run(self._require_any_active_id())
        run_id = run["run_id"]
        pending = self._next_planned_batch(run_id)
        if pending is not None:
            raise PreconditionError(
                "migration_not_all_switched",
                "仍有未切换批次，不能完成迁移",
                next_batch=pending["batch_no"],
            )
        if run["incompatible_objects"]:
            raise PreconditionError(
                "migration_has_incompatible",
                "存在不兼容对象，需先更正或显式排除后再完成",
                count=run["incompatible_objects"],
            )
        state = self.repository.read()
        reference_map = self.ledger.get_reference_map(run_id)
        comparison = self.engine.compare_states(
            state,
            state,
            reference_map=reference_map,
        )
        # cutover 前做最终全量四维校验。
        final_business = self.verifier.business_checkpoint(
            state,
            reference_map=reference_map,
        )
        frozen = self.verifier.frozen_analysis_check(
            state,
            reference_map=reference_map,
        )
        if not final_business["equal"] or frozen["problems"]:
            raise PreconditionError(
                "migration_final_verification_failed",
                "最终全量校验未通过，迁移保持在切换前状态",
                business=final_business,
                frozen=frozen,
            )

        # cutover 不重写业务实体载荷（它们已经是 v2 形态且与 v1 业务等价），
        # 只在台账和 meta 上记录“自此时点起按 v2 解释”。这样历史时点视图、
        # 冻结分析和启动恢复都不会因为一次全局版本翻转而改变结论。
        self.database.initialize()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO meta (key, value) VALUES ('active_domain_version', '2')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            connection.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, 'true')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"migration.{run_id}.cutover",),
            )
        self.ledger.update_run(
            run_id,
            status="completed",
            completed_at=_now_iso(),
        )
        self.ledger.record_event(
            run_id,
            event_type="migration.completed",
            actor_id=actor_id,
            payload={
                "reference_map": reference_map,
                "business": final_business,
                "frozen": frozen,
            },
        )
        return self.status(run_id)

    # ------------------------------------------------------------------
    # 迁移期间的更正、合并、授权撤销
    # ------------------------------------------------------------------
    def record_correction(
        self,
        *,
        actor_id: str,
        kind: str,
        object_id: str,
        note: str,
    ) -> dict[str, Any]:
        """业务方在迁移期间通过常规接口更正了对象；迁移重新对其双读。"""

        run = self._require_active_or_paused()
        run_id = run["run_id"]
        state = self.repository.read()
        container = CONTAINER_FOR_KIND.get(kind)
        if container is None or object_id not in state[container]:
            raise NotFoundError("业务对象", f"{kind}:{object_id}")
        payload = state[container][object_id]
        reference_map = self.ledger.get_reference_map(run_id)
        result = self.engine.read_object(
            kind,
            payload,
            reference_map=reference_map,
        )
        equal, roundtrip_reasons = self.engine.round_trip_equal(kind, payload)
        reasons = sorted(set(result.reasons + roundtrip_reasons))
        status = "ready" if result.business_equal and equal else "incompatible"
        tracked = self.ledger.get_object(run_id, kind, object_id)
        self.ledger.upsert_object(
            run_id,
            kind=kind,
            object_id=object_id,
            status=status,
            legacy_fingerprint=result.legacy_fingerprint,
            new_fingerprint=result.new_fingerprint,
            legacy_payload=payload,
            new_payload=result.upgraded_payload,
            batch_no=tracked["batch_no"] if tracked else None,
            last_error="；".join(reasons) if reasons else None,
        )
        self.ledger.record_event(
            run_id,
            event_type="migration.correction",
            actor_id=actor_id,
            payload={
                "kind": kind,
                "id": object_id,
                "note": note,
                "new_status": status,
            },
        )
        return self.ledger.get_object(run_id, kind, object_id)  # type: ignore[return-value]

    def merge_trees(
        self,
        *,
        actor_id: str,
        canonical_tree_id: str,
        member_tree_ids: list[str],
    ) -> dict[str, Any]:
        run = self._require_active_or_paused()
        run_id = run["run_id"]
        state = self.repository.read()
        canonical = state["trees"].get(canonical_tree_id)
        members = [state["trees"].get(mid) for mid in member_tree_ids]
        if canonical is None:
            raise NotFoundError("规范植株", canonical_tree_id)
        missing = [mid for mid, item in zip(member_tree_ids, members) if item is None]
        if missing:
            raise NotFoundError("成员植株", ",".join(missing))
        observations = [
            item
            for item in state["observations"].values()
            if item["tree_id"] in {canonical_tree_id, *member_tree_ids}
        ]
        validate_merge(
            canonical=canonical,
            members=[item for item in members if item is not None],
            observations=observations,
        )
        reference_map = self.ledger.get_reference_map(run_id)
        additions = build_reference_map(
            canonical_id=canonical_tree_id,
            member_ids=member_tree_ids,
        )

        # 合并前先证明：引用归一不会改变既有业务结论。
        before = self.engine.compare_states(state, state, reference_map=reference_map)
        merged_map = {**reference_map, **additions}
        after = self.engine.compare_states(state, state, reference_map=merged_map)
        if not before["equal"] or not after["equal"]:
            raise PreconditionError(
                "merge_changes_conclusion",
                "合并会改变既有业务结论，已拒绝",
            )

        merged_tree = apply_merge_to_tree(
            canonical,
            [item for item in members if item is not None],
        )

        def apply_to(state_view: dict[str, Any]) -> dict[str, Any]:
            state_view["trees"][canonical_tree_id] = merged_tree
            return merged_tree

        from ..security.context import RequestContext, request_scope

        context = RequestContext(
            actor_id=actor_id,
            request_method="PUT",
            request_path=f"/api/migration/{run_id}/merge",
            route_template="/api/migration/{run_id}/merge",
        )
        with request_scope(context):
            self.repository.atomic_update_migration(
                apply_to,
                reason="migration_merge",
                run_id=run_id,
                kind="tree",
                object_id=canonical_tree_id,
            )

        self.ledger.update_reference_map(run_id, additions)
        # 规范树现在依赖新规则，记录为保留事实并置为 switched 形态。
        upgraded_observations = [
            self.engine.upgrade_payload("observation", item)
            for item in observations
        ]
        new_facts = merge_has_new_rule_facts(
            merged_canonical=merged_tree,
            upgraded_observations=upgraded_observations,
        )
        tracked = self.ledger.get_object(run_id, "tree", canonical_tree_id)
        canonical_result = self.engine.read_object(
            "tree",
            merged_tree,
            reference_map=merged_map,
        )
        # 规范树是显式的 v2-only 干预动作：放入保留批次（0），不参与常规切换。
        canonical_batch = 0
        self.ledger.plan_batch(
            run_id,
            canonical_batch,
            [("tree", canonical_tree_id)],
        )
        self.ledger.finish_batch(
            run_id,
            canonical_batch,
            status="succeeded",
            checkpoint={
                "kind": "merge",
                "canonical_tree_id": canonical_tree_id,
                "member_tree_ids": member_tree_ids,
                "retained": True,
            },
        )
        self.ledger.upsert_object(
            run_id,
            kind="tree",
            object_id=canonical_tree_id,
            status="switched",
            legacy_fingerprint=canonical_result.legacy_fingerprint,
            new_fingerprint=canonical_result.new_fingerprint,
            legacy_payload=canonical,
            new_payload=merged_tree,
            batch_no=canonical_batch,
            retained_reason=None,
        )
        for member_id in member_tree_ids:
            member_result = self.engine.read_object(
                "tree",
                state["trees"][member_id],
                reference_map=merged_map,
            )
            member_tracked = self.ledger.get_object(run_id, "tree", member_id)
            self.ledger.upsert_object(
                run_id,
                kind="tree",
                object_id=member_id,
                status="ready",
                legacy_fingerprint=member_result.legacy_fingerprint,
                new_fingerprint=member_result.new_fingerprint,
                batch_no=(
                    member_tracked["batch_no"]
                    if member_tracked and member_tracked["batch_no"]
                    else None
                ),
            )
        self.ledger.add_retained(
            run_id,
            [
                {
                    "kind": "tree",
                    "id": canonical_tree_id,
                    "reason": "规范树引用是已提交的新规则事实："
                    + "、".join(new_facts),
                }
            ],
        )
        self.ledger.record_event(
            run_id,
            event_type="migration.merge",
            actor_id=actor_id,
            payload={
                "canonical_tree_id": canonical_tree_id,
                "member_tree_ids": member_tree_ids,
                "reference_map_additions": additions,
                "new_rule_facts": new_facts,
            },
        )
        counts = self.ledger.counts_by_status(run_id)
        self.ledger.update_run(
            run_id,
            switched_objects=counts.get("switched", 0),
            retained_objects=len(self.ledger.list_retained(run_id)),
        )
        return {
            "canonical_tree_id": canonical_tree_id,
            "merged_tree": merged_tree,
            "reference_map": merged_map,
            "new_rule_facts": new_facts,
        }

    def revoke_grant(
        self,
        *,
        actor_id: str,
        grant_id: str,
    ) -> dict[str, Any]:
        """迁移期间撤销授权：新旧表示下等价的授权必须同时失效。"""

        run = self._require_active_or_paused()
        run_id = run["run_id"]
        identity = self._identity_service()
        grants_view = identity.list_grants()["items"]
        target = next((item for item in grants_view if item["id"] == grant_id), None)
        if target is None:
            raise NotFoundError("授权", grant_id)
        now_active = [
            item for item in grants_view if item["revoked_at"] is None
        ]
        from .grants import is_hierarchical_scope

        records = [
            GrantRecord(
                actor_id=item["actor_id"],
                capability=item["capability"],
                resource_kind=item["resource_kind"],
                resource_id=item["resource_id"],
                revoked=item["revoked_at"] is not None,
                hierarchical=is_hierarchical_scope(item["resource_id"]),
            )
            for item in now_active
        ]
        equivalents = revocation_targets(
            records,
            actor_id=target["actor_id"],
            capability=target["capability"],
            resource_kind=target["resource_kind"],
            resource_id=target["resource_id"],
        )
        equivalent_keys = {
            (
                item.actor_id,
                item.capability,
                item.resource_kind,
                item.resource_id,
            )
            for item in equivalents
        }
        equivalent_grants = [
            item
            for item in now_active
            if (
                item["actor_id"],
                item["capability"],
                item["resource_kind"],
                item["resource_id"],
            )
            in equivalent_keys
        ]
        revoked_results = [identity.revoke(item["id"]) for item in equivalent_grants]
        if not any(item["id"] == grant_id for item in revoked_results):
            revoked_results.append(identity.revoke(grant_id))
        self.ledger.record_event(
            run_id,
            event_type="migration.grant_revoked",
            actor_id=actor_id,
            payload={
                "requested_grant_id": grant_id,
                "revoked_grant_ids": [item["id"] for item in revoked_results],
            },
        )
        return {
            "revoked_grants": revoked_results,
            "decision_kept_consistent": True,
        }

    # ------------------------------------------------------------------
    # 状态与报告
    # ------------------------------------------------------------------
    def status(self, run_id: str) -> dict[str, Any]:
        run = self.ledger.get_run(run_id)
        counts = self.ledger.counts_by_status(run_id)
        batches = self.ledger.list_batches(run_id)["items"]
        return {
            "run": run,
            "object_counts": counts,
            "batches": batches,
            "retained": self.ledger.list_retained(run_id),
            "reference_map": self.ledger.get_reference_map(run_id),
            "change_set": self._change_set(run["change_set"]).describe(),
        }

    def incompatible_objects(self, run_id: str) -> dict[str, Any]:
        return self.ledger.list_objects(run_id, status="incompatible", limit=5000)

    def failures(self, run_id: str) -> dict[str, Any]:
        failed = self.ledger.list_objects(run_id, status="failed", limit=5000)
        blocked_batches = [
            item
            for item in self.ledger.list_batches(run_id)["items"]
            if item["status"] == "failed"
        ]
        return {"objects": failed["items"], "blocked_batches": blocked_batches}

    def retry_failed_batch(self, *, batch_no: int, actor_id: str) -> dict[str, Any]:
        run = self._require_active_or_paused()
        run_id = run["run_id"]
        batch = next(
            (
                item
                for item in self.ledger.list_batches(run_id)["items"]
                if item["batch_no"] == batch_no
            ),
            None,
        )
        if batch is None:
            raise NotFoundError("迁移批次", str(batch_no))
        if batch["status"] != "failed":
            raise PreconditionError(
                "batch_not_failed",
                "只有失败批次可以重试",
                status=batch["status"],
            )
        # 把失败对象复位为 ready，批次回到 planned 以重新执行。
        for item in self.ledger.list_objects(
            run_id,
            status="failed",
            batch_no=batch_no,
            limit=10_000,
        )["items"]:
            self.ledger.upsert_object(
                run_id,
                kind=item["kind"],
                object_id=item["id"],
                status="ready",
                last_error=None,
            )
        self.ledger.finish_batch(
            run_id,
            batch_no,
            status="planned",
            checkpoint={**batch["checkpoint"], "retried": True},
            error=None,
        )
        self.ledger.update_run(run_id, status="running", last_error=None)
        self.ledger.record_event(
            run_id,
            event_type="migration.batch_retried",
            actor_id=actor_id,
            payload={"batch_no": batch_no},
        )
        return self.run_next_batch(actor_id=actor_id)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _change_set(self, change_set_id: str) -> Any:
        from . import CHANGE_SETS

        change_set = CHANGE_SETS.get(change_set_id)
        if change_set is None:
            raise NotFoundError("变更集", change_set_id)
        return change_set

    def _ready_object_keys(self, run_id: str) -> list[tuple[str, str]]:
        rows = self.ledger.list_objects(run_id, status="ready", limit=1_000_000)["items"]
        return sorted((item["kind"], item["id"]) for item in rows)

    def _next_planned_batch(self, run_id: str) -> dict[str, Any] | None:
        batches = self.ledger.list_batches(run_id)["items"]
        return next(
            (item for item in batches if item["status"] == "planned"),
            None,
        )

    def _require_active(self) -> dict[str, Any]:
        run = self.ledger.active_run()
        if run is None or run["status"] not in ACTIVE_STATUSES:
            raise PreconditionError(
                "migration_not_running",
                "没有处于双读或切换阶段的迁移活动",
                status=run["status"] if run else None,
            )
        return run

    def _require_active_or_paused(self) -> dict[str, Any]:
        run = self.ledger.active_run()
        if run is None:
            raise PreconditionError("migration_not_active", "没有进行中的迁移活动")
        return run

    def _require_any_active_id(self) -> str:
        run = self.ledger.active_run()
        if run is None:
            raise PreconditionError("migration_not_active", "没有进行中的迁移活动")
        return run["run_id"]

    def _verify_grants(self) -> list[dict[str, Any]]:
        identity = self._identity_service()
        grants_raw = identity.list_grants()["items"]
        from .grants import is_hierarchical_scope

        grants = [
            GrantRecord(
                actor_id=item["actor_id"],
                capability=item["capability"],
                resource_kind=item["resource_kind"],
                resource_id=item["resource_id"],
                revoked=item["revoked_at"] is not None,
                hierarchical=is_hierarchical_scope(item["resource_id"]),
            )
            for item in grants_raw
        ]
        probes = self._grant_probes(grants_raw)
        return verify_grant_equivalence(grants, probes)

    @staticmethod
    def _grant_probes(grants_raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        actors = {item["actor_id"] for item in grants_raw}
        kinds = {item["resource_kind"] for item in grants_raw if item["resource_kind"] != "*"}
        capabilities = {
            item["capability"] for item in grants_raw if item["capability"] != "*"
        }
        probes: list[dict[str, Any]] = []
        for actor in actors:
            for kind in sorted(kinds) or ["plot"]:
                for capability in sorted(capabilities) or [f"{kind}:read"]:
                    for resource_id in ("*", "plot/demo-1", "plot/demo-1/tree/t-1"):
                        probes.append(
                            {
                                "actor_active": True,
                                "capability": capability,
                                "resource_kind": kind,
                                "resource_id": resource_id,
                            }
                        )
        return probes

    def _identity_service(self) -> Any:
        from ..security.management import IdentityService

        return IdentityService(self.database)

    @staticmethod
    def _summarize_failure(
        failures: list[dict[str, Any]],
        business: dict[str, Any],
        audit: dict[str, Any],
        outbox: dict[str, Any],
        lineage: list[dict[str, Any]],
    ) -> str:
        parts: list[str] = []
        if failures:
            parts.append(f"对象失败 {len(failures)} 项")
        if not business.get("equal"):
            parts.append(f"业务结论不一致：{business.get('mismatched_kinds')}")
        if audit.get("problems"):
            parts.append("审计校验失败")
        if outbox.get("problems"):
            parts.append("outbox 校验失败")
        if lineage:
            parts.append("版本血缘异常")
        return "；".join(parts) or "批次校验失败"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
