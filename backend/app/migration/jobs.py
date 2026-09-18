"""迁移专用后台任务处理器。

注册到 worker 后，迁移批次以普通任务形式被租约调度，复用现有
尝试次数、延迟重试和死信机制，因此迁移任务与其他后台任务一样
可恢复、可重试、可取消，且单批失败不影响其他批次。
"""

from __future__ import annotations

from typing import Any

from .service import MigrationService


JOB_TYPE_RUN_BATCH = "migration_run_batch"
JOB_TYPE_RECOVER = "migration_recover"


def register_migration_handlers(worker: Any, repository: Any) -> None:
    service = MigrationService(repository)

    def run_batch(payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or "")
        status = service.run_next_batch(actor_id=payload.get("actor_id", "migration-job"))
        return {
            "run_id": run_id,
            "run_status": status["run"]["status"],
            "switched": status["run"]["switched_objects"],
            "object_counts": status["object_counts"],
        }

    def recover(payload: dict[str, Any]) -> dict[str, Any]:
        from .recovery import MigrationRecovery

        return MigrationRecovery(repository).recover() or {"status": "no_active_run"}

    worker.handlers[JOB_TYPE_RUN_BATCH] = run_batch
    worker.handlers[JOB_TYPE_RECOVER] = recover
