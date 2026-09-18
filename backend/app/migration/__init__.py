"""可暂停、可回滚、按对象分批的领域规则与数据结构迁移框架。"""

from .engine import (
    IncompatibleObject,
    business_fingerprint,
    calculate_offsets_v2,
    downgrade,
    merge_eligibility,
    project_object,
    validate_v2_observation,
    v1_v2_parity,
)
from .runtime import RUNTIME
from .service import MigrationService

__all__ = [
    "IncompatibleObject",
    "MigrationService",
    "RUNTIME",
    "business_fingerprint",
    "calculate_offsets_v2",
    "downgrade",
    "merge_eligibility",
    "project_object",
    "validate_v2_observation",
    "v1_v2_parity",
]
