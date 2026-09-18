# 领域规则与数据结构的可安全迁移框架

本目录把“新的阶段定义、字段精度、权限范围或引用结构”做成一个可在**生产式负载
下安全推进**的迁移，而不是一次性、不可逆的 SQL 脚本。

第 2 代规则相对第 1 代的真实演进（作为框架的首个落地计划）：

1. **阶段定义**：新增非必需阶段 `winter_rest`（休眠期，rank 100，位于 `leaf_fall`
   之后）。九个既有阶段的 key、中文标签、rank 与“完成必需”标记全部不变。
2. **字段精度**：置信度由 1..5 整数精度演进为 0.00..1.00 的两位小数
   （`confidence_score`），并要求映射可逆（0/0.25/0.5/0.75/1.0 ↔ 1..5）。
3. **引用结构**：允许把“同一株树的重复登记”合并到规范植株
   （`canonical_id` / `tree_canonical_id` / `season_key`），但不重写冻结对象
   的历史引用。

## 核心原则

### 1. 先双读验证，再按对象分批切换

每个批次的生命周期是：

```
pending → verifying → verified → applying → applied
                     ↘ failed（不影响其他批次，可更正后重试）
```

- `verify` 在同一事务里把批次内每个 v1 对象投影为 v2，并比较
  **业务结论指纹**（`engine.business_fingerprint`）。指纹忽略修订号、
  更新时间这类元数据，只比较真正的业务事实。
- 只有全批对象 v1/v2 指纹一致，批次才进入 `verified`；不一致的对象进入
  **不兼容集合**（`incompatible`），批次标记为 `failed`，不能切换。
- `apply` 在切换前还会再做一次复核（乐观防漂移），防止“验证之后、切换之前”
  对象被业务写入改动。
- 切换在**单个 SQLite 事务**内提交：业务实体、对象版本、审计事件、outbox
  事件和批次检查点一起成功或一起回滚。一个批次失败绝不影响其他已完成批次。

### 2. 新旧写入必须产生同一业务结论

迁移期间业务仍在持续写入。仓储 `Repository.atomic_update` 在每个写事务中、
业务落库前调用迁移双写钩子（`MigrationService.before_commit`）：

- **影子期（计划 active/paused）**：每笔业务写入都在同一事务内投影到 v2 并
  校验指纹一致。若某笔写入无法在新规则下保持同一结论
  （`migration_dual_write_conflict`），整笔业务写入连同实体、审计、outbox
  一起回滚。迁移期间新建的对象自动登记到最后一个未应用批次。
- **已切换期（计划 finalized，domain_generation=2）**：业务写入被就地升级为
  v2 载荷后再落库；不满足 v2 规则的写入被拒绝
  （`migration_v2_rule_rejected`）。
- 已切换批次内的可变对象在计划完成前不允许再按旧规则改写
  （`migration_object_locked`），避免“半迁移”状态下的语义分裂。

### 3. 进度、失败项、检查点、不兼容集合都被持久化

- `migration_plans`：计划状态（active/paused/finalized/rolled_back）、对象
  计数、原因说明。
- `migration_batches`：批次状态、期望/已切换对象数、**检查点**（成员键、
  切换前后 state_revision、规范植株映射）、最近一次核对报告、错误信息。
- `migration_objects`：每个对象的批次归属、状态、旧快照、v2 投影、v1/v2
  指纹与不兼容原因。
- `migration_ops`：迁移期间发生的**更正**与**合并**操作及生效批次。
- `entity_projections`：v2 影子投影。

任意时刻进程崩溃，`MigrationService.recover_runtime()` 在启动时把停留在
`verifying/applying` 的批次安全重置为 `pending`（这两种状态没有已提交事务
边界），并恢复生效世代、活跃计划与规范植株映射。

### 4. 可暂停、可恢复、可回滚未切换批次，并保留已提交事实

- `pause` / `resume` 暂停或恢复推进，期间只读核对仍可用。
- `rollback` 只回滚**尚未切换**的批次（清掉影子投影、成员回到 rolled_back）。
  **已经 `applied` 的批次不做反向重写**：这些对象已按 v2 正式提交，对象版本、
  审计和 outbox 已构成下游依赖的事实。回滚结果里逐项列出被保留的批次及其
  保留原因。
- `finalize` 只在“无遗留不兼容对象、所有批次 applied”时才允许，之后
  `domain_generation` 置为 2，新规则正式生效。

### 5. 冻结分析、历史时点视图、启动恢复不随模式切换改变

