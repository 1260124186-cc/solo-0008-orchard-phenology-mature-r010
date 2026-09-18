"""生产式安全迁移框架的端到端测试。

覆盖：
- 新旧规则双读与字段精度、阶段、引用、权限四类演进；
- 分批切换、单批失败隔离、失败项与不兼容集合、批次检查点；
- 生产负载下常规写入与新规则语义保持一致（实时护栏）；
- 停止/回滚未切换批次，已提交的新规则事实保留并解释；
- 迁移期更正、合并、授权撤销；
- 冻结分析、历史时点视图、启动恢复不因模式切换改变；
- 每批同时比较业务结果、版本血缘、审计与 outbox。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application import (
    BriefService,
    CatalogService,
    ComparisonService,
    ObservationService,
)
from app.domain.comparison_rules import calculate_stage_offsets
from app.domain.plot_rules import new_identifier, now_iso
from app.errors import DomainError
from app.jobs import JobService, JobWorker
from app.migration import MigrationService
from app.migration.dual_read import DualReadEngine
from app.migration.grants import (
    GrantRecord,
    revocation_targets,
    scope_matches,
    verify_grant_equivalence,
)
from app.migration.live_guard import LiveWriteGuard
from app.migration.phenology_v1 import V1_RULESET
from app.migration.phenology_v2 import V2_RULESET
from app.migration.recovery import MigrationRecovery
from app.migration.verifier import MigrationVerifier
from app.persistence import Database, Repository
from app.security import RequestContext, request_scope
from app.security.management import IdentityService


REQUIRED_STAGES = [
    ("bud_burst", "2024-03-01"),
    ("full_bloom", "2024-04-01"),
    ("fruit_set", "2024-05-01"),
    ("harvest", "2024-09-01"),
]


def _scope(key: str, *, actor: str = "local-admin"):
    return request_scope(
        RequestContext(
            actor_id=actor,
            idempotency_key=key,
            request_method="PUT",
            request_path="/api/test",
            request_hash=key,
            route_template="/api/test",
        )
    )


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.repository = Repository(
            Database(self.data_dir / "atlas.sqlite3")
        )
        self.repository.open()
        self.repository.live_write_guard = LiveWriteGuard(self.repository)
        self.catalog = CatalogService(self.repository)
        self.observations = ObservationService(self.repository)
        self.comparisons = ComparisonService(self.repository)
        self.briefs = BriefService(self.repository)
        self.service = MigrationService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    # ------------------------------------------------------------------
    def make_plot(self, code: str = "OR-3001"):
        with _scope(f"plot-{code}"):
            return self.catalog.create_plot(
                {
                    "code": code,
                    "name": f"园 {code}",
                    "locality": "本地",
                    "cultivar_focus": "地方品种",
                    "steward": "档案员",
                    "planting_year": 2008,
                    "note": "",
                }
            )

    def make_tree(self, plot_id: str, code: str, *, suffix: str = "01"):
        with _scope(f"tree-{code}-{suffix}"):
            return self.catalog.create_tree(
                {
                    "plot_id": plot_id,
                    "code": f"{code}-T{suffix}",
                    "cultivar": "地方品种",
                    "rootstock": "本砧",
                    "planting_year": 2010,
                    "status": "active",
                    "note": "",
                }
            )

    def confirm_plot(self, plot):
        with _scope(f"confirm-{plot['id']}"):
            self.catalog.confirm_plot(
                plot["id"], expected_revision=plot["revision"]
            )

    def make_completed_season(
        self,
        tree,
        *,
        season: str = "2024",
        confidence: int = 4,
        observer: str = "观察员甲",
        key: str = "",
    ):
        with _scope(f"season-{key or tree['id']}"):
            record = self.observations.start_observation(
                {
                    "tree_id": tree["id"],
                    "season": season,
                    "observer": observer,
                    "note": "",
                }
            )
        revision = record["revision"]
        observation_id = record["id"]
        for stage, observed_on in REQUIRED_STAGES:
            with _scope(f"stage-{key or tree['id']}-{stage}"):
                updated = self.observations.add_stage(
                    observation_id,
                    {
                        "stage": stage,
                        "observed_on": observed_on,
                        "confidence": confidence,
                        "note": "",
                        "revision": revision,
                    },
                )
            revision = updated["revision"]
        with _scope(f"done-{key or tree['id']}"):
            self.observations.complete_observation(
                observation_id, {"revision": revision}
            )
        return self.observations.get_observation(observation_id)

    def completed_observation_payload(self, observation_id: str):
        return self.repository.read()["observations"][observation_id]

    def plan(self, *, batch_size: int = 20):
        with _scope("migration-plan"):
            return self.service.create_plan(
                actor_id="local-admin",
                batch_size=batch_size,
            )

    def run_all_batches(self, *, cap: int = 100):
        for _ in range(cap):
            with _scope(f"migration-batch-{_}"):
                status = self.service.run_next_batch(actor_id="local-admin")
            if status["run"]["status"] != "running":
                break
        return self.service.ledger.active_run()

    def complete(self):
        with _scope("migration-complete"):
            return self.service.complete(actor_id="local-admin")


class DualReadSemanticsTests(MigrationTestBase):
    def test_integer_confidence_equivalent_to_v2_decimal(self) -> None:
        engine = DualReadEngine()
        observation = self._observation_payload(confidence=4)
        read = engine.read_object("observation", observation)
        self.assertTrue(read.business_equal, read.reasons)
        upgraded = read.upgraded_payload
        self.assertEqual(
            upgraded["entries"][0]["confidence"], 4.0
        )
        # v1 4 与 v2 4.0 在统一刻度下结论相同。
        old_conclusion = engine.fingerprint_object(
            "observation", observation, ruleset=V1_RULESET
        )
        new_conclusion = engine.fingerprint_object(
            "observation", upgraded, ruleset=V2_RULESET
        )
        self.assertEqual(old_conclusion, new_conclusion)

    def test_half_step_confidence_is_v2_only_and_blocks_roundtrip(self) -> None:
        engine = DualReadEngine()
        observation = self._observation_payload(confidence=4.5)
        equal, reasons = engine.round_trip_equal("observation", observation)
        self.assertFalse(equal)
        self.assertTrue(any("半级" in item for item in reasons))

    def test_dormant_bud_keeps_required_set_and_order(self) -> None:
        # 休眠芽阶段在 v2 目录中且不是完成必需项。
        self.assertEqual(V2_RULESET.catalog.rank_of("dormant_bud"), 55)
        self.assertNotIn("dormant_bud", V2_RULESET.catalog.required)
        self.assertEqual(
            set(V1_RULESET.catalog.required),
            set(V2_RULESET.catalog.required),
        )
        # 原九阶段相对顺序不变。
        old_keys = [
            key
            for key, _rank in sorted(
                V1_RULESET.catalog.order.items(), key=lambda item: item[1]
            )
        ]
        new_keys = [
            key
            for key in V2_RULESET.catalog.order
            if key != "dormant_bud"
        ]
        self.assertEqual(old_keys, new_keys)

    def test_comparison_offset_unchanged_under_precision_change(self) -> None:
        plot = self.make_plot()
        tree_a = self.make_tree(plot["id"], plot["code"], suffix="01")
        tree_b = self.make_tree(plot["id"], plot["code"], suffix="02")
        self.confirm_plot(plot)
        self.make_completed_season(tree_a, key="a")
        self.make_completed_season(tree_b, confidence=3, key="b")
        state = self.repository.read()
        left = next(iter(state["observations"].values()))
        with _scope("comparison"):
            comparison = self.comparisons.create_comparison(
                {
                    "title": "对齐",
                    "left_observation_id": self._observation_id_for_tree(
                        tree_a["id"]
                    ),
                    "right_observation_id": self._observation_id_for_tree(
                        tree_b["id"]
                    ),
                }
            )
        engine = DualReadEngine()
        record = self.repository.read()["comparisons"][comparison["id"]]
        read = engine.read_object("comparison", record)
        self.assertTrue(read.business_equal, read.reasons)

    def _observation_id_for_tree(self, tree_id: str) -> str:
        for item in self.repository.read()["observations"].values():
            if item["tree_id"] == tree_id:
                return item["id"]
        raise AssertionError("missing observation")

    def _observation_payload(self, *, confidence):
        return {
            "id": new_identifier("season"),
            "schema_version": 1,
            "tree_id": "tree_x",
            "plot_id": "plot_x",
            "season": "2024",
            "observer": "甲",
            "note": "",
            "status": "open",
            "entries": [
                {
                    "id": new_identifier("entry"),
                    "stage": "bud_burst",
                    "observed_on": "2024-03-01",
                    "confidence": confidence,
                    "note": "",
                    "created_at": now_iso(),
                }
            ],
            "revision": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "completed_at": None,
        }


class BatchMigrationTests(MigrationTestBase):
    def test_plan_records_incompatible_and_batches(self) -> None:
        plot = self.make_plot()
        self.make_tree(plot["id"], plot["code"])
        plan = self.plan(batch_size=1)
        self.assertIn(plan["run"]["status"], {"shadow"})
        # plot + tree 两个对象，批大小为 1 => 2 个批次。
        self.assertEqual(len(plan["batches"]), 2)
        self.assertEqual(plan["run"]["total_objects"], 2)

    def test_each_batch_checkpoint_compares_all_four_dimensions(self) -> None:
        plot = self.make_plot()
        tree = self.make_tree(plot["id"], plot["code"])
        self.confirm_plot(plot)
        season = self.make_completed_season(tree)
        with _scope("brief"):
            brief = self.briefs.create_brief(plot["id"], {"title": "编研简报"})
        plan = self.plan(batch_size=2)
        run_id = plan["run"]["run_id"]
        self.run_all_batches()
        batches = self.service.ledger.list_batches(run_id)["items"]
        succeeded = [item for item in batches if item["status"] == "succeeded"]
        self.assertTrue(succeeded)
        for batch in succeeded:
            checkpoint = batch["checkpoint"]
            self.assertTrue(checkpoint["after"]["equal"])
            self.assertEqual(checkpoint["audit"]["problems"], [])
            self.assertEqual(checkpoint["outbox"]["problems"], [])
            self.assertEqual(checkpoint["lineage_problems"], [])
        # 业务实体都已切换为 v2 形态。
        state = self.repository.read()
        self.assertEqual(
            state["observations"][season["id"]]["schema_version"], 2
        )
        self.assertEqual(state["briefs"][brief["id"]]["schema_version"], 2)

    def test_failed_batch_is_isolated_and_can_be_retried(self) -> None:
        plot = self.make_plot()
        for suffix in ("01", "02", "03", "04", "05"):
            self.make_tree(plot["id"], plot["code"], suffix=suffix)
        self.plan(batch_size=2)
        run_id = self.service.ledger.active_run()["run_id"]

        original = self.service._switch_object
        counter = {"n": 0}

        def flaky(run_id, kind, object_id, *, actor_id):
            counter["n"] += 1
            if counter["n"] in (3, 4):  # 第 2 批注入失败
                raise RuntimeError("注入的批次失败")
            return original(run_id, kind, object_id, actor_id=actor_id)

        self.service._switch_object = flaky
        self.run_all_batches()
        self.assertEqual(self.service.ledger.get_run(run_id)["status"], "blocked")
        batch_states = {
            item["batch_no"]: item["status"]
            for item in self.service.ledger.list_batches(run_id)["items"]
        }
        self.assertEqual(batch_states[1], "succeeded")
        self.assertEqual(batch_states[2], "failed")
        self.assertEqual(batch_states[3], "planned")
        # 已成功批次结果不被失败影响。
        switched_batches = {
            item["batch_no"]
            for item in self.service.ledger.list_objects(
                run_id, status="switched"
            )["items"]
        }
        self.assertEqual(switched_batches, {1})

        # 恢复真实切换并重试失败批次。
        self.service._switch_object = original
        with _scope("retry"):
            status = self.service.retry_failed_batch(
                batch_no=2, actor_id="local-admin"
            )
        self.run_all_batches()
        with _scope("complete"):
            completed = self.service.complete(actor_id="local-admin")
        self.assertEqual(completed["run"]["status"], "completed")

    def test_stop_prevents_further_switches_but_keeps_committed(self) -> None:
        plot = self.make_plot()
        self.make_tree(plot["id"], plot["code"])
        self.plan(batch_size=1)
        run_id = self.service.ledger.active_run()["run_id"]
        with _scope("first-batch"):
            self.service.run_next_batch(actor_id="local-admin")
        committed = self.service.ledger.counts_by_status(run_id).get(
            "switched", 0
        )
        with _scope("stop"):
            stopped = self.service.request_stop(actor_id="local-admin")
        self.assertEqual(stopped["run"]["status"], "paused")
        with self.assertRaises(DomainError):
            with _scope("next-after-stop"):
                self.service.run_next_batch(actor_id="local-admin")
        self.assertEqual(
            self.service.ledger.counts_by_status(run_id).get("switched", 0),
            committed,
        )


class LiveWriteGuardTests(MigrationTestBase):
    def test_normal_live_write_tracks_new_object(self) -> None:
        plot = self.make_plot()
        plan = self.plan(batch_size=10)
        run_id = plan["run"]["run_id"]
        tree = self.make_tree(plot["id"], plot["code"], suffix="99")
        tracked = self.service.ledger.get_object(run_id, "tree", tree["id"])
        self.assertIsNotNone(tracked)
        self.assertEqual(tracked["status"], "ready")

    def test_half_step_write_is_blocked(self) -> None:
        plot = self.make_plot()
        tree = self.make_tree(plot["id"], plot["code"])
        self.plan(batch_size=10)

        def inject(state):
            record = {
                "id": new_identifier("season"),
                "schema_version": 1,
                "tree_id": tree["id"],
                "plot_id": plot["id"],
                "season": "2025",
                "observer": "甲",
                "note": "",
                "status": "open",
                "entries": [
                    {
                        "id": new_identifier("entry"),
                        "stage": "bud_burst",
                        "observed_on": "2025-03-01",
                        "confidence": 4.5,
                        "note": "",
                        "created_at": now_iso(),
                    }
                ],
                "revision": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "completed_at": None,
            }
            state["observations"][record["id"]] = record
            return record

        with _scope("half-step"):
            with self.assertRaises(DomainError) as raised:
                self.repository.atomic_update(inject)
        self.assertEqual(
            raised.exception.code, "migration_write_semantics_diverge"
        )
        # 被阻断的写入没有落地。
        self.assertNotIn("2025", {
            item["season"]
            for item in self.repository.read()["observations"].values()
        })

    def test_new_stage_write_is_blocked_under_old_api(self) -> None:
        plot = self.make_plot()
        tree = self.make_tree(plot["id"], plot["code"])
        self.plan(batch_size=10)

        def inject(state):
            record = {
                "id": new_identifier("season"),
                "schema_version": 1,
                "tree_id": tree["id"],
                "plot_id": plot["id"],
                "season": "2026",
                "observer": "甲",
                "note": "",
                "status": "open",
                "entries": [
                    {
                        "id": new_identifier("entry"),
                        "stage": "dormant_bud",
                        "observed_on": "2026-06-01",
                        "confidence": 4,
                        "note": "",
                        "created_at": now_iso(),
                    }
                ],
                "revision": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "completed_at": None,
            }
            state["observations"][record["id"]] = record
            return record

        with _scope("dormant"):
            with self.assertRaises(DomainError):
                self.repository.atomic_update(inject)


class RollbackTests(MigrationTestBase):
    def test_rollback_reverts_uncommitted_and_retains_new_facts(self) -> None:
        plot = self.make_plot()
        tree_a = self.make_tree(plot["id"], plot["code"], suffix="01")
        tree_b = self.make_tree(plot["id"], plot["code"], suffix="02")
        self.plan(batch_size=1)
        run_id = self.service.ledger.active_run()["run_id"]
        with _scope("merge"):
            merge = self.service.merge_trees(
                actor_id="local-admin",
                canonical_tree_id=tree_a["id"],
                member_tree_ids=[tree_b["id"]],
            )
        self.assertIn("canonical_tree_reference", merge["new_rule_facts"])
        self.run_all_batches()
        switched_before = self.service.ledger.counts_by_status(run_id).get(
            "switched", 0
        )
        with _scope("rollback"):
            result = self.service.rollback(actor_id="local-admin")
        self.assertEqual(result["run"]["status"], "rolled_back")
        # 规范树保留，其余已切换对象回退为 v1。
        retained_ids = {item["id"] for item in result["retained"]}
        self.assertIn(tree_a["id"], retained_ids)
        state = self.repository.read()
        self.assertEqual(
            state["trees"][tree_a["id"]].get("merged_from"),
            [tree_b["id"]],
        )
        # 普通对象恢复为旧 schema_version。
        self.assertEqual(state["plots"][plot["id"]]["schema_version"], 1)
        # 保留集合记录了原因。
        retained = self.service.ledger.list_retained(run_id)
        self.assertTrue(
            all(item.get("reason") for item in retained)
        )
        self.assertGreaterEqual(switched_before, 1)


class InterventionTests(MigrationTestBase):
    def test_correction_revalidates_object(self) -> None:
        plot = self.make_plot()
        tree = self.make_tree(plot["id"], plot["code"])
        plan = self.plan(batch_size=10)
        run_id = plan["run"]["run_id"]
        # 业务方在迁移期间通过常规接口更正植株备注。
        with _scope("live-tree-note"):
            self.catalog.retire_tree(
                tree["id"],
                {
                    "status": "retired",
                    "note": "迁移期更正",
                    "revision": tree["revision"],
                },
            )
        with _scope("correction"):
            corrected = self.service.record_correction(
                actor_id="local-admin",
                kind="tree",
                object_id=tree["id"],
                note="现场更正",
            )
        self.assertIn(corrected["status"], {"ready", "incompatible"})
        events = self.service.ledger.list_events(run_id)
        self.assertTrue(
            any(item["event_type"] == "migration.correction" for item in events["items"])
        )

    def test_grant_revocation_covers_equivalent_scopes(self) -> None:
        identity = IdentityService(self.repository.database)
        identity.create_actor(
            actor_id="observer-z", display_name="观察员子"
        )
        flat = identity.grant(
            actor_id="observer-z",
            capability="plot:read",
            resource_kind="plot",
            resource_id="plot-1",
        )
        # v2 层级表示指向同一具体植株资源（叶子段 plot-1 一致）。
        identity.grant(
            actor_id="observer-z",
            capability="plot:read",
            resource_kind="plot",
            resource_id="plot/plot-1/tree/plot-1",
        )
        self.make_plot()
        self.plan(batch_size=10)
        with _scope("revoke"):
            outcome = self.service.revoke_grant(
                actor_id="local-admin",
                grant_id=flat["id"],
            )
        # 新旧两种表示的等价授权必须同时撤销。
        revoked_ids = {item["id"] for item in outcome["revoked_grants"]}
        self.assertGreaterEqual(len(revoked_ids), 2)
        grants = identity.list_grants(actor_id="observer-z")["items"]
        self.assertTrue(all(item["revoked_at"] is not None for item in grants))

    def test_hierarchical_scope_does_not_widen_legacy_decisions(self) -> None:
        # 一条旧的扁平授权：v2 评估器必须给出与 v1 完全相同的决定，
        # 不能因为出现层级语法就把它放大到子资源。
        legacy_grants = [
            GrantRecord(
                actor_id="a",
                capability="plot:read",
                resource_kind="plot",
                resource_id="plot-1",
                hierarchical=False,
            )
        ]
        probes = [
            {
                "actor_active": True,
                "capability": "plot:read",
                "resource_kind": "plot",
                "resource_id": target,
            }
            for target in ("plot-1", "plot/plot-1/tree/t1", "plot-2", "plot")
        ]
        self.assertEqual(verify_grant_equivalence(legacy_grants, probes), [])

        # 显式 v2 层级授权才允许前缀覆盖。
        hierarchical_grants = [
            GrantRecord(
                actor_id="a",
                capability="plot:read",
                resource_kind="plot",
                resource_id="plot/p1",
                hierarchical=True,
            )
        ]
        self.assertTrue(
            scope_matches(
                "plot/p1", "plot/p1/tree/t1", hierarchical=True
            )
        )
        self.assertFalse(
            scope_matches(
                "plot/p1/tree/t1", "plot/p1", hierarchical=True
            )
        )
        # 但同样的决定对旧的扁平授权不成立（不放大）。
        self.assertFalse(
            scope_matches("plot-1", "plot/plot-1/tree/t1", hierarchical=False)
        )
        self.assertEqual(
            verify_grant_equivalence(hierarchical_grants, probes), []
        )

    def test_merge_rejects_conflicting_seasons(self) -> None:
        plot = self.make_plot()
        tree_a = self.make_tree(plot["id"], plot["code"], suffix="01")
        tree_b = self.make_tree(plot["id"], plot["code"], suffix="02")
        self.confirm_plot(plot)
        self.make_completed_season(tree_a, season="2024", key="a")
        self.make_completed_season(tree_b, season="2024", key="b")
        self.plan(batch_size=10)
        with _scope("bad-merge"):
            with self.assertRaises(DomainError):
                self.service.merge_trees(
                    actor_id="local-admin",
                    canonical_tree_id=tree_a["id"],
                    member_tree_ids=[tree_b["id"]],
                )


class FrozenAndHistoryTests(MigrationTestBase):
    def test_brief_and_comparison_conclusions_survive_switch(self) -> None:
        plot = self.make_plot()
        tree_a = self.make_tree(plot["id"], plot["code"], suffix="01")
        tree_b = self.make_tree(plot["id"], plot["code"], suffix="02")
        self.confirm_plot(plot)
        self.make_completed_season(tree_a, key="a")
        self.make_completed_season(tree_b, confidence=3, key="b")
        state = self.repository.read()
        observations = list(state["observations"].values())
        left_id = next(
            item["id"] for item in observations if item["tree_id"] == tree_a["id"]
        )
        right_id = next(
            item["id"] for item in observations if item["tree_id"] == tree_b["id"]
        )
        with _scope("comparison"):
            comparison = self.comparisons.create_comparison(
                {
                    "title": "对齐",
                    "left_observation_id": left_id,
                    "right_observation_id": right_id,
                }
            )
        with _scope("brief"):
            brief = self.briefs.create_brief(plot["id"], {"title": "简报"})

        # 冻结前记录业务结论。
        before_comparison = self.comparisons.get_comparison(comparison["id"])
        before_brief = self.briefs.get_brief(brief["id"])

        self.plan(batch_size=10)
        self.run_all_batches()
        self.complete()

        after_comparison = self.comparisons.get_comparison(comparison["id"])
        after_brief = self.briefs.get_brief(brief["id"])
        self.assertEqual(
            [
                (item["stage"], item["offset_days"])
                for item in before_comparison["stage_offsets"]
            ],
            [
                (item["stage"], item["offset_days"])
                for item in after_comparison["stage_offsets"]
            ],
        )
        self.assertEqual(
            before_brief["payload"]["plot"]["code"],
            after_brief["payload"]["plot"]["code"],
        )
        self.assertEqual(
            before_brief["observation_count"],
            after_brief["observation_count"],
        )

    def test_point_in_time_history_unchanged_across_switch(self) -> None:
        plot = self.make_plot()
        tree = self.make_tree(plot["id"], plot["code"])
        self.confirm_plot(plot)
        season = self.make_completed_season(tree)
        self.plan(batch_size=1)
        run_id = self.service.ledger.active_run()["run_id"]
        self.run_all_batches()
        self.complete()
        with self.repository.database.read_connection() as connection:
            result = MigrationVerifier(
                self.repository.database
            ).point_in_time_check(
                connection,
                kind="observation",
                object_id=season["id"],
                revisions=[1, 2, 3, 4, 5],
                reference_map=self.service.ledger.get_reference_map(run_id),
            )
        self.assertTrue(result["all_equal"])
        self.assertTrue(all(point["ok"] for point in result["points"]))


class StartupRecoveryTests(MigrationTestBase):
    def test_recovery_resets_running_batch_and_reconciles(self) -> None:
        plot = self.make_plot()
        self.make_tree(plot["id"], plot["code"])
        plan = self.plan(batch_size=1)
        run_id = plan["run"]["run_id"]
        # 模拟崩溃：把批次与对象置为中间态。
        self.service.ledger.start_batch(run_id, 1)
        self.service.ledger.upsert_object(
            run_id,
            kind="plot",
            object_id=plot["id"],
            status="switching",
            batch_no=1,
        )
        report = MigrationRecovery(self.repository).recover()
        self.assertIsNotNone(report)
        self.assertIn(1, report["reset_batches"])
        batches = {
            item["batch_no"]: item["status"]
            for item in self.service.ledger.list_batches(run_id)["items"]
        }
        self.assertEqual(batches[1], "planned")
        self.assertEqual(
            self.service.ledger.get_object(run_id, "plot", plot["id"])["status"],
            "ready",
        )
        # 恢复后仍可继续推进。
        self.run_all_batches()
        self.complete()


class BackgroundJobTests(MigrationTestBase):
    def test_migration_batch_runs_as_background_job(self) -> None:
        plot = self.make_plot()
        self.make_tree(plot["id"], plot["code"])
        self.plan(batch_size=1)
        run_id = self.service.ledger.active_run()["run_id"]
        jobs = JobService(self.repository.database)
        worker = JobWorker(
            jobs=jobs,
            repository=self.repository,
            worker_id="test-worker",
        )
        total_batches = len(self.service.ledger.list_batches(run_id)["items"])
        for _ in range(total_batches):
            jobs.enqueue(
                actor_id="local-admin",
                job_type="migration_run_batch",
                payload={"run_id": run_id, "actor_id": "local-admin"},
            )
            self.assertTrue(worker.run_once())
        self.complete()
        self.assertEqual(
            self.service.ledger.get_run(run_id)["status"], "completed"
        )


if __name__ == "__main__":
    unittest.main()
