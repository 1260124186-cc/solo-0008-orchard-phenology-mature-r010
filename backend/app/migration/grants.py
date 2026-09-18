"""授权范围的双读验证。

v2 引入层级资源范围（``plot/<id>/tree/<id>``），但对任何旧授权，
v2 评估器必须给出与 v1 完全一致的允许/拒绝决定。迁移期间撤销授权时，
新旧两套范围记录一起失效，避免“换了语义后授权扩大或缩小”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GrantRecord:
    actor_id: str
    capability: str
    resource_kind: str
    resource_id: str
    revoked: bool = False
    expired: bool = False
    # True 表示这是显式登记的 v2 层级范围（plot/p1/tree/t1）。
    # 旧授权（扁平 ID 或 *）始终按 v1 口径精确匹配，绝不被层级规则放大，
    # 这是“旧授权决定在新规则下保持不变”的关键。
    hierarchical: bool = False


def scope_segments(resource_id: str) -> list[str]:
    """把层级范围拆成段。旧的 ``*`` 与扁平 ID 是单段。"""

    return [segment for segment in resource_id.split("/") if segment]


def is_hierarchical_scope(resource_id: str) -> bool:
    """v2 层级范围形如 ``plot/<id>/tree/<id>``（至少 4 段、kind 成对）。"""

    segments = scope_segments(resource_id)
    return len(segments) >= 4 and len(segments) % 2 == 0


def scope_matches(
    granted_scope: str,
    requested_scope: str,
    *,
    hierarchical: bool = False,
) -> bool:
    """范围匹配。

    - 通配 ``*`` 覆盖一切（v1/v2 一致）；
    - 扁平授权只做相等匹配，绝不因为新层级语法而放大或缩小；
    - 只有显式 v2 层级授权才按“严格前缀”覆盖更具体的子资源。
    """

    if granted_scope == "*":
        return True
    if granted_scope == requested_scope:
        return True
    if not hierarchical:
        return False
    granted = scope_segments(granted_scope)
    requested = scope_segments(requested_scope)
    if not granted or not requested:
        return False
    if len(granted) >= len(requested):
        return False
    return requested[: len(granted)] == granted


def decide(
    grants: list[GrantRecord],
    *,
    actor_active: bool,
    capability: str,
    resource_kind: str,
    resource_id: str,
) -> bool:
    """v2 评估决定。对旧授权而言它必须与现网通配/扁平匹配完全等价。"""

    if not actor_active:
        return False
    scope = resource_id or "*"
    for grant in grants:
        if grant.revoked or grant.expired:
            continue
        if grant.capability not in ("*", capability):
            continue
        if grant.resource_kind not in ("*", resource_kind):
            continue
        if scope_matches(
            grant.resource_id,
            scope,
            hierarchical=grant.hierarchical,
        ):
            return True
    return False


def legacy_decide(
    grants: list[GrantRecord],
    *,
    actor_active: bool,
    capability: str,
    resource_kind: str,
    resource_id: str,
) -> bool:
    """复刻 security.service 中现网的 v1 匹配口径。"""

    if not actor_active:
        return False
    scope = resource_id or "*"
    for grant in grants:
        if grant.revoked or grant.expired:
            continue
        if grant.capability not in ("*", capability):
            continue
        if grant.resource_kind not in ("*", resource_kind):
            continue
        if grant.resource_id in ("*", scope):
            return True
    return False


def verify_grant_equivalence(
    grants: list[GrantRecord],
    probes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """对一组探测请求比较新旧决定。任何不一致都作为不兼容项返回。"""

    divergences: list[dict[str, Any]] = []
    for probe in probes:
        common = {
            "actor_active": bool(probe.get("actor_active", True)),
            "capability": probe["capability"],
            "resource_kind": probe["resource_kind"],
            "resource_id": probe.get("resource_id", "*"),
        }
        old_decision = legacy_decide(grants, **common)
        new_decision = decide(grants, **common)
        if old_decision != new_decision:
            divergences.append(
                {
                    "probe": probe,
                    "legacy_decision": old_decision,
                    "new_decision": new_decision,
                }
            )
    return divergences


def revocation_targets(
    grants: list[GrantRecord],
    *,
    actor_id: str,
    capability: str,
    resource_kind: str,
    resource_id: str,
) -> list[GrantRecord]:
    """撤销时同时找出新旧表示下等价的授权记录。

    - 扁平旧授权与精确相等的 v2 层级授权都要撤销；
    - 一条层级授权和一条扁平授权若指向同一具体资源，撤销必须同时生效，
      否则模式切换会改变授权结论。
    """

    targets: list[GrantRecord] = []
    requested = resource_id or "*"
    requested_leaf = scope_segments(requested)[-1:] or [requested]
    for grant in grants:
        if grant.revoked:
            continue
        if grant.actor_id != actor_id:
            continue
        if grant.capability not in ("*", capability):
            continue
        if grant.resource_kind not in ("*", resource_kind):
            continue
        if grant.resource_id == "*" or grant.resource_id == requested:
            targets.append(grant)
            continue
        # v2 层级授权：指向被撤销资源本身（以其叶子段结尾）时一并撤销。
        if grant.hierarchical and scope_segments(grant.resource_id)[-1:] == requested_leaf:
            targets.append(grant)
    return targets