- **编研简报（brief）是冻结对象**：切换只做规则见证，载荷逐字节保留
  （仍是 `schema_version=1`），测试比对切换前后的规范化 JSON 完全一致。
- **对比图谱（comparison）是不可变记录**：不产生“假变更”事件，只用 v2 规则
  复算共同阶段偏移与摘要，要求与冻结记录逐字段一致；v2 新增阶段不出现在旧
  记录里，因此偏移、平均偏移、方向、结论文案都不变。
- **版本血缘**：切换以“追加修订”的方式发生（`migration.cutover`），
  `entity_versions` 中迁移前的每个历史修订都原样保留、可逐字节取回，
  `/api/versions/...` 的历史时点视图不受影响。
- **启动恢复**：见上，恢复逻辑本身经过“中断批次 + 跨进程 finalize +
  再次重启仍为世代 2”的测试。

### 6. 验证同时比较业务结果、版本血缘、审计和 outbox

`GET /api/migration/plans/{id}/report`（或 CLI `report`）输出四类核对：

- `business_result_parity`：所有已切换对象当前业务指纹是否仍等于 v2 结论；
  冻结简报是否逐字节未变。
- `version_lineage`：每个可变对象切换前的源修订是否仍能在
  `entity_versions` 中回溯。
- `audit_outbox_parity`：每个切换对象是否**恰有一条** `migration.cutover`
  审计事件和一条 `migration_cutover` outbox 事件；不可变对象是否**没有**
  被错误地产出切换事件。
- 批次/对象计数汇总。

## 迁移中发生的四类特殊情况

- **更正（correction）**：在新旧规则共用的同一条领域校验下更正对象
  （目前支持草稿园区改名、开放季节志说明）。更正与业务实体、审计、outbox
  同事务提交，并立即重做影子投影。
- **合并（merge）**：仅当两株 active 植株同园区、品种/砧木/定植年份一致、
  且没有同年季节志冲突时才允许合并引用；合并保留原 `tree_id` 历史引用，
  只增加规范引用。
- **授权撤销（revoke）**：迁移管理是独立能力 `migration:admin`（只读
  `migration:read`）。撤销授权后后续控制操作返回 403，已提交批次不消失。
- **后台任务（job）**：`migration.verify_batch` / `migration.apply_batch`
  注册在 worker 上，可通过 `enqueue_next_batch_job` 排队推进，沿用既有的
  租约、重试、死信机制。

## HTTP 接口

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| GET | `/api/migration/status` | `migration:read` |
| GET | `/api/migration/plans` | `migration:read` |
| PUT | `/api/migration/plans` | `migration:admin` |
| GET | `/api/migration/plans/{id}` | `migration:read` |
| PUT | `/api/migration/plans/{id}/pause` | `migration:admin` |
| PUT | `/api/migration/plans/{id}/resume` | `migration:admin` |
| PUT | `/api/migration/plans/{id}/rollback` | `migration:admin` |
| PUT | `/api/migration/plans/{id}/finalize` | `migration:admin` |
| PUT | `/api/migration/plans/{id}/batches/{seq}/verify` | `migration:admin` |
| PUT | `/api/migration/plans/{id}/batches/{seq}/apply` | `migration:admin` |
| GET | `/api/migration/plans/{id}/incompatible` | `migration:read` |
| GET | `/api/migration/plans/{id}/report` | `migration:read` |
| GET/PUT | `/api/migration/plans/{id}/corrections` | read / admin |
| PUT | `/api/migration/plans/{id}/merges` | `migration:admin` |

## 运维命令行

`scripts/migrate_domain.py` 提供与接口等价的安全操作，例如逐批推进、
遇不兼容即停：

```bash
python3 scripts/migrate_domain.py --data-dir backend/var plan --batch-size 50 \
    --name "阶段字典与置信度精度 v2"
python3 scripts/migrate_domain.py --data-dir backend/var advance PLAN_ID
python3 scripts/migrate_domain.py --data-dir backend/var report PLAN_ID
python3 scripts/migrate_domain.py --data-dir backend/var finalize PLAN_ID
```

## 模块

- `contracts/stages_v2.py`：第 2 代阶段字典。
- `contracts/confidence.py`：置信度精度的可逆换算。
- `engine.py`：v1↔v2 投影、业务指纹、v2 规则校验、合并资格（纯函数）。
- `store.py`：迁移台账的低层读写。
- `service.py`：计划/批次/回滚/正式切换/更正/合并/双写钩子编排。
- `runtime.py`：领域代码与仓储共享的“当前世代”状态。
