#!/usr/bin/env python3
"""从仓库根目录启动本地后台任务 worker。"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.jobs import JobService, JobWorker  # noqa: E402
from app.migration import MigrationService  # noqa: E402
from app.persistence import Database, Repository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="果园物候图谱后台任务 worker")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    data_dir = args.data_dir.resolve()
    repository = Repository(
        Database(data_dir / "atlas.sqlite3"),
        legacy_state_path=data_dir / "state.json",
    )
    repository.open()
    migration = MigrationService(repository)
    worker = JobWorker(
        jobs=JobService(repository.database),
        repository=repository,
        worker_id=args.worker_id,
        migration=migration,
    )
    if args.once:
        return 0 if worker.run_once() else 1
    worker.run_forever(idle_seconds=args.idle_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
