#!/usr/bin/env python3
"""可在生产式负载下安全推进的领域迁移命令行。

用法示例：

    # 建立迁移计划（按 50 个对象一批）
    python3 scripts/migrate_domain.py --data-dir backend/var plan --batch-size 50 \
        --name "阶段字典与置信度精度 v2"

    # 查看计划与批次进度
    python3 scripts/migrate_domain.py --data-dir backend/var status PLAN_ID
    python3 scripts/migrate_domain.py --data-dir backend/var report PLAN_ID

    # 双读验证并切换下一批（或指定批次）
    python3 scripts/migrate_domain.py --data-dir backend/var verify PLAN_ID [SEQ]
    python3 scripts/migrate_domain.py --data-dir backend/var apply PLAN_ID [SEQ]

    # 一次性推进所有批次（每批都先双读、失败即停）
    python3 scripts/migrate_domain.py --data-dir backend/var advance PLAN_ID

    # 暂停 / 恢复 / 回滚尚未切换的批次
    python3 scripts/migrate_domain.py --data-dir backend/var pause PLAN_ID
    python3 scripts/migrate_domain.py --data-dir backend/var resume PLAN_ID
    python3 scripts/migrate_domain.py --data-dir backend/var rollback PLAN_ID \
        --reason "新规则需要调整"

    # 正式启用第 2 代规则（要求全部批次通过且无遗留不兼容对象）
    python3 scripts/migrate_domain.py --data-dir backend/var finalize PLAN_ID
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.migration import MigrationService  # noqa: E402
from app.persistence import Database, Repository  # noqa: E402


ACTOR = "migration-operator"


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _service(data_dir: Path) -> tuple[Repository, MigrationService]:
    repository = Repository(
        Database(data_dir / "atlas.sqlite3"),
        legacy_state_path=data_dir / "state.json",
    )
    repository.open()
    return repository, MigrationService(repository)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="领域规则分批迁移")
    parser.add_argument("--data-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan", help="建立迁移计划")
    plan_cmd.add_argument("--name", required=True)
    plan_cmd.add_argument("--batch-size", type=int, default=50)
    plan_cmd.add_argument("--rationale", default="")

    status_cmd = sub.add_parser("status", help="查看计划与批次")
    status_cmd.add_argument("plan_id")

    report_cmd = sub.add_parser("report", help="四切面核对报告")
    report_cmd.add_argument("plan_id")

    verify_cmd = sub.add_parser("verify", help="双读验证批次")
    verify_cmd.add_argument("plan_id")
    verify_cmd.add_argument("seq", type=int, nargs="?", default=-1)

    apply_cmd = sub.add_parser("apply", help="切换批次")
    apply_cmd.add_argument("plan_id")
    apply_cmd.add_argument("seq", type=int, nargs="?", default=-1)

    advance_cmd = sub.add_parser("advance", help="逐批双读并切换全部批次")
    advance_cmd.add_argument("plan_id")

    for name in ("pause", "resume", "finalize"):
        command = sub.add_parser(name)
        command.add_argument("plan_id")

    rollback_cmd = sub.add_parser("rollback")
    rollback_cmd.add_argument("plan_id")
    rollback_cmd.add_argument("--reason", default="")

    plans_cmd = sub.add_parser("plans", help="列出全部计划")
    plans_cmd.set_defaults(no_plan=True)

    args = parser.parse_args(argv)
    repository, migration = _service(args.data_dir.resolve())
    try:
        if args.command == "plan":
            _emit(
                migration.create_plan(
                    name=args.name,
                    actor_id=ACTOR,
                    batch_size=args.batch_size,
                    rationale=args.rationale,
                )
            )
        elif args.command == "plans":
            _emit(migration.list_plans())
        elif args.command == "status":
            _emit(migration.get_plan(args.plan_id))
        elif args.command == "report":
            _emit(migration.verify_report(args.plan_id))
        elif args.command in {"verify", "apply"}:
            seq = args.seq
            if seq < 0:
                plan = migration.get_plan(args.plan_id)
                pending = next(
                    (
                        batch
                        for batch in plan["batches"]
                        if batch["status"] in {"pending", "failed"}
                    ),
                    None,
                )
                if pending is None:
                    raise SystemExit("没有待处理批次")
                seq = pending["seq"]
            if args.command == "verify":
                _emit(
                    migration.verify_batch(
                        args.plan_id, seq, actor_id=ACTOR
                    )
                )
            else:
                _emit(
                    migration.apply_batch(
                        args.plan_id, seq, actor_id=ACTOR
                    )
                )
        elif args.command == "advance":
            while True:
                plan = migration.get_plan(args.plan_id)
                target = next(
                    (
                        batch
                        for batch in plan["batches"]
                        if batch["status"] in {"pending", "failed"}
                    ),
                    None,
                )
                if target is None:
                    break
                verified = migration.verify_batch(
                    args.plan_id, target["seq"], actor_id=ACTOR
                )
                if verified["status"] != "verified":
                    _emit(
                        {
                            "stopped_at": target["seq"],
                            "batch": verified,
                            "incompatible": migration.list_incompatible(
                                args.plan_id, batch_seq=target["seq"]
                            ),
                        }
                    )
                    return 2
                _emit(
                    migration.apply_batch(
                        args.plan_id, target["seq"], actor_id=ACTOR
                    )
                )
            _emit(migration.verify_report(args.plan_id))
        elif args.command == "pause":
            _emit(migration.pause(args.plan_id, actor_id=ACTOR))
        elif args.command == "resume":
            _emit(migration.resume(args.plan_id, actor_id=ACTOR))
        elif args.command == "rollback":
            _emit(
                migration.rollback_plan(
                    args.plan_id, actor_id=ACTOR, reason=args.reason
                )
            )
        elif args.command == "finalize":
            _emit(migration.finalize_plan(args.plan_id, actor_id=ACTOR))
    finally:
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
