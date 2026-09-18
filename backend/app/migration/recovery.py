"""启动恢复与对账。

迁移是分批、跨进程推进的，任何时刻都可能崩溃或被重启。恢复逻辑保证
“启动恢复不会因模式切换而改变业务结论”：

- 卡在 running 的批次没有提交检查点 -> 回到 planned，已切换对象重新核验；
- 台账影子与真实实体做全量对账，任何双读漂移都登记为不兼容；
- 业务指纹（观察/比较/简报）与切换前快照一致才允许继续；
- 恢复本身只改迁移台账，不改业务实体。
"""

from __future__ import annotations

import logging
from typing import Any

from .dual_read import DualReadEngine
from ..persistence.repository import ENTITY_KINDS
from .ledger import MigrationLedger


LOGGER = logging.getLogger("orchard-phenology-migration-recovery")


class MigrationRecovery:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self.ledger = MigrationLedger(repository.database)
        self.engine = DualReadEngine()

    def recover(self) -> dict[str, Any] | None:
        run = self.ledger.active_run()
        if run is None:
            return None
        run_id = run["run_id"]
        report: dict[str, Any] = {
            "run_id": run_id,
            "previous_status": run["status"],
            "reset_batches": [],
            "reconciled_objects": 0,
            "drifts": [],
        }

        # 1) 崩溃在批次执行中：running 批次回到 planned（检查点未提交）。
        for batch in self.ledger.list_batches(run_id)["items"]:
            if batch["status"] == "running":
                self.ledger.finish_batch(
                    run_id,
                    int(batch["batch_no"]),
                    status="planned",
                    checkpoint={
                        **batch["checkpoint"],
                        "recovered": "崩溃时正在执行，已回到待执行",
                    },
                )
                report["reset_batches"].append(batch["batch_no"])
                # 处于 switching 的对象复位为 ready，等待重新切换。
                for item in self.ledger.list_objects(
                    run_id,
                    status="switching",
                    batch_no=int(batch["batch_no"]),
                    limit=10_000,
                )["items"]:
                    self.ledger.upsert_object(
                        run_id,
                        kind=item["kind"],
                        object_id=item["id"],
                        status="ready",
                        last_error="启动恢复：批次中断，待重新切换",
                    )

        # 2) 全量对账：台账中的对象与真实实体双读指纹必须一致。
        state = self.repository.read()
        reference_map = self.ledger.get_reference_map(run_id)
        for container, kind in ENTITY_KINDS.items():
            for object_id, payload in state[container].items():
                tracked = self.ledger.get_object(run_id, kind, object_id)
                result = self.engine.read_object(
                    kind,
                    payload,
                    reference_map=reference_map,
                )
                report["reconciled_objects"] += 1
                if tracked is None:
                    # 规划后出现的对象（可能由崩溃前的业务写入创建）。
                    status = "ready" if result.business_equal else "incompatible"
                    self.ledger.upsert_object(
                        run_id,
                        kind=kind,
                        object_id=object_id,
                        status=status,
                        legacy_fingerprint=result.legacy_fingerprint,
                        new_fingerprint=result.new_fingerprint,
                        legacy_payload=payload,
                        new_payload=result.upgraded_payload,
                        last_error=(
                            None
                            if result.business_equal
                            else "启动恢复：新对象双读不一致"
                        ),
                    )
                    continue
                if tracked["status"] in {"switched", "retained"}:
                    # 已切换对象：以新规则指纹与当前实体核对。
                    if (
                        tracked["new_fingerprint"]
                        and tracked["new_fingerprint"] != result.new_fingerprint
                    ):
                        report["drifts"].append(
                            {
                                "kind": kind,
                                "id": object_id,
                                "reason": "已切换对象的新规则指纹发生漂移",
                            }
                        )
                elif tracked["status"] in {"ready", "pending", "failed"}:
                    if (
                        tracked["legacy_fingerprint"]
                        and tracked["legacy_fingerprint"] != result.legacy_fingerprint
                    ):
                        # 对象在迁移期间被更正：刷新双读影子。
                        status = "ready" if result.business_equal else "incompatible"
                        self.ledger.upsert_object(
                            run_id,
                            kind=kind,
                            object_id=object_id,
                            status=status,
                            legacy_fingerprint=result.legacy_fingerprint,
                            new_fingerprint=result.new_fingerprint,
                            legacy_payload=payload,
                            new_payload=result.upgraded_payload,
                            last_error=(
                                None
                                if result.business_equal
                                else "启动恢复：更正后双读不一致"
                            ),
                        )

        # 3) 漂移阻断继续切换；running 状态收敛为安全的 paused。
        new_status = run["status"]
        if report["drifts"]:
            new_status = "blocked"
        elif run["status"] == "running":
            new_status = "paused"
        if new_status != run["status"]:
            self.ledger.update_run(run_id, status=new_status)
        self.ledger.record_event(
            run_id,
            event_type="migration.recovered",
            actor_id="startup-recovery",
            payload=report,
        )
        report["new_status"] = new_status
        LOGGER.info("迁移恢复完成：%s", report)
        return report
