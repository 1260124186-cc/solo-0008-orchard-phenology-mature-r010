"""迁移台账：进度、失败项、批次检查点、不兼容集合与引用映射。

所有迁移决定都持久化，因此：
- 每个对象的双读指纹、尝试次数、失败原因、保留原因可查询；
- 每批有独立检查点，单批失败只标记该批，不触碰已成功批次；
- 引用映射（规范树）随台账保存，重放与启动恢复时口径一致；
- 迁移期间的更正、合并、撤销事件与常规业务审计分流记录。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from ..errors import NotFoundError, PreconditionError
from .dual_read import CONTAINER_FOR_KIND


RUN_STATUSES = {
    "planning",
    "shadow",
    "running",
    "paused",
    "blocked",
    "completed",
    "rolling_back",
    "rolled_back",
}
OBJECT_STATUSES = {
    "pending",
    "incompatible",
    "ready",
    "switching",
    "switched",
    "failed",
    "retained",
}
BATCH_STATUSES = {"planned", "running", "succeeded", "failed", "reverted"}


class MigrationLedger:
    def __init__(self, database: Any) -> None:
        self.database = database

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def create_run(
        self,
        *,
        change_set_id: str,
        from_version: int,
        to_version: int,
        batch_size: int,
        created_by: str,
    ) -> dict[str, Any]:
        run_id = f"mig_{uuid.uuid4().hex[:16]}"
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO migration_runs
                (run_id, change_set, from_version, to_version, batch_size,
                 status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'planning', ?, ?, ?)
                """,
                (
                    run_id,
                    change_set_id,
                    from_version,
                    to_version,
                    batch_size,
                    created_by,
                    now,
                    now,
                ),
            )
            _set_meta(connection, f"migration.{run_id}.reference_map", "{}")
            _set_meta(connection, f"migration.{run_id}.retained", "[]")
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            row = _fetch_run(connection, run_id)
        if row is None:
            raise NotFoundError("迁移活动", run_id)
        return _run_view(row)

    def list_runs(self, *, limit: int = 100) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM migration_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        items = [_run_view(row) for row in rows]
        return {"items": items, "total": len(items)}

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_run(run_id)
        allowed = {
            "status",
            "total_objects",
            "switched_objects",
            "incompatible_objects",
            "retained_objects",
            "cursor",
            "stop_requested",
            "rollback_requested",
            "completed_at",
            "last_error",
        }
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"非法的迁移运行字段：{key}")
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_now())
        values.append(run_id)
        with self.database.transaction(immediate=True) as connection:
            row = _fetch_run(connection, run_id)
            if row is None:
                raise NotFoundError("迁移活动", run_id)
            connection.execute(
                f"UPDATE migration_runs SET {', '.join(assignments)} WHERE run_id = ?",
                values,
            )
        return self.get_run(run_id)

    # ------------------------------------------------------------------
    # 对象
    # ------------------------------------------------------------------
    def upsert_object(
        self,
        run_id: str,
        *,
        kind: str,
        object_id: str,
        status: str,
        legacy_fingerprint: str | None = None,
        new_fingerprint: str | None = None,
        legacy_payload: dict[str, Any] | None = None,
        new_payload: dict[str, Any] | None = None,
        batch_no: int | None = None,
        last_error: str | None = None,
        retained_reason: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        if status not in OBJECT_STATUSES:
            raise ValueError(f"非法对象状态：{status}")
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT status, attempts FROM migration_objects
                WHERE run_id = ? AND kind = ? AND object_id = ?
                """,
                (run_id, kind, object_id),
            ).fetchone()
            switched_at = now if status == "switched" else None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO migration_objects
                    (run_id, kind, object_id, batch_no, status,
                     legacy_fingerprint, new_fingerprint,
                     legacy_payload, new_payload,
                     attempts, last_error, switched_at, retained_reason, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        kind,
                        object_id,
                        batch_no,
                        status,
                        legacy_fingerprint,
                        new_fingerprint,
                        _json(legacy_payload) if legacy_payload is not None else None,
                        _json(new_payload) if new_payload is not None else None,
                        1 if increment_attempts else 0,
                        last_error,
                        switched_at,
                        retained_reason,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE migration_objects
                    SET status = ?,
                        batch_no = COALESCE(?, batch_no),
                        legacy_fingerprint = COALESCE(?, legacy_fingerprint),
                        new_fingerprint = COALESCE(?, new_fingerprint),
                        legacy_payload = COALESCE(?, legacy_payload),
                        new_payload = COALESCE(?, new_payload),
                        attempts = attempts + ?,
                        last_error = ?,
                        switched_at = COALESCE(?, switched_at),
                        retained_reason = COALESCE(?, retained_reason),
                        updated_at = ?
                    WHERE run_id = ? AND kind = ? AND object_id = ?
                    """,
                    (
                        status,
                        batch_no,
                        legacy_fingerprint,
                        new_fingerprint,
                        _json(legacy_payload) if legacy_payload is not None else None,
                        _json(new_payload) if new_payload is not None else None,
                        1 if increment_attempts else 0,
                        last_error,
                        switched_at,
                        retained_reason,
                        now,
                        run_id,
                        kind,
                        object_id,
                    ),
                )

    def list_objects(
        self,
        run_id: str,
        *,
        status: str | None = None,
        kind: str | None = None,
        batch_no: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if batch_no is not None:
            clauses.append("batch_no = ?")
            params.append(batch_no)
        params.append(max(1, min(limit, 5000)))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, kind, object_id, batch_no, status,
                       legacy_fingerprint, new_fingerprint,
                       legacy_payload, new_payload,
                       attempts, last_error, switched_at, retained_reason, updated_at
                FROM migration_objects
                WHERE {' AND '.join(clauses)}
                ORDER BY kind, object_id
                LIMIT ?
                """,
                params,
            ).fetchall()
        items = [_object_view(row) for row in rows]
        return {"items": items, "total": len(items)}

    def get_object(
        self,
        run_id: str,
        kind: str,
        object_id: str,
    ) -> dict[str, Any] | None:
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM migration_objects WHERE run_id = ? AND kind = ? AND object_id = ?",
                (run_id, kind, object_id),
            ).fetchone()
        return _object_view(row) if row is not None else None

    def counts_by_status(self, run_id: str) -> dict[str, int]:
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM migration_objects
                WHERE run_id = ?
                GROUP BY status
                """,
                (run_id,),
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------
    def plan_batch(
        self,
        run_id: str,
        batch_no: int,
        objects: list[tuple[str, str]],
    ) -> None:
        checkpoint = _json(
            {
                "planned_objects": [
                    {"kind": kind, "id": object_id}
                    for kind, object_id in objects
                ],
            }
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO migration_batches
                (run_id, batch_no, status, object_count, checkpoint, started_at)
                VALUES (?, ?, 'planned', ?, ?, NULL)
                ON CONFLICT(run_id, batch_no) DO NOTHING
                """,
                (run_id, batch_no, len(objects), checkpoint),
            )

    def start_batch(self, run_id: str, batch_no: int) -> None:
        with self.database.transaction(immediate=True) as connection:
            _require_batch(connection, run_id, batch_no)
            connection.execute(
                """
                UPDATE migration_batches
                SET status = 'running', started_at = COALESCE(started_at, ?)
                WHERE run_id = ? AND batch_no = ?
                """,
                (_now(), run_id, batch_no),
            )

    def finish_batch(
        self,
        run_id: str,
        batch_no: int,
        *,
        status: str,
        checkpoint: dict[str, Any],
        error: str | None = None,
    ) -> None:
        # planned 也允许写入：重试与启动恢复会把中断/失败批次重置为待执行。
        if status not in {"planned", "succeeded", "failed", "reverted"}:
            raise ValueError(f"非法批次终态：{status}")
        with self.database.transaction(immediate=True) as connection:
            _require_batch(connection, run_id, batch_no)
            if status == "planned":
                connection.execute(
                    """
                    UPDATE migration_batches
                    SET status = 'planned', checkpoint = ?,
                        started_at = NULL, finished_at = NULL, last_error = ?
                    WHERE run_id = ? AND batch_no = ?
                    """,
                    (_json(checkpoint), error, run_id, batch_no),
                )
            else:
                connection.execute(
                    """
                    UPDATE migration_batches
                    SET status = ?, checkpoint = ?, finished_at = ?, last_error = ?
                    WHERE run_id = ? AND batch_no = ?
                    """,
                    (status, _json(checkpoint), _now(), error, run_id, batch_no),
                )

    def list_batches(self, run_id: str) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, batch_no, status, object_count, checkpoint,
                       started_at, finished_at, last_error
                FROM migration_batches
                WHERE run_id = ?
                ORDER BY batch_no
                """,
                (run_id,),
            ).fetchall()
        items = [_batch_view(row) for row in rows]
        return {"items": items, "total": len(items)}

    # ------------------------------------------------------------------
    # 迁移事件（更正、合并、撤销、切换、回滚）
    # ------------------------------------------------------------------
    def record_event(
        self,
        run_id: str,
        *,
        event_type: str,
        actor_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self.database.transaction(immediate=True) as connection:
            cur = connection.execute(
                """
                INSERT INTO migration_events
                (run_id, event_type, actor_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, event_type, actor_id, _json(payload), now),
            )
            event_id = int(cur.lastrowid)
        return {
            "id": event_id,
            "run_id": run_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": now,
        }

    def list_events(
        self,
        run_id: str,
        *,
        event_type: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(max(1, min(limit, 2000)))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, run_id, event_type, actor_id, payload, created_at
                FROM migration_events
                WHERE {' AND '.join(clauses)}
                ORDER BY id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return {
            "items": [
                {
                    "id": row["id"],
                    "run_id": row["run_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    # ------------------------------------------------------------------
    # 引用映射与保留集合（meta 存储）
    # ------------------------------------------------------------------
    def get_reference_map(self, run_id: str) -> dict[str, str]:
        with self.database.read_connection() as connection:
            raw = _get_meta(connection, f"migration.{run_id}.reference_map", "{}")
        return {str(key): str(value) for key, value in json.loads(raw).items()}

    def update_reference_map(
        self,
        run_id: str,
        additions: dict[str, str],
    ) -> dict[str, str]:
        with self.database.transaction(immediate=True) as connection:
            raw = _get_meta(connection, f"migration.{run_id}.reference_map", "{}")
            mapping = json.loads(raw)
            mapping.update(
                {str(key): str(value) for key, value in additions.items()}
            )
            _set_meta(connection, f"migration.{run_id}.reference_map", _json(mapping))
        return {str(key): str(value) for key, value in mapping.items()}

    def list_retained(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.read_connection() as connection:
            raw = _get_meta(connection, f"migration.{run_id}.retained", "[]")
        return json.loads(raw)

    def add_retained(self, run_id: str, entries: list[dict[str, Any]]) -> None:
        with self.database.transaction(immediate=True) as connection:
            raw = _get_meta(connection, f"migration.{run_id}.retained", "[]")
            retained = json.loads(raw)
            existing = {(item["kind"], item["id"]) for item in retained}
            for entry in entries:
                key = (entry["kind"], entry["id"])
                if key in existing:
                    continue
                retained.append(entry)
                existing.add(key)
            _set_meta(connection, f"migration.{run_id}.retained", _json(retained))

    def active_run(self) -> dict[str, Any] | None:
        """返回当前未终结的迁移活动，供启动恢复使用。"""

        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM migration_runs
                WHERE status NOT IN ('completed', 'rolled_back')
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return _run_view(row) if row is not None else None


def _fetch_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM migration_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def _require_batch(
    connection: sqlite3.Connection,
    run_id: str,
    batch_no: int,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM migration_batches WHERE run_id = ? AND batch_no = ?",
        (run_id, batch_no),
    ).fetchone()
    if row is None:
        raise NotFoundError("迁移批次", f"{run_id}#{batch_no}")
    return row


def _run_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "change_set": row["change_set"],
        "from_version": row["from_version"],
        "to_version": row["to_version"],
        "batch_size": row["batch_size"],
        "status": row["status"],
        "total_objects": row["total_objects"],
        "switched_objects": row["switched_objects"],
        "incompatible_objects": row["incompatible_objects"],
        "retained_objects": row["retained_objects"],
        "cursor": row["cursor"],
        "stop_requested": bool(row["stop_requested"]),
        "rollback_requested": bool(row["rollback_requested"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "last_error": row["last_error"],
    }


def _object_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "kind": row["kind"],
        "id": row["object_id"],
        "batch_no": row["batch_no"],
        "status": row["status"],
        "legacy_fingerprint": row["legacy_fingerprint"],
        "new_fingerprint": row["new_fingerprint"],
        "legacy_payload": json.loads(row["legacy_payload"]) if row["legacy_payload"] else None,
        "new_payload": json.loads(row["new_payload"]) if row["new_payload"] else None,
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "switched_at": row["switched_at"],
        "retained_reason": row["retained_reason"],
        "updated_at": row["updated_at"],
    }


def _batch_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "batch_no": row["batch_no"],
        "status": row["status"],
        "object_count": row["object_count"],
        "checkpoint": json.loads(row["checkpoint"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_error": row["last_error"],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _get_meta(
    connection: sqlite3.Connection,
    key: str,
    default: str | None = None,
) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def _set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
