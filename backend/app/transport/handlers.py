"""API 处理器与输入输出映射。"""

from __future__ import annotations

from typing import Any

from ..application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from ..domain.stages import active_stages
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
        from ..migration.runtime import RUNTIME

        return {
            "status": "ok",
            "service": "orchard-phenology-atlas",
            "schema_version": 3,
            "domain_generation": RUNTIME.generation,
            "active_migration_plan": RUNTIME.active_plan_id,
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
                for stage in active_stages()
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
    return router


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
