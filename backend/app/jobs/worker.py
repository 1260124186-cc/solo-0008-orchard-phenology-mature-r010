"""本地任务 worker 与内置任务处理器。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ..persistence import Repository
from .service import JobService


LOGGER = logging.getLogger("orchard-phenology-worker")


class JobWorker:
    def __init__(
        self,
        *,
        jobs: JobService,
        repository: Repository,
        worker_id: str,
        migration: Any | None = None,
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        self.jobs = jobs
        self.repository = repository
        self.worker_id = worker_id
        self.migration = migration
        self.handlers = {
            "integrity_scan": self._integrity_scan,
            "migration.verify_batch": self._migration_verify_batch,
            "migration.apply_batch": self._migration_apply_batch,
            **(handlers or {}),
        }

    def run_once(self) -> bool:
        job = self.jobs.claim(worker_id=self.worker_id, lease_seconds=60)
        if job is None:
            return False
        handler = self.handlers.get(job["job_type"])
        if handler is None:
            self.jobs.fail(
                job["id"],
                worker_id=self.worker_id,
                error=f"未注册的任务类型：{job['job_type']}",
            )
            return True
        try:
            result = handler(job["payload"])
        except Exception as exc:
            LOGGER.exception("任务 %s 执行失败", job["id"])
            self.jobs.fail(
                job["id"],
                worker_id=self.worker_id,
                error=str(exc),
            )
        else:
            self.jobs.complete(
                job["id"],
                worker_id=self.worker_id,
                result=result,
            )
        return True

    def run_forever(self, *, idle_seconds: float = 1.0) -> None:
        while True:
            worked = self.run_once()
            if not worked:
                time.sleep(max(0.1, idle_seconds))

    def _integrity_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        state = self.repository.read()
        from ..persistence.snapshot import check_relationships

        problems = check_relationships(state)
        return {
            "status": "healthy" if not problems else "issues_found",
            "problem_count": len(problems),
            "problems": problems[:200],
            "state_revision": state["revision"],
        }

    def _require_migration(self) -> Any:
        if self.migration is None:
            from ..migration import MigrationService

            self.migration = MigrationService(self.repository)
        return self.migration

    def _migration_verify_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        migration = self._require_migration()
        actor = str(payload.get("actor_id") or "migration-worker")
        batch = migration.verify_batch(
            str(payload["plan_id"]),
            int(payload["seq"]),
            actor_id=actor,
        )
        return {"batch": batch["seq"], "status": batch["status"]}

    def _migration_apply_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        migration = self._require_migration()
        actor = str(payload.get("actor_id") or "migration-worker")
        batch = migration.apply_batch(
            str(payload["plan_id"]),
            int(payload["seq"]),
            actor_id=actor,
        )
        return {
            "batch": batch["seq"],
            "status": batch["status"],
            "applied": batch["applied_objects"],
        }
