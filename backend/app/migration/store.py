"""迁移台账的低层读写。所有方法都在调用方事务/连接内执行。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fetch_plan(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM migration_plans WHERE id = ?",
        (plan_id,),
    ).fetchone()
    return _plan_view(row) if row is not None else None


def active_plan(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM migration_plans
        WHERE status IN ('active', 'paused')
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()
    return _plan_view(row) if row is not None else None


def finalized_generation(connection: sqlite3.Connection) -> tuple[str | None, int]:
    row = connection.execute(
        """
        SELECT id, to_generation FROM migration_plans
        WHERE status = 'finalized'
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None, 1
    return row["id"], int(row["to_generation"])


def insert_plan(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    name: str,
    from_generation: int,
    to_generation: int,
    batch_size: int,
    rationale: str,
    created_by: str,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO migration_plans
        (id, name, from_generation, to_generation, status, batch_size,
         total_objects, applied_objects, incompatible_objects, rationale,
         created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', ?, 0, 0, 0, ?, ?, ?, ?)
        """,
        (
            plan_id,
            name,
            from_generation,
            to_generation,
            batch_size,
            rationale,
            created_by,
            timestamp,
            timestamp,
        ),
    )


def update_plan_status(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    status: str,
    timestamp: str,
) -> None:
    finalized = ", finalized_at = ?" if status == "finalized" else ""
    rolled_back = ", rolled_back_at = ?" if status == "rolled_back" else ""
    params: list[Any] = [status, timestamp]
    if status == "finalized":
        params.append(timestamp)
    if status == "rolled_back":
        params.append(timestamp)
    params.append(plan_id)
    connection.execute(
        f"""
        UPDATE migration_plans
        SET status = ?, updated_at = ?{finalized}{rolled_back}
        WHERE id = ?
        """,
        params,
    )


def recompute_plan_counts(
    connection: sqlite3.Connection,
    plan_id: str,
) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM migration_objects
        WHERE plan_id = ?
        GROUP BY status
        """,
        (plan_id,),
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in rows}
    total = sum(counts.values())
    applied = counts.get("applied", 0)
    incompatible = counts.get("incompatible", 0)
    connection.execute(
        """
        UPDATE migration_plans
        SET total_objects = ?, applied_objects = ?, incompatible_objects = ?,
            updated_at = updated_at
        WHERE id = ?
        """,
        (total, applied, incompatible, plan_id),
    )
    return {"total": total, "applied": applied, "incompatible": incompatible}


def insert_batch(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    seq: int,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO migration_batches
        (plan_id, seq, status, expected_objects, applied_objects,
         incompatible_count, created_at, updated_at)
        VALUES (?, ?, 'pending', 0, 0, 0, ?, ?)
        """,
        (plan_id, seq, timestamp, timestamp),
    )


def fetch_batch(
    connection: sqlite3.Connection,
    plan_id: str,
    seq: int,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM migration_batches WHERE plan_id = ? AND seq = ?",
        (plan_id, seq),
    ).fetchone()
    return _batch_view(row) if row is not None else None


def next_pending_batch(
    connection: sqlite3.Connection,
    plan_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM migration_batches
        WHERE plan_id = ? AND status IN ('pending', 'verified', 'failed')
        ORDER BY seq
        LIMIT 1
        """,
        (plan_id,),
    ).fetchone()
    return _batch_view(row) if row is not None else None


def update_batch(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    seq: int,
    status: str,
    timestamp: str,
    expected: int | None = None,
    applied: int | None = None,
    incompatible: int | None = None,
    checkpoint: str | None = None,
    report: str | None = None,
    error: str | None = None,
    applied_at: str | None = None,
) -> None:
    assignments = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, timestamp]
    if expected is not None:
        assignments.append("expected_objects = ?")
        params.append(expected)
    if applied is not None:
        assignments.append("applied_objects = ?")
        params.append(applied)
    if incompatible is not None:
        assignments.append("incompatible_count = ?")
        params.append(incompatible)
    if checkpoint is not None:
        assignments.append("checkpoint = ?")
        params.append(checkpoint)
    if report is not None:
        assignments.append("report = ?")
        params.append(report)
    if error is not None:
        assignments.append("error = ?")
        params.append(error)
    if applied_at is not None:
        assignments.append("applied_at = ?")
        params.append(applied_at)
    params.extend([plan_id, seq])
    connection.execute(
        f"""
        UPDATE migration_batches
        SET {', '.join(assignments)}
        WHERE plan_id = ? AND seq = ?
        """,
        params,
    )


