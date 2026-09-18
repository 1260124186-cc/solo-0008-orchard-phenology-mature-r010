"""领域迁移的 HTTP 处理器。"""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ..security import current_request_context
from .router import Router


PLAN_FIELDS = {"name", "batch_size", "rationale", "to_generation"}
CORRECTION_FIELDS = {"kind", "object_id", "expected_revision", "change"}
MERGE_FIELDS = {"survivor_id", "alias_id"}
VERIFY_FIELDS = {"note"}
ROLLBACK_FIELDS = {"reason"}


class MigrationHandlers:
    def __init__(self, migrations: Any) -> None:
        self.migrations = migrations

    def list_plans(self) -> dict[str, Any]:
        return self.migrations.list_plans()

    def create_plan(self, *, body: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(body) - PLAN_FIELDS)
        if unknown:
            raise ValidationError(
                "迁移计划包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        context = current_request_context()
        return self.migrations.create_plan(
            name=str(body.get("name") or ""),
            actor_id=context.actor_id,
            batch_size=int(body.get("batch_size") or 50),
            rationale=str(body.get("rationale") or ""),
            to_generation=int(body.get("to_generation") or 2),
        )

    def get_plan(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.get_plan(params["plan_id"])

    def pause_plan(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.pause(
            params["plan_id"],
            actor_id=current_request_context().actor_id,
        )

    def resume_plan(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.resume(
            params["plan_id"],
            actor_id=current_request_context().actor_id,
        )

    def rollback_plan(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(body) - ROLLBACK_FIELDS)
        if unknown:
            raise ValidationError(
                "回滚请求包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        return self.migrations.rollback_plan(
            params["plan_id"],
            actor_id=current_request_context().actor_id,
            reason=str(body.get("reason") or ""),
        )

    def finalize_plan(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.finalize_plan(
            params["plan_id"],
            actor_id=current_request_context().actor_id,
        )

    def verify_batch(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(body) - VERIFY_FIELDS)
        if unknown:
            raise ValidationError(
                "批次验证包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        return self.migrations.verify_batch(
            params["plan_id"],
            int(params["seq"]),
            actor_id=current_request_context().actor_id,
        )

    def apply_batch(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(body) - VERIFY_FIELDS)
        if unknown:
            raise ValidationError(
                "批次切换包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        return self.migrations.apply_batch(
            params["plan_id"],
            int(params["seq"]),
            actor_id=current_request_context().actor_id,
        )

    def list_incompatible(
        self,
        *,
        params: dict[str, str],
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        raw = query.get("batch_seq")
        batch_seq = int(raw[-1]) if raw else None
        return self.migrations.list_incompatible(
            params["plan_id"],
            batch_seq=batch_seq,
        )

    def correct_object(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(body) - CORRECTION_FIELDS)
        if unknown:
            raise ValidationError(
                "迁移更正包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        if "expected_revision" not in body:
            raise ValidationError("迁移更正需要 expected_revision")
        if not isinstance(body.get("change"), dict):
            raise ValidationError("change 必须是对象", field_name="change")
        return self.migrations.correct_object(
            params["plan_id"],
            kind=str(body.get("kind") or ""),
            object_id=str(body.get("object_id") or ""),
            change=body["change"],
            actor_id=current_request_context().actor_id,
            expected_revision=int(body["expected_revision"]),
        )

    def request_merge(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(body) - MERGE_FIELDS)
        if unknown:
            raise ValidationError(
                "合并请求包含不支持的字段",
                details={"unknown_fields": unknown},
            )
        return self.migrations.request_merge(
            params["plan_id"],
            survivor_id=str(body.get("survivor_id") or ""),
            alias_id=str(body.get("alias_id") or ""),
            actor_id=current_request_context().actor_id,
        )

    def list_corrections(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.list_corrections(params["plan_id"])

    def verify_report(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.migrations.verify_report(params["plan_id"])

    def runtime_status(self) -> dict[str, Any]:
        from ..migration.runtime import RUNTIME

        return {
            "generation": RUNTIME.generation,
            "active_plan": RUNTIME.active_plan_id,
            "finalized_plan": RUNTIME.finalized_plan_id,
            "canonical_tree_count": len(RUNTIME.canonical_tree),
        }


def build_migration_router(handlers: MigrationHandlers) -> Router:
    router = Router()
    router.add(
        "GET",
        "/api/migration/status",
        handlers.runtime_status,
        capability="migration:read",
        resource_kind="migration",
    )
    router.add(
        "GET",
        "/api/migration/plans",
        handlers.list_plans,
        capability="migration:read",
        resource_kind="migration",
    )
    router.add(
        "PUT",
        "/api/migration/plans",
        handlers.create_plan,
        capability="migration:admin",
        resource_kind="migration",
    )
    router.add(
        "GET",
        "/api/migration/plans/{plan_id}",
        handlers.get_plan,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/pause",
        handlers.pause_plan,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/resume",
        handlers.resume_plan,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/rollback",
        handlers.rollback_plan,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/finalize",
        handlers.finalize_plan,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/batches/{seq}/verify",
        handlers.verify_batch,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/batches/{seq}/apply",
        handlers.apply_batch,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "GET",
        "/api/migration/plans/{plan_id}/incompatible",
        handlers.list_incompatible,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "GET",
        "/api/migration/plans/{plan_id}/report",
        handlers.verify_report,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/corrections",
        handlers.correct_object,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "GET",
        "/api/migration/plans/{plan_id}/corrections",
        handlers.list_corrections,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    router.add(
        "PUT",
        "/api/migration/plans/{plan_id}/merges",
        handlers.request_merge,
        capability="migration:admin",
        resource_kind="migration",
        resource_id_param="plan_id",
    )
    return router
