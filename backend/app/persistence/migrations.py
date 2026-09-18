"""版本化数据库迁移。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..errors import DomainError

if TYPE_CHECKING:
    from .database import Database


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "entity store and revision history",
        (
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS entities (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                revision INTEGER NOT NULL CHECK (revision > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS entities_kind_updated_idx
            ON entities (kind, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS entity_versions (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, id, revision)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS entity_versions_created_idx
            ON entity_versions (created_at DESC)
            """,
        ),
    ),
    Migration(
        2,
        "identity, audit, outbox and idempotency",
        (
            """
            CREATE TABLE IF NOT EXISTS actors (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS access_grants (
                id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL REFERENCES actors(id),
                capability TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS access_grants_lookup_idx
            ON access_grants (actor_id, capability, resource_kind, resource_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                details TEXT NOT NULL CHECK (json_valid(details)),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS audit_events_resource_idx
            ON audit_events (resource_kind, resource_id, sequence DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS outbox_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                status TEXT NOT NULL CHECK (status IN ('pending', 'published', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                published_at TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS outbox_status_idx
            ON outbox_events (status, id)
            """,
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                actor_id TEXT NOT NULL,
                key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response TEXT NOT NULL CHECK (json_valid(response)),
                created_at TEXT NOT NULL,
                PRIMARY KEY (actor_id, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                status TEXT NOT NULL CHECK (
                    status IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter', 'cancelled')
                ),
                attempt INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                priority INTEGER NOT NULL DEFAULT 100,
                available_at TEXT NOT NULL,
                lease_expires_at TEXT,
                worker_id TEXT,
                result TEXT CHECK (result IS NULL OR json_valid(result)),
                error TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS jobs_claim_idx
            ON jobs (status, available_at, priority, created_at)
            """,
        ),
    ),
    Migration(
        3,
        "safe dual-read domain migration ledger",
        (
            # 每次迁移活动：进度、批次检查点、停止/回滚决定都落在此处。
            """
            CREATE TABLE IF NOT EXISTS migration_runs (
                run_id TEXT PRIMARY KEY,
                change_set TEXT NOT NULL,
                from_version INTEGER NOT NULL,
                to_version INTEGER NOT NULL,
                batch_size INTEGER NOT NULL CHECK (batch_size > 0),
                status TEXT NOT NULL CHECK (status IN (
                    'planning', 'shadow', 'running', 'paused',
                    'blocked', 'completed', 'rolling_back', 'rolled_back'
                )),
                total_objects INTEGER NOT NULL DEFAULT 0,
                switched_objects INTEGER NOT NULL DEFAULT 0,
                incompatible_objects INTEGER NOT NULL DEFAULT 0,
                retained_objects INTEGER NOT NULL DEFAULT 0,
                cursor TEXT,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                rollback_requested INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                last_error TEXT
            )
            """,
            # 每个对象的迁移状态：双读指纹、失败原因、不兼容集合、保留依据。
            """
            CREATE TABLE IF NOT EXISTS migration_objects (
                run_id TEXT NOT NULL REFERENCES migration_runs(run_id),
                kind TEXT NOT NULL,
                object_id TEXT NOT NULL,
                batch_no INTEGER,
                status TEXT NOT NULL CHECK (status IN (
                    'pending', 'incompatible', 'ready',
                    'switching', 'switched', 'failed', 'retained'
                )),
                legacy_fingerprint TEXT,
                new_fingerprint TEXT,
                legacy_payload TEXT CHECK (legacy_payload IS NULL OR json_valid(legacy_payload)),
                new_payload TEXT CHECK (new_payload IS NULL OR json_valid(new_payload)),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                switched_at TEXT,
                retained_reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, kind, object_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS migration_objects_run_idx
            ON migration_objects (run_id, status, batch_no, kind, object_id)
            """,
            # 每批检查点：比较结果、业务指纹、血缘/审计/outbox 计数都可回溯。
            """
            CREATE TABLE IF NOT EXISTS migration_batches (
                run_id TEXT NOT NULL REFERENCES migration_runs(run_id),
                batch_no INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'planned', 'running', 'succeeded', 'failed', 'reverted'
                )),
                object_count INTEGER NOT NULL DEFAULT 0,
                checkpoint TEXT NOT NULL CHECK (json_valid(checkpoint)),
                started_at TEXT,
                finished_at TEXT,
                last_error TEXT,
                PRIMARY KEY (run_id, batch_no)
            )
            """,
            # 迁移期间发生的更正、合并、授权撤销等干预动作。
            """
            CREATE TABLE IF NOT EXISTS migration_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES migration_runs(run_id),
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload TEXT NOT NULL CHECK (json_valid(payload)),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS migration_events_run_idx
            ON migration_events (run_id, id)
            """,
        ),
    ),
)


def migrate(database: "Database") -> None:
    connection = database.connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            try:
                connection.execute("BEGIN IMMEDIATE")
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        _now(),
                    ),
                )
                if migration.version == 2:
                    _seed_actors(connection)
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                raise DomainError(
                    "migration_failed",
                    f"数据库迁移 {migration.version} 失败",
                    500,
                    {"name": migration.name, "reason": str(exc)},
                ) from exc
    finally:
        connection.close()


def _seed_actors(connection: object) -> None:
    timestamp = _now()
    connection.execute(
        """
        INSERT OR IGNORE INTO actors (id, display_name, status, created_at)
        VALUES (?, ?, 'active', ?)
        """,
        ("local-admin", "本机管理员", timestamp),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO actors (id, display_name, status, created_at)
        VALUES (?, ?, 'active', ?)
        """,
        ("local-observer", "本机观察员", timestamp),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO access_grants
        (id, actor_id, capability, resource_kind, resource_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "grant_local_admin_all",
            "local-admin",
            "*",
            "*",
            "*",
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO access_grants
        (id, actor_id, capability, resource_kind, resource_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "grant_local_observer_read",
            "local-observer",
            "plot:read",
            "plot",
            "*",
            timestamp,
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