def list_batches(
    connection: sqlite3.Connection,
    plan_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM migration_batches WHERE plan_id = ? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    return [_batch_view(row) for row in rows]


def upsert_object(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    kind: str,
    object_id: str,
    status: str,
    timestamp: str,
    batch_seq: int | None = None,
    source_revision: int | None = None,
    legacy_payload: str | None = None,
    projected_payload: str | None = None,
    v1_fingerprint: str | None = None,
    v2_fingerprint: str | None = None,
    reason: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO migration_objects
        (plan_id, kind, object_id, batch_seq, status, source_revision,
         legacy_payload, projected_payload, v1_fingerprint, v2_fingerprint,
         reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_id, kind, object_id) DO UPDATE SET
            batch_seq = COALESCE(excluded.batch_seq, migration_objects.batch_seq),
            status = excluded.status,
            source_revision = COALESCE(excluded.source_revision,
                                       migration_objects.source_revision),
            legacy_payload = COALESCE(excluded.legacy_payload,
                                      migration_objects.legacy_payload),
            projected_payload = COALESCE(excluded.projected_payload,
                                         migration_objects.projected_payload),
            v1_fingerprint = COALESCE(excluded.v1_fingerprint,
                                      migration_objects.v1_fingerprint),
            v2_fingerprint = COALESCE(excluded.v2_fingerprint,
                                      migration_objects.v2_fingerprint),
            reason = COALESCE(excluded.reason, migration_objects.reason),
            updated_at = excluded.updated_at
        """,
        (
            plan_id,
            kind,
            object_id,
            batch_seq,
            status,
            source_revision,
            legacy_payload,
            projected_payload,
            v1_fingerprint,
            v2_fingerprint,
            reason,
            timestamp,
        ),
    )


def fetch_object(
    connection: sqlite3.Connection,
    plan_id: str,
    kind: str,
    object_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM migration_objects
        WHERE plan_id = ? AND kind = ? AND object_id = ?
        """,
        (plan_id, kind, object_id),
    ).fetchone()
    return _object_view(row) if row is not None else None


def list_objects(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    batch_seq: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["plan_id = ?"]
    params: list[Any] = [plan_id]
    if batch_seq is not None:
        clauses.append("batch_seq = ?")
        params.append(batch_seq)
    if status:
        clauses.append("status = ?")
        params.append(status)
    rows = connection.execute(
        f"""
        SELECT * FROM migration_objects
        WHERE {' AND '.join(clauses)}
        ORDER BY kind, object_id
        """,
        params,
    ).fetchall()
    return [_object_view(row) for row in rows]


def count_objects_by_status(
    connection: sqlite3.Connection,
    plan_id: str,
) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM migration_objects
        WHERE plan_id = ?
        GROUP BY status
        """,
        (plan_id,),
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def insert_op(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    op_type: str,
    target_kind: str,
    target_id: str,
    payload: dict[str, Any],
    created_by: str,
    timestamp: str,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO migration_ops
        (plan_id, op_type, target_kind, target_id, payload, status,
         created_by, created_at)
        VALUES (?, ?, ?, ?, ?, 'requested', ?, ?)
        """,
        (
            plan_id,
            op_type,
            target_kind,
            target_id,
            _json(payload),
            created_by,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def mark_op(
    connection: sqlite3.Connection,
    op_id: int,
    *,
    status: str,
    batch_seq: int | None = None,
) -> None:
    connection.execute(
        """
        UPDATE migration_ops
        SET status = ?, applied_batch_seq = COALESCE(?, applied_batch_seq)
        WHERE id = ?
        """,
        (status, batch_seq, op_id),
    )


def list_ops(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    op_type: str | None = None,
) -> list[dict[str, Any]]:
    if op_type:
        rows = connection.execute(
            """
            SELECT * FROM migration_ops
            WHERE plan_id = ? AND op_type = ?
            ORDER BY id
            """,
            (plan_id, op_type),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM migration_ops WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
    return [_op_view(row) for row in rows]


def upsert_projection(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    kind: str,
    object_id: str,
    generation: int,
    payload: dict[str, Any],
    fingerprint: str,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO entity_projections
        (plan_id, kind, object_id, generation, payload, fingerprint, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_id, kind, object_id) DO UPDATE SET
            generation = excluded.generation,
            payload = excluded.payload,
            fingerprint = excluded.fingerprint,
            updated_at = excluded.updated_at
        """,
        (
            plan_id,
            kind,
            object_id,
            generation,
            _json(payload),
            fingerprint,
            timestamp,
        ),
    )


def get_projection(
    connection: sqlite3.Connection,
    plan_id: str,
    kind: str,
    object_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM entity_projections
        WHERE plan_id = ? AND kind = ? AND object_id = ?
        """,
        (plan_id, kind, object_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": row["kind"],
        "object_id": row["object_id"],
        "generation": int(row["generation"]),
        "payload": json.loads(row["payload"]),
        "fingerprint": row["fingerprint"],
    }


def _plan_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "from_generation": int(row["from_generation"]),
        "to_generation": int(row["to_generation"]),
        "status": row["status"],
        "batch_size": int(row["batch_size"]),
        "total_objects": int(row["total_objects"]),
        "applied_objects": int(row["applied_objects"]),
        "incompatible_objects": int(row["incompatible_objects"]),
        "rationale": row["rationale"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finalized_at": row["finalized_at"],
        "rolled_back_at": row["rolled_back_at"],
    }


def _batch_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "plan_id": row["plan_id"],
        "seq": int(row["seq"]),
        "status": row["status"],
        "expected_objects": int(row["expected_objects"]),
        "applied_objects": int(row["applied_objects"]),
        "incompatible_count": int(row["incompatible_count"]),
        "checkpoint": json.loads(row["checkpoint"]) if row["checkpoint"] else None,
        "report": json.loads(row["report"]) if row["report"] else None,
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "applied_at": row["applied_at"],
    }


def _object_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "plan_id": row["plan_id"],
        "kind": row["kind"],
        "object_id": row["object_id"],
        "batch_seq": row["batch_seq"],
        "status": row["status"],
        "source_revision": row["source_revision"],
        "legacy_payload": json.loads(row["legacy_payload"])
        if row["legacy_payload"]
        else None,
        "projected_payload": json.loads(row["projected_payload"])
        if row["projected_payload"]
        else None,
        "v1_fingerprint": row["v1_fingerprint"],
        "v2_fingerprint": row["v2_fingerprint"],
        "reason": row["reason"],
        "updated_at": row["updated_at"],
    }


def _op_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "plan_id": row["plan_id"],
        "op_type": row["op_type"],
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "payload": json.loads(row["payload"]),
        "status": row["status"],
        "applied_batch_seq": row["applied_batch_seq"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
