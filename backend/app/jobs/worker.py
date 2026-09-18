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
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        self.jobs = jobs
        self.repository = repository
        self.worker_id = worker_id
        self.handlers = {
            "integrity_scan": self._integrity_scan,
            **(handlers or {}),
        }
        # 迁移批次作为普通后台任务注册，复用租约、重试与死信。
        from ..migration.jobs import register_migration_handlers

        register_migration_handlers(self, repository)

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
