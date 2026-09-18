"""迁移运行时：领域代码与仓储共享的“当前世代”状态与双写钩子。

- 切换期间领域规则通过本模块读取生效世代（1 或 2）、合并后的规范植株映射，
  以及 v2 阶段字典，而不直接依赖迁移服务，避免循环导入。
- 仓储在每个写事务内调用 ``DualWriteHook``，保证新旧写入都会在同一事务里
  产出对方世代的投影并校验业务结论一致。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol

from .contracts.stages_v2 import V2_STAGES, V2_STAGE_BY_KEY


GENERATION_META_KEY = "domain_generation"


class RuntimeState:
    """进程内的生效世代与规范植株映射，启动时从数据库恢复。"""

    def __init__(self) -> None:
        self.generation = 1
        self.finalized_plan_id: str | None = None
        self.active_plan_id: str | None = None
        self.canonical_tree: dict[str, str] = {}

    def reset(self) -> None:
        self.generation = 1
        self.finalized_plan_id = None
        self.active_plan_id = None
        self.canonical_tree = {}

    def stage_items(self) -> tuple[tuple[str, str, int, bool], ...]:
        if self.generation >= 2:
            return tuple(
                (s.key, s.label, s.rank, s.required_for_completion)
                for s in V2_STAGES
            )
        from ..domain.stages import STAGES

        return tuple(
            (s.key, s.label, s.rank, s.required_for_completion) for s in STAGES
        )

    def stage_label(self, key: str) -> str | None:
        if key in V2_STAGE_BY_KEY:
            return V2_STAGE_BY_KEY[key].label
        from ..domain.stages import STAGE_BY_KEY

        stage = STAGE_BY_KEY.get(key)
        return stage.label if stage else None

    def canonical_id_for_tree(self, tree_id: str) -> str:
        return self.canonical_tree.get(tree_id, tree_id)


RUNTIME = RuntimeState()


class DualWriteHook(Protocol):
    """仓储在写事务中调用的钩子协议（由迁移服务实现并装配）。"""

    def before_commit(
        self,
        connection: sqlite3.Connection,
        *,
        changes: list[tuple[str, str, dict[str, Any], str]],
        next_revision: int,
    ) -> None:
        """在业务实体、版本、审计、outbox 已写入后、提交前调用。"""
