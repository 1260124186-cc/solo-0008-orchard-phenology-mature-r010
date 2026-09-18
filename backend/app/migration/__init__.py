"""可演进领域规则迁移框架。

目标是在生产式负载下安全地推进领域规则与数据结构，而不是一次性不可逆脚本：

- 旧规则与新规则先“双读”，对每个对象比较同一业务结论；
- 对象按批切换，单批失败只影响该批，已提交批次不被波及；
- 迁移全程维护进度、失败项、批次检查点与不兼容对象集合；
- 新旧写入在同一事务内对拍，业务结论不一致即暂停迁移；
- 可停止或回滚尚未切换的批次；已正式提交且依赖新规则的事实保留并记录原因；
- 冻结分析、历史时点视图和启动恢复不随模式切换改变；
- 校验同时比较业务结果、版本血缘、审计与 outbox。
"""

from .service import MigrationService
from .ledger import MigrationLedger
from .phenology_v1 import V1_RULESET
from .phenology_v2 import V2_CHANGE_SET, V2_RULESET

CHANGE_SETS = {
    V2_CHANGE_SET.change_set_id: V2_CHANGE_SET,
}

__all__ = [
    "MigrationService",
    "MigrationLedger",
    "CHANGE_SETS",
    "V1_RULESET",
    "V2_RULESET",
    "V2_CHANGE_SET",
]
