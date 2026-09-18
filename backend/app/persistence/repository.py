"""基于 SQLite 事务、对象修订和审计事件的仓储。"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from ..errors import ConflictError, DomainError
from ..security.context import current_request_context
from .database import Database
from .snapshot import check_relationships, empty_state, ensure_state_shape


T = TypeVar("T")

ENTITY_KINDS: dict[str, str] = {
    "plots": "plot",
    "trees": "tree",
    "observations": "observation",
    "comparisons": "comparison",
    "briefs": "brief",
}


class Repository:
    """保存实体行、版本快照、审计事件和事务 outbox。"""

    def __init__(
        self,
        database: Database,
        *,
        legacy_state_path: Path | None = None,
    ) -> None:
        self.database = database
        self.legacy_state_path = legacy_state_path
        # 由 MigrationService 在装配时注入；未启用迁移时为 None。
        self.migration_hook: Callable[..., None] | None = None

    def open(self) -> None:
        self.database.initialize()
        self._import_legacy_state()
        problems = check_relationships(self.read())
        if problems:
            raise DomainError(
                "state_relationships_invalid",
                "数据库关系检查失败",
                500,
                {"problems": problems[:10]},
            )

    def close(self) -> None:
        return

    def read(self) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            return _load_state(connection)

    def atomic_update(self, action: Callable[[dict[str, Any]], T]) -> T:
        context = current_request_context()
        with self.database.transaction(immediate=True) as connection:
            if context.idempotency_key:
                existing = _read_idempotent_result(connection, context)
                if existing is not None:
                    return copy.deepcopy(existing)

            baseline = _load_state(connection)
            working = copy.deepcopy(baseline)
            result = action(working)
            ensure_state_shape(working)
            problems = check_relationships(working)
            if problems:
                raise DomainError(
                    "state_relationships_invalid",
                    "事务会产生无效的对象关系",
                    500,
                    {"problems": problems[:10]},
                )

            changes = _changed_entities(baseline, working)
            if changes:
                next_revision = int(baseline["revision"]) + 1
                working["revision"] = next_revision
                # 双写钩子在同一事务内、业务落库前运行：
                # - 影子期：把每个变更投影到新世代并校验业务结论一致；
                # - 已切换期：就地把载荷升级为新世代，使本次写入按新规则落库。
                # 若新规则无法保持同一业务结论，这里抛错会让整笔业务写入连同
                # 实体、审计、outbox 一起回滚。
                if self.migration_hook is not None:
                    self.migration_hook(
                        connection,
                        changes=changes,
                        state=working,
                        next_revision=next_revision,
                    )
                _persist_changes(
                    connection,
                    changes,
                    actor_id=context.actor_id or "anonymous",
                    action=_action_name(context),
                    next_revision=next_revision,
                    context=context,
                )
                _set_meta(connection, "state_revision", str(next_revision))

            if context.idempotency_key:
                _store_idempotent_result(connection, context, result)
            return copy.deepcopy(result)

    def stats(self) -> dict[str, int]:
        with self.database.read_connection() as connection:
            counts = {
                "revision": int(_get_meta(connection, "state_revision", "0")),
                "plot_count": _count(connection, "plot"),
                "tree_count": _count(connection, "tree"),
                "observation_count": _count(connection, "observation"),
                "comparison_count": _count(connection, "comparison"),
                "brief_count": _count(connection, "brief"),
                "event_count": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM audit_events"
                    ).fetchone()["count"]
                ),
            }
        return counts

    def list_audit_events(
        self,
        *,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        actor_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if resource_kind:
            clauses.append("resource_kind = ?")
            params.append(resource_kind)
        if resource_id:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        if actor_id:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT sequence, event_id, actor_id, action, resource_kind,
                       resource_id, revision, details, created_at
                FROM audit_events
                {where}
                ORDER BY sequence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return {
            "items": [
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "resource_kind": row["resource_kind"],
                    "resource_id": row["resource_id"],
                    "revision": row["revision"],
                    "details": json.loads(row["details"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def list_entity_versions(
        self,
        *,
        kind: str,
        identifier: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT kind, id, revision, payload, actor_id, action, created_at
                FROM entity_versions
                WHERE kind = ? AND id = ?
                ORDER BY revision DESC
                LIMIT ?
                """,
                (kind, identifier, max(1, min(limit, 1000))),
            ).fetchall()
        return {
            "items": [
                {
                    "kind": row["kind"],
                    "id": row["id"],
                    "revision": row["revision"],
                    "payload": json.loads(row["payload"]),
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def list_outbox_events(
        self,
        *,
        status: str | None = None,
        topic: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if status and status not in {"pending", "published", "failed"}:
            raise DomainError(
                "invalid_outbox_status",
                "outbox 状态不合法",
                422,
                {"status": status},
            )
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, event_id, topic, payload, status, attempts,
                       created_at, published_at
                FROM outbox_events
                {where}
                ORDER BY id
                LIMIT ?
                """,
                params,
            ).fetchall()
        return {
            "items": [
                {
                    "id": row["id"],
                    "event_id": row["event_id"],
                    "topic": row["topic"],
                    "payload": json.loads(row["payload"]),
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "created_at": row["created_at"],
                    "published_at": row["published_at"],
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def publish_outbox_event(self, event_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT id, event_id, topic, payload, status, attempts,
                       created_at, published_at
                FROM outbox_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise DomainError(
                    "outbox_event_not_found",
                    "outbox 事件不存在",
                    404,
                    {"event_id": event_id},
                )
            if row["status"] == "published":
                return _outbox_view(row)
            timestamp = _now()
            connection.execute(
                """
                UPDATE outbox_events
                SET status = 'published', attempts = attempts + 1, published_at = ?
                WHERE event_id = ?
                """,
                (timestamp, event_id),
            )
            updated = connection.execute(
                """
                SELECT id, event_id, topic, payload, status, attempts,
                       created_at, published_at
                FROM outbox_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        return _outbox_view(updated)

    def _import_legacy_state(self) -> None:
        if self.legacy_state_path is None or not self.legacy_state_path.is_file():
            return
        with self.database.transaction(immediate=True) as connection:
            imported = _get_meta(connection, "legacy_state_imported")
            has_entities = connection.execute(
                "SELECT 1 FROM entities LIMIT 1"
            ).fetchone()
            if imported == "true" or has_entities is not None:
                return
            try:
                raw = json.loads(self.legacy_state_path.read_text(encoding="utf-8"))
                state = ensure_state_shape(raw)
            except (OSError, json.JSONDecodeError, DomainError) as exc:
                raise DomainError(
                    "legacy_state_unreadable",
                    "旧 JSON 快照无法导入 SQLite",
                    500,
                    {"path": str(self.legacy_state_path), "reason": str(exc)},
                ) from exc
            timestamp = _now()
            for container, kind in ENTITY_KINDS.items():
                for identifier, payload in state[container].items():
                    _upsert_entity(
                        connection,
                        kind=kind,
                        identifier=identifier,
                        payload=payload,
                        actor_id="migration",
                        action="import",
                        created_at=timestamp,
                    )
            revision = int(state["revision"])
            _set_meta(connection, "state_revision", str(revision))
            _set_meta(connection, "legacy_state_imported", "true")
            _append_audit(
                connection,
                actor_id="migration",
                action="import",
                resource_kind="snapshot",
                resource_id="legacy",
                revision=revision,
                details={"source": str(self.legacy_state_path)},
                context=None,
            )


def _load_state(connection: sqlite3.Connection) -> dict[str, Any]:
    state = empty_state()
    state["revision"] = int(_get_meta(connection, "state_revision", "0"))
    for container, kind in ENTITY_KINDS.items():
        state[container] = {
            row["id"]: json.loads(row["payload"])
            for row in connection.execute(
                """
                SELECT id, payload
                FROM entities
                WHERE kind = ?
                ORDER BY id
                """,
                (kind,),
            )
        }
    state["events"] = [
        {
            "sequence": row["sequence"],
            "action": row["action"],
            "resource": row["resource_kind"],
            "id": row["resource_id"],
            "revision": row["revision"],
        }
        for row in connection.execute(
            """
            SELECT sequence, action, resource_kind, resource_id, revision
            FROM audit_events
            ORDER BY sequence DESC
            LIMIT 100
            """
        )
    ]
    return state


def _changed_entities(
    baseline: dict[str, Any],
    working: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any], str]]:
    changes: list[tuple[str, str, dict[str, Any], str]] = []
    for container, kind in ENTITY_KINDS.items():
        before = baseline[container]
        after = working[container]
        for identifier, payload in after.items():
            if _canonical(before.get(identifier)) != _canonical(payload):
                changes.append((kind, identifier, payload, "upsert"))
        for identifier in set(before) - set(after):
            changes.append((kind, identifier, {}, "delete"))
    return changes


def _persist_changes(
    connection: sqlite3.Connection,
    changes: list[tuple[str, str, dict[str, Any], str]],
    *,
    actor_id: str,
    action: str,
    next_revision: int,
    context: Any,
) -> None:
    timestamp = _now()
    for kind, identifier, payload, operation in changes:
        if operation == "delete":
            connection.execute(
                "DELETE FROM entities WHERE kind = ? AND id = ?",
                (kind, identifier),
            )
        else:
            _upsert_entity(
                connection,
                kind=kind,
                identifier=identifier,
                payload=payload,
                actor_id=actor_id,
                action=action,
                created_at=timestamp,
            )
        _append_audit(
            connection,
            actor_id=actor_id,
            action=action,
            resource_kind=kind,
            resource_id=identifier,
            revision=int(payload.get("revision") or next_revision),
            details={
                "operation": operation,
                "route": getattr(context, "route_template", ""),
            },
            context=context,
        )
        _append_outbox(
            connection,
            topic=f"atlas.{kind}.changed",
            payload={
                "kind": kind,
                "id": identifier,
                "operation": operation,
                "revision": int(payload.get("revision") or next_revision),
                "actor_id": actor_id,
            },
        )


def _upsert_entity(
    connection: sqlite3.Connection,
    *,
    kind: str,
    identifier: str,
    payload: dict[str, Any],
    actor_id: str,
    action: str,
    created_at: str,
) -> None:
    serialized = _canonical(payload)
    revision = int(payload.get("revision") or 1)
    timestamp = str(payload.get("updated_at") or created_at)
    connection.execute(
        """
        INSERT INTO entities
        (kind, id, payload, revision, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, id) DO UPDATE SET
            payload = excluded.payload,
            revision = excluded.revision,
            updated_at = excluded.updated_at
        """,
        (
            kind,
            identifier,
            serialized,
            revision,
            str(payload.get("created_at") or created_at),
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO entity_versions
        (kind, id, revision, payload, actor_id, action, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kind,
            identifier,
            revision,
            serialized,
            actor_id,
            action,
            timestamp,
        ),
    )


def _append_audit(
    connection: sqlite3.Connection,
    *,
    actor_id: str,
    action: str,
    resource_kind: str,
    resource_id: str,
    revision: int,
    details: dict[str, Any],
    context: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events
        (event_id, actor_id, action, resource_kind, resource_id,
         revision, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"evt_{uuid.uuid4().hex}",
            actor_id,
            action,
            resource_kind,
            resource_id,
            revision,
            _canonical(details),
            _now(),
        ),
    )


def _append_outbox(
    connection: sqlite3.Connection,
    *,
    topic: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO outbox_events
        (event_id, topic, payload, status, attempts, created_at)
        VALUES (?, ?, ?, 'pending', 0, ?)
        """,
        (
            f"out_{uuid.uuid4().hex}",
            topic,
            _canonical(payload),
            _now(),
        ),
    )


def _read_idempotent_result(
    connection: sqlite3.Connection,
    context: Any,
) -> Any | None:
    row = connection.execute(
        """
        SELECT request_hash, response
        FROM idempotency_keys
        WHERE actor_id = ? AND key = ?
        """,
        (context.actor_id, context.idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if row["request_hash"] != context.request_hash:
        raise ConflictError(
            "idempotency_key_reused",
            "同一幂等键已用于不同请求",
            idempotency_key=context.idempotency_key,
        )
    return json.loads(row["response"])


def _store_idempotent_result(
    connection: sqlite3.Connection,
    context: Any,
    result: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO idempotency_keys
        (actor_id, key, request_hash, response, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(actor_id, key) DO UPDATE SET
            request_hash = excluded.request_hash,
            response = excluded.response,
            created_at = excluded.created_at
        """,
        (
            context.actor_id,
            context.idempotency_key,
            context.request_hash,
            _canonical(result),
            _now(),
        ),
    )


def _get_meta(
    connection: sqlite3.Connection,
    key: str,
    default: str | None = None,
) -> str | None:
    row = connection.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row is not None else default


def _set_meta(
    connection: sqlite3.Connection,
    key: str,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _count(connection: sqlite3.Connection, kind: str) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM entities WHERE kind = ?",
            (kind,),
        ).fetchone()["count"]
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _action_name(context: Any) -> str:
    template = str(getattr(context, "route_template", "") or "")
    method = str(getattr(context, "request_method", "") or "write").lower()
    if template.endswith("/confirm"):
        return "confirm"
    if template.endswith("/complete"):
        return "complete"
    if template.endswith("/close"):
        return "close"
    if method == "put":
        return "create"
    if method == "patch":
        return "update"
    if method == "delete":
        return "delete"
    return "write"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def request_fingerprint(
    *,
    actor_id: str,
    method: str,
    path: str,
    body: dict[str, Any],
) -> str:
    payload = _canonical(
        {
            "actor_id": actor_id,
            "method": method,
            "path": path,
            "body": body,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _outbox_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "topic": row["topic"],
        "payload": json.loads(row["payload"]),
        "status": row["status"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "published_at": row["published_at"],
    }
