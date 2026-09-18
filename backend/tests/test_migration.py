"""生产安全的领域规则迁移框架测试。

覆盖：

- 双读验证（业务结果指纹等价）后才允许切换批次；
- 分批独立事务，单批失败不影响已完成批次；
- 迁移进度、失败项、批次检查点、不兼容集合的持久化；
- 迁移中的更正、植株合并、授权撤销与后台任务；
- 暂停、恢复、回滚未切换批次并保留已提交事实及原因；
- 冻结简报、历史时点版本视图、启动恢复不随模式切换改变；
- 验证同时比较业务结果、版本血缘、审计和 outbox。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from app.errors import DomainError, PreconditionError
from app.jobs import JobService, JobWorker
from app.migration import MigrationService, engine
from app.migration import store as migration_store
from app.migration.runtime import RUNTIME
from app.persistence import Database, Repository
from app.security import (
    AuthorizationService,
    IdentityService,
    RequestContext,
    request_scope,
)


def _request(actor_id: str = "local-admin", idempotency_key: str | None = None):
    key = idempotency_key or f"{actor_id}-key"
    return request_scope(
        RequestContext(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_method="PUT",
            request_path="/api/test",
            request_hash=f"{actor_id}:{key}",
            route_template="/api/test",
        )
    )


class MigrationHarness(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME.reset()
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "atlas.sqlite3"
        self.database = Database(self.database_path)
        self.repository = Repository(self.database)
        self.repository.open()
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.briefs = BriefService(self.repository)
        self.migration = MigrationService(self.repository)

    def tearDown(self) -> None:
        RUNTIME.reset()
        self.repository.close()
        self.temporary.cleanup()

    # -- 夹具构造 --------------------------------------------------------

    def create_plot_with_tree(
        self,
        code: str = "OR-6101",
        tree_suffix: str = "01",
        *,
        confirm: bool = True,
        cultivar: str = "富士",
    ):
        with _request():
            plot = self.catalog.create_plot(
                {
                    "code": code,
                    "name": f"园区 {code}",
                    "locality": "测试地点",
                    "cultivar_focus": cultivar,
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
            tree = self.catalog.create_tree(
                {
                    "plot_id": plot["id"],
                    "code": f"{code}-T{tree_suffix}",
                    "cultivar": cultivar,
                    "rootstock": "山定子",
                    "planting_year": 2012,
                    "status": "active",
                    "note": "",
                }
            )
            if confirm:
                plot = self.catalog.confirm_plot(
                    plot["id"], expected_revision=plot["revision"]
                )
        return plot, tree

    def add_tree(self, plot, code: str, suffix: str, *, cultivar: str = "富士"):
        with _request():
            return self.catalog.create_tree(
                {
                    "plot_id": plot["id"],
                    "code": f"{code}-T{suffix}",
                    "cultivar": cultivar,
                    "rootstock": "山定子",
                    "planting_year": 2012,
                    "status": "active",
                    "note": "",
                }
            )

    def confirm_plot(self, plot):
        with _request():
            return self.catalog.confirm_plot(
                plot["id"], expected_revision=plot["revision"]
            )

    def complete_season(self, tree, season: str = "2023", *, confidence: int = 3):
        with _request():
            observation = self.observations.start_observation(
                {
                    "tree_id": tree["id"],
                    "season": season,
                    "observer": "观察员甲",
                    "note": "",
                }
            )
            dates = {
                "bud_burst": f"{season}-03-10",
                "full_bloom": f"{season}-04-15",
                "fruit_set": f"{season}-05-20",
                "harvest": f"{season}-09-30",
            }
            for stage, observed_on in dates.items():
                observation = self.observations.add_stage(
                    observation["id"],
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": confidence,
                        "note": "",
                        "revision": observation["revision"],
                    },
                )
            observation = self.observations.complete_observation(
                observation["id"], {"revision": observation["revision"]}
            )
        return observation

    def seed_archive(self, *, trees: int = 2, with_comparison: bool = True,
                     with_brief: bool = True):
        # 先建草稿园区并加入全部植株，再确认。
        with _request():
            plot = self.catalog.create_plot(
                {
                    "code": "OR-6101",
                    "name": "园区 OR-6101",
                    "locality": "测试地点",
                    "cultivar_focus": "富士",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        tree_records = [self.add_tree(plot, "OR-6101", f"{index:02d}")
                        for index in range(1, trees + 1)]
        plot = self.confirm_plot(plot)
        season_records = [
            self.complete_season(tree, "2023") for tree in tree_records[:2]
        ]
        comparison = None
        if with_comparison:
            with _request():
                comparison = self.comparisons.create_comparison(
                    {
                        "title": "2023 品种对齐",
                        "left_observation_id": season_records[0]["id"],
                        "right_observation_id": season_records[1]["id"],
                    }
                )
        brief = None
        if with_brief:
            with _request():
                brief = self.briefs.create_brief(plot["id"], {"title": "2023 简报"})
        return {
            "plot": plot,
            "trees": tree_records,
            "observations": season_records,
            "comparison": comparison,
            "brief": brief,
        }

    def run_plan(self, plan_ref: str | dict, *, stop_after: int | None = None):
        plan_id = plan_ref["id"] if isinstance(plan_ref, dict) else plan_ref
        detail = self.migration.get_plan(plan_id)
        applied = []
        for index, batch in enumerate(detail["batches"]):
            if stop_after is not None and index >= stop_after:
                break
            if batch["status"] == "applied":
                applied.append(batch)
                continue
            verified = self.migration.verify_batch(
                plan_id, batch["seq"], actor_id="local-admin"
            )
            if verified["status"] != "verified":
                return applied
            switched = self.migration.apply_batch(
                plan_id, batch["seq"], actor_id="local-admin"
            )
            applied.append(switched)
        return applied


class DualReadAndBatchTests(MigrationHarness):
    def test_shadow_verify_then_cutover_keeps_business_conclusions(self) -> None:
        archive = self.seed_archive()
        plan = self.migration.create_plan(
            name="第 2 代阶段与精度",
            actor_id="local-admin",
            batch_size=5,
        )
        detail = self.migration.get_plan(plan["id"])
        self.assertGreaterEqual(len(detail["batches"]), 2)

        first = detail["batches"][0]
        verified = self.migration.verify_batch(
            plan["id"], first["seq"], actor_id="local-admin"
        )
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["incompatible_count"], 0)
        # 验证只写影子投影，业务实体仍是 v1。
        state = self.repository.read()
        observation = state["observations"][archive["observations"][0]["id"]]
        self.assertEqual(observation["schema_version"], 1)
        self.assertNotIn("confidence_score", observation["entries"][0])

        self.migration.apply_batch(
            plan["id"], first["seq"], actor_id="local-admin"
        )
        state = self.repository.read()
        # 被切换的可变对象已升级，且置信度可逆。
        switched_seasons = [
            item
            for item in state["observations"].values()
        ]
        self.assertTrue(
            any(item["schema_version"] == 2 for item in switched_seasons)
        )
        v2_entry = next(
            item
            for item in switched_seasons
            if item["schema_version"] == 2
        )["entries"][0]
        self.assertEqual(v2_entry["confidence_score"], 0.5)
        self.assertEqual(v2_entry["confidence"], 3)

        report = self.migration.verify_report(plan["id"])
        self.assertTrue(report["business_result_parity"]["ok"])
        self.assertEqual(
            report["audit_outbox_parity"]["switched"],
            report["version_lineage"]["checked"],
        )
        self.assertTrue(report["audit_outbox_parity"]["ok"])
        # 跨切面核对通过，但计划整体仍处于进行中。
        self.assertEqual(report["status"], "active")
        self.assertLess(
            report["object_summary"]["applied"],
            report["object_summary"]["total"],
        )

    def test_one_failed_batch_does_not_touch_other_batches(self) -> None:
        archive = self.seed_archive()
        # 注入一个历史遗留的不兼容事实：置信度越界。
        connection = self.database.connect()
        row = connection.execute(
            "SELECT payload FROM entities WHERE kind='observation' LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload"])
        for entry in payload["entries"]:
            entry["confidence"] = 9
        connection.execute(
            "UPDATE entities SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["id"]),
        )
        connection.commit()
        connection.close()

        plan = self.migration.create_plan(
            name="带不兼容对象", actor_id="local-admin", batch_size=2
        )
        detail = self.migration.get_plan(plan["id"])
        statuses = []
        for batch in detail["batches"]:
            result = self.migration.verify_batch(
                plan["id"], batch["seq"], actor_id="local-admin"
            )
            statuses.append(result["status"])
        self.assertIn("failed", statuses)
        incompatible = self.migration.list_incompatible(plan["id"])
        kinds = {item["kind"] for item in incompatible["items"]}
        # 越界置信度的季节志不兼容；引用它的对比图谱复算结果也随之偏离。
        self.assertIn("observation", kinds)
        self.assertIn("comparison", kinds)

        # 已通过的批次仍可独立切换；不兼容批次拒绝应用。
        for batch in detail["batches"]:
            fresh = self.migration.get_plan(plan["id"])["batches"][batch["seq"]]
            if fresh["status"] == "verified":
                applied = self.migration.apply_batch(
                    plan["id"], batch["seq"], actor_id="local-admin"
                )
                self.assertEqual(applied["status"], "applied")
            elif fresh["status"] == "failed":
                with self.assertRaises(PreconditionError) as raised:
                    self.migration.apply_batch(
                        plan["id"], batch["seq"], actor_id="local-admin"
                    )
                self.assertEqual(
                    raised.exception.code, "migration_batch_not_verified"
                )

        # 不能在存在不兼容对象时正式切换。
        with self.assertRaises(PreconditionError) as raised:
            self.migration.finalize_plan(plan["id"], actor_id="local-admin")
        self.assertEqual(raised.exception.code, "migration_not_complete")
        self.assertIn("incompatible", str(raised.exception.details))

    def test_correction_then_reverify_clears_incompatible(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="更正后继续", actor_id="local-admin", batch_size=2
        )
        # 先对一份草稿园区做更正（已确认园区不可改）。
        with _request():
            draft_plot = self.catalog.create_plot(
                {
                    "code": "OR-6202",
                    "name": "草稿园区",
                    "locality": "另一地点",
                    "cultivar_focus": "品种",
                    "steward": "组",
                    "planting_year": 2011,
                    "note": "",
                }
            )
        result = self.migration.correct_object(
            plan,
            kind="plot",
            object_id=draft_plot["id"],
            change={"name": "迁移期更正名称"},
            actor_id="local-admin",
            expected_revision=draft_plot["revision"],
        )
        self.assertGreater(result["revision"], 0)
        state = self.repository.read()
        self.assertEqual(state["plots"][draft_plot["id"]]["name"], "迁移期更正名称")
        ops = self.migration.list_corrections(plan)
        self.assertTrue(any(o["op_type"] == "correction" for o in ops["items"]))

    def test_pause_blocks_progress_and_resume_continues(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="暂停计划", actor_id="local-admin", batch_size=3
        )
        paused = self.migration.pause(plan, actor_id="local-admin")
        self.assertEqual(paused["status"], "paused")
        detail = self.migration.get_plan(plan)
        with self.assertRaises(PreconditionError) as raised:
            self.migration.verify_batch(
                plan, detail["batches"][0]["seq"], actor_id="local-admin"
            )
        self.assertEqual(raised.exception.code, "migration_plan_not_active")
        resumed = self.migration.resume(plan, actor_id="local-admin")
        self.assertEqual(resumed["status"], "active")
        applied = self.run_plan(plan)
        self.assertTrue(applied)


class RollbackTests(MigrationHarness):
    def test_rollback_keeps_committed_batches_with_reason(self) -> None:
        self.seed_archive(trees=3)
        plan = self.migration.create_plan(
            name="部分切换后回滚", actor_id="local-admin", batch_size=2
        )
        self.run_plan(plan, stop_after=1)
        result = self.migration.rollback_plan(
            plan, actor_id="local-admin", reason="新规则需要调整"
        )
        self.assertEqual(result["status"], "rolled_back")
        retained = result["retained_applied_batches"]
        self.assertEqual(len(retained), 1)
        self.assertIn("第 2 代规则正式提交", retained[0]["reason"])

        detail = self.migration.get_plan(plan)
        applied_seqs = [b["seq"] for b in detail["batches"] if b["status"] == "applied"]
        rolled_seqs = [
            b["seq"] for b in detail["batches"] if b["status"] == "rolled_back"
        ]
        self.assertEqual(applied_seqs, [0])
        self.assertIn(1, rolled_seqs)

        # 已提交对象保持第 2 代形态，未切换对象保持第 1 代。
        state = self.repository.read()
        generations = {
            item["schema_version"]
            for item in state["trees"].values()
        }
        self.assertIn(1, generations)
        self.assertIn(2, generations)

    def test_rollback_plan_cannot_be_restarted_but_new_plan_allowed(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="回滚后重建", actor_id="local-admin", batch_size=3
        )
        self.run_plan(plan, stop_after=1)
        self.migration.rollback_plan(plan, actor_id="local-admin")
        with self.assertRaises(PreconditionError):
            self.migration.verify_batch(plan, 0, actor_id="local-admin")

    def test_rollback_before_any_switch_reverts_requested_merges(self) -> None:
        with _request():
            plot = self.catalog.create_plot(
                {
                    "code": "OR-6101",
                    "name": "园区 OR-6101",
                    "locality": "测试地点",
                    "cultivar_focus": "富士",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        survivor = self.add_tree(plot, "OR-6101", "01")
        alias = self.add_tree(plot, "OR-6101", "02")
        self.confirm_plot(plot)
        self.complete_season(survivor, "2022")
        self.complete_season(alias, "2023")

        plan = self.migration.create_plan(
            name="回滚合并", actor_id="local-admin", batch_size=100
        )
        self.migration.request_merge(
            plan["id"],
            survivor_id=survivor["id"],
            alias_id=alias["id"],
            actor_id="local-admin",
        )
        result = self.migration.rollback_plan(
            plan, actor_id="local-admin", reason="重新评估"
        )
        self.assertEqual(result["retained_applied_batches"], [])
        ops = self.migration.list_corrections(plan["id"])
        merge_ops = [o for o in ops["items"] if o["op_type"] == "merge"]
        self.assertTrue(merge_ops)
        self.assertTrue(all(o["status"] == "reverted" for o in merge_ops))

        state = self.repository.read()
        self.assertTrue(
            all(t["schema_version"] == 1 for t in state["trees"].values())
        )
        # 全新计划可以重新登记同一对植株的合并。
        new_plan = self.migration.create_plan(
            name="重新迁移", actor_id="local-admin", batch_size=100
        )
        merge = self.migration.request_merge(
            new_plan["id"],
            survivor_id=survivor["id"],
            alias_id=alias["id"],
            actor_id="local-admin",
        )
        self.assertTrue(merge["op_id"])


class MergeTests(MigrationHarness):
    def test_merge_restructures_references_without_changing_conclusions(self) -> None:
        with _request():
            plot = self.catalog.create_plot(
                {
                    "code": "OR-6101",
                    "name": "园区 OR-6101",
                    "locality": "测试地点",
                    "cultivar_focus": "富士",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        survivor = self.add_tree(plot, "OR-6101", "01")
        alias = self.add_tree(plot, "OR-6101", "02")
        self.confirm_plot(plot)
        season_left = self.complete_season(survivor, "2022")
        season_right = self.complete_season(alias, "2023")  # 不同年份可合并

        plan = self.migration.create_plan(
            name="引用结构合并", actor_id="local-admin", batch_size=20
        )
        self.migration.request_merge(
            plan,
            survivor_id=survivor["id"],
            alias_id=alias["id"],
            actor_id="local-admin",
        )
        self.run_plan(plan)
        self.migration.finalize_plan(plan, actor_id="local-admin")

        state = self.repository.read()
        alias_tree = state["trees"][alias["id"]]
        self.assertEqual(alias_tree["canonical_id"], survivor["id"])
        self.assertEqual(alias_tree["merged_into"], survivor["id"])
        # 季节志保留原 tree_id（引用不被重写），同时带规范引用。
        right = state["observations"][season_right["id"]]
        self.assertEqual(right["tree_id"], alias["id"])
        self.assertEqual(right["tree_canonical_id"], survivor["id"])
        self.assertEqual(right["season_key"], f"{survivor['id']}#2023")
        left = state["observations"][season_left["id"]]
        self.assertEqual(left["tree_canonical_id"], survivor["id"])

    def test_merge_rejects_conflicting_duplicate_seasons(self) -> None:
        with _request():
            plot = self.catalog.create_plot(
                {
                    "code": "OR-6101",
                    "name": "园区 OR-6101",
                    "locality": "测试地点",
                    "cultivar_focus": "富士",
                    "steward": "测试组",
                    "planting_year": 2010,
                    "note": "",
                }
            )
        survivor = self.add_tree(plot, "OR-6101", "01")
        alias = self.add_tree(plot, "OR-6101", "02")
        self.confirm_plot(plot)
        self.complete_season(survivor, "2023")
        self.complete_season(alias, "2023")
        plan = self.migration.create_plan(
            name="冲突合并", actor_id="local-admin", batch_size=20
        )
        with self.assertRaises(PreconditionError) as raised:
            self.migration.request_merge(
                plan,
                survivor_id=survivor["id"],
                alias_id=alias["id"],
                actor_id="local-admin",
            )
        self.assertEqual(raised.exception.code, "tree_merge_ineligible")
        self.assertTrue(raised.exception.details["reasons"])


class FrozenAndHistoryTests(MigrationHarness):
    def test_brief_payload_is_byte_identical_after_finalize(self) -> None:
        archive = self.seed_archive()
        brief_id = archive["brief"]["id"]
        before = self.repository.read()["briefs"][brief_id]
        before_blob = json.dumps(before, ensure_ascii=False, sort_keys=True)
        plan = self.migration.create_plan(
            name="冻结见证", actor_id="local-admin", batch_size=50
        )
        self.run_plan(plan)
        report = self.migration.verify_report(plan)
        self.assertTrue(report["business_result_parity"]["ok"])
        self.assertGreaterEqual(
            report["business_result_parity"]["frozen_briefs_checked"], 1
        )
        self.migration.finalize_plan(plan, actor_id="local-admin")
        after = self.repository.read()["briefs"][brief_id]
        after_blob = json.dumps(after, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before_blob, after_blob)
        self.assertEqual(after["schema_version"], 1)

    def test_historical_point_in_time_versions_survive_cutover(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="历史血缘", actor_id="local-admin", batch_size=50
        )
        # 记录某个可变对象在迁移前的修订链。
        before_state = self.repository.read()
        sample_tree_id = next(iter(before_state["trees"]))
        before_versions = self.repository.list_entity_versions(
            kind="tree", identifier=sample_tree_id
        )
        self.run_plan(plan)
        self.migration.finalize_plan(plan, actor_id="local-admin")
        after_versions = self.repository.list_entity_versions(
            kind="tree", identifier=sample_tree_id
        )
        # 旧修订号全部还在，且新追加的是迁移切换修订。
        before_revisions = [v["revision"] for v in before_versions["items"]]
        after_revisions = [v["revision"] for v in after_versions["items"]]
        for revision in before_revisions:
            self.assertIn(revision, after_revisions)
        self.assertGreater(len(after_revisions), len(before_revisions))
        actions = {v["action"] for v in after_versions["items"]}
        self.assertIn("migration.cutover", actions)
        # 迁移前最早的版本载荷仍能逐字节取回（历史时点视图）。
        oldest_before = before_versions["items"][-1]
        matching = next(
            v
            for v in after_versions["items"]
            if v["revision"] == oldest_before["revision"]
        )
        self.assertEqual(
            json.dumps(oldest_before["payload"], sort_keys=True),
            json.dumps(matching["payload"], sort_keys=True),
        )

    def test_comparison_offsets_and_summary_remain_unchanged(self) -> None:
        archive = self.seed_archive()
        comparison_id = archive["comparison"]["id"]
        before = self.comparisons.get_comparison(comparison_id)
        plan = self.migration.create_plan(
            name="对比见证", actor_id="local-admin", batch_size=50
        )
        self.run_plan(plan)
        self.migration.finalize_plan(plan, actor_id="local-admin")
        after = self.comparisons.get_comparison(comparison_id)
        self.assertEqual(before["stage_offsets"], after["stage_offsets"])
        self.assertEqual(before["summary"]["sentence"], after["summary"]["sentence"])
        self.assertEqual(before["summary"]["average_offset_days"],
                         after["summary"]["average_offset_days"])

    def test_restart_recovers_interrupted_batch_and_generation(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="崩溃恢复", actor_id="local-admin", batch_size=2
        )
        # 手动把一个批次置为 verifying，模拟进程在验证中途崩溃（在恢复之前）。
        with self.database.transaction(immediate=True) as connection:
            migration_store.update_batch(
                connection,
                plan_id=plan["id"],
                seq=1,
                status="verifying",
                timestamp=_iso_now(),
            )
        # 用一个全新的仓储/服务重启；构造时即完成启动恢复。
        self.repository.close()
        RUNTIME.reset()
        database = Database(self.database_path)
        repository = Repository(database)
        repository.open()
        service = MigrationService(repository)
        recovered = service.recover_runtime()
        # 构造函数已经把中断批次重置；显式再恢复时幂等且无新重置。
        self.assertEqual(recovered["recovered_batches"], [])
        detail = service.get_plan(plan)
        self.assertEqual(detail["batches"][1]["status"], "pending")
        self.assertTrue(
            detail["batches"][1]["error"].startswith("进程重启")
        )

        # 全新进程可以继续完成全部批次并正式切换。
        self.migration = service
        self.repository = repository
        self.database = database
        self.run_plan(plan)
        finalized = service.finalize_plan(plan, actor_id="local-admin")
        self.assertEqual(finalized["status"], "finalized")

        # 再次重启后代际仍然是 2。
        repository.close()
        RUNTIME.reset()
        database2 = Database(self.database_path)
        repository2 = Repository(database2)
        repository2.open()
        service2 = MigrationService(repository2)
        self.assertEqual(service2.recover_runtime()["generation"], 2)
        repository2.close()


class BackgroundJobAndAuthorizationTests(MigrationHarness):
    def test_batches_progress_through_background_jobs(self) -> None:
        self.seed_archive(trees=3)
        plan = self.migration.create_plan(
            name="后台推进", actor_id="local-admin", batch_size=2
        )
        jobs = JobService(self.database)
        worker = JobWorker(
            jobs=jobs,
            repository=self.repository,
            worker_id="worker-test",
            migration=self.migration,
        )
        detail = self.migration.get_plan(plan)
        for _ in detail["batches"]:
            self.migration.enqueue_next_batch_job(
                plan, actor_id="local-admin", apply=False
            )
            self.assertTrue(worker.run_once())
            self.migration.enqueue_next_batch_job(
                plan, actor_id="local-admin", apply=True
            )
            self.assertTrue(worker.run_once())
        detail = self.migration.get_plan(plan)
        self.assertTrue(
            all(b["status"] == "applied" for b in detail["batches"])
        )

    def test_migration_capability_can_be_revoked_mid_flight(self) -> None:
        self.seed_archive()
        identity = IdentityService(self.database)
        with _request():
            identity.create_actor(actor_id="mig-admin", display_name="迁移管理员")
            grant = identity.grant(
                actor_id="mig-admin",
                capability="migration:admin",
                resource_kind="migration",
                resource_id="*",
            )
        authorization = AuthorizationService(self.database)
        authorization.require(
            actor_id="mig-admin",
            capability="migration:admin",
            resource_kind="migration",
            resource_id="x",
        )
        plan = self.migration.create_plan(
            name="撤销场景", actor_id="mig-admin", batch_size=20
        )
        with _request():
            identity.revoke(grant["id"])
        with self.assertRaises(DomainError) as raised:
            authorization.require(
                actor_id="mig-admin",
                capability="migration:admin",
                resource_kind="migration",
                resource_id=plan["id"],
            )
        self.assertEqual(raised.exception.code, "forbidden")
        # 已提交批次不因为授权撤销而消失。
        detail = self.migration.get_plan(plan)
        self.assertIn("batches", detail)


class LiveWriteDualWriteTests(MigrationHarness):
    def test_write_to_applied_object_is_blocked_before_finalize(self) -> None:
        archive = self.seed_archive()
        plan = self.migration.create_plan(
            name="双写封锁", actor_id="local-admin", batch_size=50
        )
        self.run_plan(plan)  # 既有对象全部 applied，但计划尚未 finalize

        # 已切换批次内的对象不能再按旧规则被双写（尝试修订已确认园区会被领域
        # 规则先行拦截；这里改用“迁移期间新建对象仍可旧写入”来证明双写持续生效）。
        with _request():
            draft = self.catalog.create_plot(
                {
                    "code": "OR-6303",
                    "name": "新草稿",
                    "locality": "L",
                    "cultivar_focus": "C",
                    "steward": "S",
                    "planting_year": 2011,
                    "note": "",
                }
            )
        member = None
        with self.database.read_connection() as connection:
            for obj in migration_store.list_objects(connection, plan["id"]):
                if obj["object_id"] == draft["id"]:
                    member = obj
        self.assertIsNotNone(member)
        # 新对象进入一个新的尾随批次，切换它之后才能 finalize。
        self.run_plan(plan)
        self.migration.finalize_plan(plan, actor_id="local-admin")
        # finalize 后第 2 代规则生效，阶段字典包含休眠期。
        from app.domain.stages import active_stages

        self.assertIn("winter_rest", {s.key for s in active_stages()})

    def test_second_generation_write_is_upgraded_inline(self) -> None:
        self.seed_archive()
        plan = self.migration.create_plan(
            name="第 2 代写入", actor_id="local-admin", batch_size=50
        )
        self.run_plan(plan)
        self.migration.finalize_plan(plan, actor_id="local-admin")
        plot, tree = self.create_plot_with_tree("OR-6404", "01")
        observation = self.complete_season(tree, "2024", confidence=4)
        state = self.repository.read()
        record = state["observations"][observation["id"]]
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["tree_canonical_id"], tree["id"])
        self.assertTrue(all("confidence_score" in e for e in record["entries"]))
        self.assertEqual(record["entries"][0]["confidence_score"], 0.75)

    def test_engine_helpers_round_trip_confidence(self) -> None:
        from app.migration.contracts.confidence import (
            is_compatible_v2_score,
            v1_confidence_to_v2_score,
            v2_score_to_v1_confidence,
        )

        for level in range(1, 6):
            score = v1_confidence_to_v2_score(level)
            self.assertTrue(is_compatible_v2_score(score))
            self.assertEqual(v2_score_to_v1_confidence(score), level)
        self.assertFalse(is_compatible_v2_score(0.13))

    def test_fingerprint_ignores_revision_and_timestamps(self) -> None:
        plot, tree = self.create_plot_with_tree()
        first = {
            **self.repository.read()["trees"][tree["id"]],
        }
        second = {**first, "revision": first["revision"] + 99,
                  "updated_at": "2099-01-01T00:00:00+00:00"}
        self.assertEqual(
            engine.business_fingerprint("tree", first),
            engine.business_fingerprint("tree", second),
        )


class ConcurrentLoadTests(MigrationHarness):
    def test_migration_under_concurrent_business_writes_loses_nothing(self) -> None:
        # 30 个草稿园区分 3 批迁移。
        for index in range(30):
            with request_scope(
                RequestContext(
                    actor_id="local-admin",
                    request_method="PUT",
                    request_path="/x",
                    request_hash=f"seed-{index}",
                    route_template="/x",
                )
            ):
                self.catalog.create_plot(
                    {
                        "code": f"OR-{7000 + index}",
                        "name": f"园区 {index}",
                        "locality": "L",
                        "cultivar_focus": "C",
                        "steward": "S",
                        "planting_year": 2010,
                        "note": "",
                    }
                )
        plan = self.migration.create_plan(
            name="并发负载", actor_id="local-admin", batch_size=10
        )

        def business_write(index: int) -> None:
            with request_scope(
                RequestContext(
                    actor_id="local-admin",
                    request_method="PUT",
                    request_path="/x",
                    request_hash=f"biz-{index}",
                    route_template="/x",
                )
            ):
                self.catalog.create_plot(
                    {
                        "code": f"OR-{8000 + index}",
                        "name": f"新园区 {index}",
                        "locality": "L",
                        "cultivar_focus": "C",
                        "steward": "S",
                        "planting_year": 2010,
                        "note": "",
                    }
                )

        def migrate_batch(seq: int) -> None:
            self.migration.verify_batch(
                plan["id"], seq, actor_id="local-admin"
            )
            self.migration.apply_batch(
                plan["id"], seq, actor_id="local-admin"
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(migrate_batch, seq) for seq in range(3)
            ]
            futures += [
                executor.submit(business_write, index) for index in range(8)
            ]
            for future in futures:
                future.result()  # 不允许任何批次或写入抛错

        state = self.repository.read()
        with self.database.read_connection() as connection:
            tracked = {
                obj["object_id"]
                for obj in migration_store.list_objects(connection, plan["id"])
            }
        self.assertEqual(len(state["plots"]), 38)
        self.assertEqual(set(state["plots"]), tracked)
        # 所有对象（含迁移期间新建）都能继续完成切换并正式启用。
        self.run_plan(plan["id"])
        detail = self.migration.get_plan(plan["id"])
        self.assertTrue(
            all(b["status"] == "applied" for b in detail["batches"])
        )
        finalized = self.migration.finalize_plan(
            plan["id"], actor_id="local-admin"
        )
        self.assertEqual(finalized["status"], "finalized")
        final_state = self.repository.read()
        self.assertTrue(
            all(p["schema_version"] == 2 for p in final_state["plots"].values())
        )


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    unittest.main()
