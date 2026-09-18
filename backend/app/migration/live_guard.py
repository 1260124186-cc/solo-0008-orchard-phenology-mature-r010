"""生产负载下的实时双写护栏。

迁移进行期间，所有常规业务写入仍走旧接口。护栏在业务事务内、提交前
对“这次写入产生的对象”同时按旧规则与新规则计算业务指纹：

- 结论一致：写入放行，并在事务后把对象的双读影子刷新到台账；
- 结论不一致：抛出错误回滚整个业务事务，迁移保持旧语义，不允许
  换用新语义的写入落地。

这样“新旧写入都必须产生同一业务结论”由写路径强制，而不是靠事后巡检。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ..errors import DomainError
from .dual_read import CONTAINER_FOR_KIND, DualReadEngine


LOGGER = logging.getLogger("orchard-phenology-migration")


class LiveWriteGuard:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self.engine = DualReadEngine()

    # ------------------------------------------------------------------
    # 事务内：提交前阻断
    # ------------------------------------------------------------------
    def verify(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        changes: list[tuple[str, str, dict[str, Any], str]],
        *,
        allow_new_rule_facts: bool = False,
    ) -> None:
        run = self._active_run(connection)
        if run is None:
            return
        reference_map = self._reference_map(connection, run["run_id"])
        blockers: list[dict[str, Any]] = []
        for kind, object_id, payload, operation in changes:
            if operation == "delete":
                continue
            result = self.engine.read_object(
                kind,
                payload,
                reference_map=reference_map,
            )
            reasons = list(result.reasons)
            if not allow_new_rule_facts:
                # 常规写入必须仍能用旧规则完整表达（v1→v2→v1 往返一致）。
                # 半级置信度、休眠芽等新规则事实不允许从旧写路径“溜进来”。
                equal, roundtrip_reasons = self.engine.round_trip_equal(kind, payload)
                if not equal:
                    reasons.extend(roundtrip_reasons)
                if not result.business_equal or roundtrip_reasons:
                    blockers.append(
                        {
                            "kind": kind,
                            "id": object_id,
                            "reasons": sorted(set(reasons)),
                        }
                    )
            elif not result.business_equal:
                # 迁移内部写入允许 v2-only 事实，但新旧业务结论仍必须等价。
                blockers.append(
                    {
                        "kind": kind,
                        "id": object_id,
                        "reasons": sorted(set(reasons))
                        or ["迁移写入的新旧业务指纹不一致"],
                    }
                )
        if blockers:
            raise DomainError(
                "migration_write_semantics_diverge",
                "写入在新旧规则下业务结论不一致，已阻止以保持迁移语义一致",
                409,
                {"run_id": run["run_id"], "objects": blockers[:20]},
            )

    # ------------------------------------------------------------------
    # 事务后：刷新双读影子（失败只记录，由启动恢复兜底，不回滚业务）
    # ------------------------------------------------------------------
    def refresh_shadow(
        self,
        changes: list[tuple[str, str, dict[str, Any], str]],
        *,
        run_id: str,
        reference_map: dict[str, str],
        actor_id: str = "",
    ) -> None:
        from .ledger import MigrationLedger

        # 迁移自身的切换/回滚/合并写入由迁移用例显式维护台账状态，
        # 这里不能把 switched/retained 覆盖回 ready。
        if actor_id.startswith("migration://"):
            return
        ledger = MigrationLedger(self.repository.database)
        for kind, object_id, _payload, operation in changes:
            tracked = ledger.get_object(run_id, kind, object_id)
            if tracked is None:
                # 迁移规划之后新建的对象：纳入跟踪，进入下一批候选。
                if operation == "delete":
                    continue
                state = self.repository.read()
                payload = state[CONTAINER_FOR_KIND[kind]].get(object_id)
                if payload is None:
                    continue
                result = self.engine.read_object(
                    kind,
                    payload,
                    reference_map=reference_map,
                )
                status = "ready" if result.business_equal else "incompatible"
                ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status=status,
                    legacy_fingerprint=result.legacy_fingerprint,
                    new_fingerprint=result.new_fingerprint,
                    legacy_payload=payload,
                    new_payload=result.upgraded_payload,
                    last_error="；".join(result.reasons) if result.reasons else None,
                )
                ledger.record_event(
                    run_id,
                    event_type="migration.shadow_new_object",
                    actor_id="live-write",
                    payload={"kind": kind, "id": object_id, "status": status},
                )
                continue
            if tracked["status"] in {"switched", "retained"}:
                # 已切换对象在迁移期间被更正：用新载荷重算，保持双读同步。
                state = self.repository.read()
                payload = state[CONTAINER_FOR_KIND[kind]].get(object_id)
                if payload is None:
                    continue
                result = self.engine.read_object(
                    kind,
                    payload,
                    reference_map=reference_map,
                )
                ledger.upsert_object(
                    run_id,
                    kind=kind,
                    object_id=object_id,
                    status=(
                        "switched"
                        if result.business_equal
                        else "incompatible"
                    ),
                    new_payload=result.upgraded_payload,
                    new_fingerprint=result.new_fingerprint,
                    last_error=(
                        None
                        if result.business_equal
                        else "；".join(result.reasons)
                    ),
                )
                ledger.record_event(
                    run_id,
                    event_type="migration.correction_live",
                    actor_id="live-write",
                    payload={
                        "kind": kind,
                        "id": object_id,
                        "consistent": result.business_equal,
                    },
                )

    def active_run_id(self) -> str | None:
        with self.repository.database.read_connection() as connection:
            run = self._active_run(connection)
        return run["run_id"] if run is not None else None

    @staticmethod
    def _active_run(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT run_id, status FROM migration_runs
            WHERE status IN ('shadow', 'running', 'paused', 'blocked', 'rolling_back')
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    @staticmethod
    def _reference_map(connection: sqlite3.Connection, run_id: str) -> dict[str, str]:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?",
            (f"migration.{run_id}.reference_map",),
        ).fetchone()
        if row is None:
            return {}
        return {
            str(key): str(value)
            for key, value in json.loads(row["value"]).items()
        }
