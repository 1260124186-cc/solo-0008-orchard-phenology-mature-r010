"""API 处理器与输入输出映射。"""

from __future__ import annotations

from typing import Any

from ..application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from ..domain.stages import STAGES
from ..errors import ValidationError
from ..jobs import JobService
from ..persistence import Repository
from ..security import IdentityService, current_request_context
from .router import Router


class ApiHandlers:
    def __init__(
        self,
        catalog: CatalogService,
        observations: ObservationService,
        comparisons: ComparisonService,
        briefs: BriefService,
        repository: Repository,
        jobs: JobService,
        identity: IdentityService,
    ) -> None:
        self.catalog = catalog
        self.observations = observations
        self.comparisons = comparisons
        self.briefs = briefs
        self.repository = repository
        self.jobs = jobs
        self.identity = identity

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "orchard-phenology-atlas",
            "schema_version": 2,
        }

    def stage_catalog(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "key": stage.key,
                    "label": stage.label,
                    "rank": stage.rank,
                    "required_for_completion": stage.required_for_completion,
                }
                for stage in STAGES
            ]
        }

    def list_plots(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.catalog.list_plots(
            status=_optional_query(query, "status"),
            query=_optional_query(query, "q"),
        )

    def create_plot(self, *, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.create_plot(body)

    def get_plot(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.catalog.get_plot(params["plot_id"])

    def update_plot(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.catalog.update_plot(params["plot_id"], body)

    def confirm_plot(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"revision"}
        _reject_unknown(body, allowed, "确认园区")
        return self.catalog.confirm_plot(
            params["plot_id"],
            expected_revision=_optional_revision(body),
        )

    def list_trees(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.catalog.list_trees(
            plot_id=_optional_query(query, "plot_id"),
            status=_optional_query(query, "status"),
        )

    def create_tree(self, *, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.create_tree(body)

    def get_tree(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.catalog.get_tree(params["tree_id"])

    def retire_tree(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.catalog.retire_tree(params["tree_id"], body)

    def list_observations(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.observations.list_observations(
            plot_id=_optional_query(query, "plot_id"),
            tree_id=_optional_query(query, "tree_id"),
            season=_optional_query(query, "season"),
            status=_optional_query(query, "status"),
        )

    def create_observation(self, *, body: dict[str, Any]) -> dict[str, Any]:
        return self.observations.start_observation(body)

    def get_observation(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.observations.get_observation(params["observation_id"])

    def update_observation(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.observations.update_observation(
            params["observation_id"],
            body,
        )

    def add_observation_stage(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.observations.add_stage(params["observation_id"], body)

    def remove_observation_stage(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.observations.remove_stage(
            params["observation_id"],
            params["stage"],
            body,
        )

    def complete_observation(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.observations.complete_observation(
            params["observation_id"],
            body,
        )

    def list_comparisons(self) -> dict[str, Any]:
        return self.comparisons.list_comparisons()

    def create_comparison(self, *, body: dict[str, Any]) -> dict[str, Any]:
        return self.comparisons.create_comparison(body)

    def get_comparison(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.comparisons.get_comparison(params["comparison_id"])

    def list_briefs(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.briefs.list_briefs(plot_id=_optional_query(query, "plot_id"))

    def create_brief(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.briefs.create_brief(params["plot_id"], body)

    def get_brief(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.briefs.get_brief(params["brief_id"])

    def list_audit(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.repository.list_audit_events(
            resource_kind=_optional_query(query, "resource_kind"),
            resource_id=_optional_query(query, "resource_id"),
            actor_id=_optional_query(query, "actor_id"),
            limit=_optional_int_query(query, "limit", 100, 1000),
        )

    def list_versions(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.repository.list_entity_versions(
            kind=params["kind"],
            identifier=params["identifier"],
            limit=100,
        )

    def list_outbox(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.repository.list_outbox_events(
            status=_optional_query(query, "status"),
            topic=_optional_query(query, "topic"),
            limit=_optional_int_query(query, "limit", 100, 1000),
        )

    def publish_outbox(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.repository.publish_outbox_event(params["event_id"])

    def list_jobs(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.jobs.list_jobs(
            status=_optional_query(query, "status"),
            job_type=_optional_query(query, "job_type"),
            actor_id=_optional_query(query, "actor_id"),
            limit=_optional_int_query(query, "limit", 100, 500),
        )

    def enqueue_job(self, *, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "job_type",
            "payload",
            "max_attempts",
            "priority",
            "available_at",
        }
        _reject_unknown(body, allowed, "后台任务")
        context = current_request_context()
        return self.jobs.enqueue(
            actor_id=context.actor_id,
            job_type=str(body.get("job_type") or ""),
            payload=body.get("payload") or {},
            max_attempts=int(body.get("max_attempts") or 3),
            priority=int(body.get("priority") or 100),
            available_at=body.get("available_at"),
            idempotency_key=context.idempotency_key,
        )

    def get_job(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.jobs.get_job(params["job_id"])

    def cancel_job(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        _reject_unknown(body, set(), "取消任务")
        return self.jobs.cancel(
            params["job_id"],
            actor_id=current_request_context().actor_id,
        )

    def retry_job(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        _reject_unknown(body, set(), "重试任务")
        return self.jobs.retry(
            params["job_id"],
            actor_id=current_request_context().actor_id,
        )

    def list_actors(self) -> dict[str, Any]:
        return self.identity.list_actors()

    def create_actor(self, *, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {"id", "display_name", "status"}
        _reject_unknown(body, allowed, "操作者")
        return self.identity.create_actor(
            actor_id=str(body.get("id") or ""),
            display_name=str(body.get("display_name") or ""),
            status=str(body.get("status") or "active"),
        )

    def list_grants(
        self,
        *,
        query: dict[str, list[str]],
    ) -> dict[str, Any]:
        return self.identity.list_grants(
            actor_id=_optional_query(query, "actor_id"),
        )

    def create_grant(self, *, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "actor_id",
            "capability",
            "resource_kind",
            "resource_id",
            "expires_at",
        }
        _reject_unknown(body, allowed, "授权")
        return self.identity.grant(
            actor_id=str(body.get("actor_id") or ""),
            capability=str(body.get("capability") or ""),
            resource_kind=str(body.get("resource_kind") or ""),
            resource_id=str(body.get("resource_id") or "*"),
            expires_at=body.get("expires_at"),
        )

    def revoke_grant(self, *, params: dict[str, str]) -> dict[str, Any]:
        return self.identity.revoke(params["grant_id"])

    # ------------------------------------------------------------------
    # 安全迁移
    # ------------------------------------------------------------------
    def migration_overview(self) -> dict[str, Any]:
        from ..migration.service import MigrationService

        service = MigrationService(self.repository)
        run = service.ledger.active_run()
        return {
            "active_run": service.status(run["run_id"]) if run else None,
            "runs": service.ledger.list_runs()["items"],
            "change_sets": {
                key: value.describe()
                for key, value in __import__(
                    "app.migration", fromlist=["CHANGE_SETS"]
                ).CHANGE_SETS.items()
            },
        }

    def create_migration(self, *, body: dict[str, Any]) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, {"batch_size", "change_set_id"}, "迁移规划")
        actor_id = current_request_context().actor_id
        service = MigrationService(self.repository)
        status = service.create_plan(
            actor_id=actor_id,
            batch_size=int(body.get("batch_size") or 20),
            change_set_id=str(
                body.get("change_set_id")
                or "phenology-v2-stage-precision-scope-reference"
            ),
        )
        return status

    def get_migration(self, *, params: dict[str, str]) -> dict[str, Any]:
        from ..migration.service import MigrationService

        return MigrationService(self.repository).status(params["run_id"])

    def run_migration_batch(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, {"enqueue_job"}, "迁移批次")
        actor_id = current_request_context().actor_id
        service = MigrationService(self.repository)
        if body.get("enqueue_job"):
            enqueued = self.jobs.enqueue(
                actor_id=actor_id,
                job_type="migration_run_batch",
                payload={"run_id": params["run_id"], "actor_id": actor_id},
            )
            return {"enqueued": enqueued}
        return service.run_next_batch(actor_id=actor_id)

    def stop_migration(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, set(), "停止迁移")
        return MigrationService(self.repository).request_stop(
            actor_id=current_request_context().actor_id,
        )

    def resume_migration(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, set(), "继续迁移")
        return MigrationService(self.repository).resume(
            actor_id=current_request_context().actor_id,
        )

    def rollback_migration(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, set(), "回滚迁移")
        return MigrationService(self.repository).rollback(
            actor_id=current_request_context().actor_id,
        )

    def complete_migration(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, set(), "完成迁移")
        return MigrationService(self.repository).complete(
            actor_id=current_request_context().actor_id,
        )

    def migration_incompatible(self, *, params: dict[str, str]) -> dict[str, Any]:
        from ..migration.service import MigrationService

        return MigrationService(self.repository).incompatible_objects(params["run_id"])

    def migration_failures(self, *, params: dict[str, str]) -> dict[str, Any]:
        from ..migration.service import MigrationService

        return MigrationService(self.repository).failures(params["run_id"])

    def migration_events(self, *, params: dict[str, str]) -> dict[str, Any]:
        from ..migration.service import MigrationService

        service = MigrationService(self.repository)
        return service.ledger.list_events(params["run_id"])

    def retry_migration_batch(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, set(), "重试迁移批次")
        return MigrationService(self.repository).retry_failed_batch(
            batch_no=int(params["batch_no"]),
            actor_id=current_request_context().actor_id,
        )

    def merge_migration_trees(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, {"canonical_tree_id", "member_tree_ids"}, "迁移合并")
        member_ids = body.get("member_tree_ids") or []
        if not isinstance(member_ids, list) or not all(
            isinstance(item, str) for item in member_ids
        ):
            raise ValidationError("成员植株必须是标识数组", field_name="member_tree_ids")
        return MigrationService(self.repository).merge_trees(
            actor_id=current_request_context().actor_id,
            canonical_tree_id=str(body.get("canonical_tree_id") or ""),
            member_tree_ids=[str(item) for item in member_ids],
        )

    def record_migration_correction(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(
            body,
            {"kind", "object_id", "note"},
            "迁移更正",
        )
        return MigrationService(self.repository).record_correction(
            actor_id=current_request_context().actor_id,
            kind=str(body.get("kind") or ""),
            object_id=str(body.get("object_id") or ""),
            note=str(body.get("note") or ""),
        )

    def revoke_migration_grant(
        self,
        *,
        params: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..migration.service import MigrationService

        _reject_unknown(body, {"grant_id"}, "迁移期撤销授权")
        return MigrationService(self.repository).revoke_grant(
            actor_id=current_request_context().actor_id,
            grant_id=str(body.get("grant_id") or params.get("grant_id") or ""),
        )

    def migration_point_in_time(self, *, params: dict[str, str]) -> dict[str, Any]:
        from ..migration.service import MigrationService
        from ..migration.verifier import MigrationVerifier

        service = MigrationService(self.repository)
        run_id = params["run_id"]
        reference_map = service.ledger.get_reference_map(run_id)
        verifier = MigrationVerifier(self.repository.database)
        with self.repository.database.read_connection() as connection:
            return verifier.point_in_time_check(
                connection,
                kind=params["kind"],
                object_id=params["identifier"],
                revisions=[
                    int(value)
                    for value in params["revisions"].split(",")
                    if value.strip()
                ],
                reference_map=reference_map,
            )


def build_router(handlers: ApiHandlers) -> Router:
    router = Router()
    router.add("GET", "/api/health", handlers.health)
    router.add("GET", "/api/stages", handlers.stage_catalog)

    router.add(
        "GET",
        "/api/plots",
        handlers.list_plots,
        capability="plot:read",
        resource_kind="plot",
    )
    router.add(
        "PUT",
        "/api/plots",
        handlers.create_plot,
        capability="plot:write",
        resource_kind="plot",
    )
    router.add(
        "GET",
        "/api/plots/{plot_id}",
        handlers.get_plot,
        capability="plot:read",
        resource_kind="plot",
        resource_id_param="plot_id",
    )
    router.add(
        "PATCH",
        "/api/plots/{plot_id}",
        handlers.update_plot,
        capability="plot:write",
        resource_kind="plot",
        resource_id_param="plot_id",
    )
    router.add(
        "PUT",
        "/api/plots/{plot_id}/confirm",
        handlers.confirm_plot,
        capability="plot:write",
        resource_kind="plot",
        resource_id_param="plot_id",
    )
    router.add(
        "PUT",
        "/api/plots/{plot_id}/briefs",
        handlers.create_brief,
        capability="brief:write",
        resource_kind="plot",
        resource_id_param="plot_id",
    )

    router.add(
        "GET",
        "/api/trees",
        handlers.list_trees,
        capability="tree:read",
        resource_kind="tree",
    )
    router.add(
        "PUT",
        "/api/trees",
        handlers.create_tree,
        capability="tree:write",
        resource_kind="tree",
    )
    router.add(
        "GET",
        "/api/trees/{tree_id}",
        handlers.get_tree,
        capability="tree:read",
        resource_kind="tree",
        resource_id_param="tree_id",
    )
    router.add(
        "PUT",
        "/api/trees/{tree_id}/close",
        handlers.retire_tree,
        capability="tree:write",
        resource_kind="tree",
        resource_id_param="tree_id",
    )

    router.add(
        "GET",
        "/api/observations",
        handlers.list_observations,
        capability="observation:read",
        resource_kind="observation",
    )
    router.add(
        "PUT",
        "/api/observations",
        handlers.create_observation,
        capability="observation:write",
        resource_kind="observation",
    )
    router.add(
        "GET",
        "/api/observations/{observation_id}",
        handlers.get_observation,
        capability="observation:read",
        resource_kind="observation",
        resource_id_param="observation_id",
    )
    router.add(
        "PATCH",
        "/api/observations/{observation_id}",
        handlers.update_observation,
        capability="observation:write",
        resource_kind="observation",
        resource_id_param="observation_id",
    )
    router.add(
        "PUT",
        "/api/observations/{observation_id}/stages",
        handlers.add_observation_stage,
        capability="observation:write",
        resource_kind="observation",
        resource_id_param="observation_id",
    )
    router.add(
        "DELETE",
        "/api/observations/{observation_id}/stages/{stage}",
        handlers.remove_observation_stage,
        capability="observation:write",
        resource_kind="observation",
        resource_id_param="observation_id",
    )
    router.add(
        "PUT",
        "/api/observations/{observation_id}/complete",
        handlers.complete_observation,
        capability="observation:write",
        resource_kind="observation",
        resource_id_param="observation_id",
    )

    router.add(
        "GET",
        "/api/comparisons",
        handlers.list_comparisons,
        capability="comparison:read",
        resource_kind="comparison",
    )
    router.add(
        "PUT",
        "/api/comparisons",
        handlers.create_comparison,
        capability="comparison:write",
        resource_kind="comparison",
    )
    router.add(
        "GET",
        "/api/comparisons/{comparison_id}",
        handlers.get_comparison,
        capability="comparison:read",
        resource_kind="comparison",
        resource_id_param="comparison_id",
    )

    router.add(
        "GET",
        "/api/briefs",
        handlers.list_briefs,
        capability="brief:read",
        resource_kind="brief",
    )
    router.add(
        "GET",
        "/api/briefs/{brief_id}",
        handlers.get_brief,
        capability="brief:read",
        resource_kind="brief",
        resource_id_param="brief_id",
    )
    router.add(
        "GET",
        "/api/audit",
        handlers.list_audit,
        capability="audit:read",
        resource_kind="audit",
    )
    router.add(
        "GET",
        "/api/versions/{kind}/{identifier}",
        handlers.list_versions,
        capability="audit:read",
        resource_kind="audit",
    )
    router.add(
        "GET",
        "/api/outbox",
        handlers.list_outbox,
        capability="outbox:read",
        resource_kind="outbox",
    )
    router.add(
        "PUT",
        "/api/outbox/{event_id}/publish",
        handlers.publish_outbox,
        capability="outbox:write",
        resource_kind="outbox",
        resource_id_param="event_id",
    )
    router.add(
        "GET",
        "/api/jobs",
        handlers.list_jobs,
        capability="job:read",
        resource_kind="job",
    )
    router.add(
        "PUT",
        "/api/jobs",
        handlers.enqueue_job,
        capability="job:write",
        resource_kind="job",
    )
    router.add(
        "GET",
        "/api/jobs/{job_id}",
        handlers.get_job,
        capability="job:read",
        resource_kind="job",
        resource_id_param="job_id",
    )
    router.add(
        "PUT",
        "/api/jobs/{job_id}/cancel",
        handlers.cancel_job,
        capability="job:write",
        resource_kind="job",
        resource_id_param="job_id",
    )
    router.add(
        "PUT",
        "/api/jobs/{job_id}/retry",
        handlers.retry_job,
        capability="job:write",
        resource_kind="job",
        resource_id_param="job_id",
    )
    router.add(
        "GET",
        "/api/actors",
        handlers.list_actors,
        capability="actor:read",
        resource_kind="actor",
    )
    router.add(
        "PUT",
        "/api/actors",
        handlers.create_actor,
        capability="actor:write",
        resource_kind="actor",
    )
    router.add(
        "GET",
        "/api/grants",
        handlers.list_grants,
        capability="grant:read",
        resource_kind="grant",
    )
    router.add(
        "PUT",
        "/api/grants",
        handlers.create_grant,
        capability="grant:write",
        resource_kind="grant",
    )
    router.add(
        "PUT",
        "/api/grants/{grant_id}/revoke",
        handlers.revoke_grant,
        capability="grant:write",
        resource_kind="grant",
        resource_id_param="grant_id",
    )

    _build_migration_routes(router, handlers)
    return router


def _build_migration_routes(router: Router, handlers: ApiHandlers) -> None:
    router.add(
        "GET",
        "/api/migrations",
        handlers.migration_overview,
        capability="migration:read",
        resource_kind="migration",
    )
    router.add(
        "PUT",
        "/api/migrations",
        handlers.create_migration,
        capability="migration:write",
        resource_kind="migration",
    )
    router.add(
        "GET",
        "/api/migrations/{run_id}",
        handlers.get_migration,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/batches/next",
        handlers.run_migration_batch,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/stop",
        handlers.stop_migration,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/resume",
        handlers.resume_migration,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/rollback",
        handlers.rollback_migration,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/complete",
        handlers.complete_migration,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "GET",
        "/api/migrations/{run_id}/incompatible",
        handlers.migration_incompatible,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "GET",
        "/api/migrations/{run_id}/failures",
        handlers.migration_failures,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "GET",
        "/api/migrations/{run_id}/events",
        handlers.migration_events,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/batches/{batch_no}/retry",
        handlers.retry_migration_batch,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/trees/merge",
        handlers.merge_migration_trees,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/corrections",
        handlers.record_migration_correction,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "PUT",
        "/api/migrations/{run_id}/grants/revoke",
        handlers.revoke_migration_grant,
        capability="migration:write",
        resource_kind="migration",
        resource_id_param="run_id",
    )
    router.add(
        "GET",
        "/api/migrations/{run_id}/point-in-time/{kind}/{identifier}/{revisions}",
        handlers.migration_point_in_time,
        capability="migration:read",
        resource_kind="migration",
        resource_id_param="run_id",
    )


def _optional_query(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def _optional_revision(body: dict[str, Any]) -> int | None:
    if "revision" not in body:
        return None
    value = body["revision"]
    if isinstance(value, bool):
        raise ValidationError("修订号必须是整数", field_name="revision")
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("修订号必须是整数", field_name="revision") from exc
    if revision < 1:
        raise ValidationError("修订号必须大于零", field_name="revision")
    return revision


def _optional_int_query(
    query: dict[str, list[str]],
    key: str,
    default: int,
    maximum: int,
) -> int:
    raw = _optional_query(query, key)
    if raw is None:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError as exc:
        raise ValidationError("查询参数必须是整数", field_name=key) from exc


def _reject_unknown(
    body: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationError(
            f"{label}包含不支持的字段",
            details={"unknown_fields": unknown},
        )
