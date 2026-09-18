# 果园物候图谱编研台

本仓库是配对基线中的 `solo-0008-orchard-phenology-mature`，用于承载已有工作流上的 Feature 迭代任务。

面向地方品种保护人员、果园档案员和农业文化研究者的全栈编研工具。产品从园区建档开始，逐株登记果树，按季节记录物候阶段，再将两份已完成的季节志进行确定性对齐，并生成可长期保存的编研简报。

系统不连接外部气象、地图或远程数据库服务。档案保存在本机 SQLite 数据库中，支持事务、迁移、对象版本、审计、幂等写入和后台任务。

## 主要能力

- 园区档案：建立园区草稿，登记编号、地点、重点品种、责任人和种植年份。
- 植株编目：按园区维护植株编号、品种、砧木、定植年份和生长状态。
- 季节物候：按固定阶段顺序记录日期、置信度和说明，完成后冻结。
- 品种比较：只对两份同年已完成季节志的共同阶段计算日期偏移。
- 编研简报：冻结已确认园区在生成时点的植株与季节志摘要，并下载文本。

## 技术结构

```text
.
├── frontend/                 Vue 3 与 TypeScript 用户界面
│   └── src/
│       ├── app/              工作区状态与用例协调
│       ├── components/       表单、阶段轨道、植株卡片和反馈组件
│       ├── domain/           前端阶段字典、类型和边界规则
│       ├── services/         HTTP 调用与错误映射
│       └── views/            园区、观察、比较和简报工作面
├── backend/app/              Python 标准库服务
│   ├── application/          用例编排
│   ├── domain/               实体规则、状态转换和比较计算
│   ├── jobs/                 本地任务队列与 worker
│   ├── persistence/          SQLite、迁移、版本和审计
│   ├── security/             操作者、作用域与授权
│   └── transport/            HTTP 路由与响应编码
├── scripts/
│   ├── run_server.py         后端启动入口
│   ├── run_worker.py         后台任务 worker
│   └── workflow_check.mjs    三条浏览器工作流检查
├── PROJECT_SPEC.md
├── .project-manifest.json
└── .project-runtime.json
```

## 环境要求

- Node.js 20 或更高版本
- npm 10 或更高版本
- Python 3.11 或更高版本
- Playwright Chromium（浏览器工作流检查需要）

首次安装：

```bash
npm install
npx playwright install chromium
```

## 构建

```bash
npm run build
python3 -m compileall -q backend scripts
```

前端静态资源输出到 `dist/`。构建不会访问外部接口，也不会创建测试文件。

## 本地运行

先启动后端：

```bash
python3 scripts/run_server.py --host 127.0.0.1 --port 8765
```

需要处理后台任务时，在另一个终端启动 worker：

```bash
python3 scripts/run_worker.py --data-dir backend/var
```

再启动前端开发服务：

```bash
npm run dev
```

打开 `http://127.0.0.1:4317`。Vite 会把 `/api` 请求代理到同一个本机后端。

后端数据默认写入 `backend/var/atlas.sqlite3`。如果目录中存在旧版 `state.json`，首次启动会自动迁移到 SQLite。可以通过 `--data-dir` 指定其他目录。

受保护接口需要请求头 `X-Actor-Id`。本机前端默认使用 `local-admin`。写入接口可以携带 `X-Idempotency-Key`，相同操作者、相同键和相同请求体会复用第一次成功结果。

## 环境变量

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `ORCHARD_ATLAS_HOST` | 服务监听地址 | `127.0.0.1` |
| `ORCHARD_ATLAS_PORT` | 服务监听端口 | `8765` |
| `ORCHARD_ATLAS_DATA_DIR` | SQLite 数据库与运行文件目录 | `backend/var` |
| `X-Actor-Id` | 请求操作者标识，默认前端使用 `local-admin` | 必填 |
| `X-Idempotency-Key` | 写入请求幂等键 | 可选 |

浏览器工作流检查使用固定的本机端口 `8765` 和 `4317`，并使用临时数据目录。

## 测试与工作流检查

后端基础测试：

```bash
npm run test:backend
```

测试覆盖数据库迁移、事务写入、并发写入、幂等复用、对象版本、审计、身份作用域、授权撤销和任务生命周期。

一次执行构建、编译、后端测试和三条浏览器工作流：

```bash
npm run check
```

需要准备性能或容量场景时，可以生成可重复的规模数据：

```bash
python3 scripts/generate_dataset.py \
  --data-dir /tmp/orchard-scale \
  --plots 1000 \
  --trees-per-plot 5 \
  --observations-per-tree 3 \
  --comparisons 500
```

生成器会直接建立与业务域一致的园区、植株、完成季节志和比较记录，用于查询、迁移、压缩和并发实验。

三条检查都启动真实后端与真实 Vue 页面，通过浏览器完成关键步骤，再从 API 核对结果：

```bash
node scripts/workflow_check.mjs --workflow catalog
node scripts/workflow_check.mjs --workflow observe
node scripts/workflow_check.mjs --workflow compare
```

- `catalog`：建立园区、加入植株、确认园区并核对服务端状态。
- `observe`：建立季节志、补录四个必需阶段、完成并核对冻结结果。
- `compare`：准备两份同年已完成季节志，在页面生成比较并核对四条阶段偏移。

检查结束后会关闭服务、浏览器和临时数据目录。

## HTTP 接口概览

所有接口均以 `/api` 开头：

- `GET /api/health`：服务状态。
- `GET /api/stages`：固定物候阶段字典。
- `GET|PUT /api/actors`：查询或创建本机操作者。
- `GET|PUT /api/grants`：查询或创建资源授权。
- `PUT /api/grants/{grant_id}/revoke`：撤销授权。
- `GET /api/audit`：按对象或操作者查询审计事件。
- `GET /api/versions/{kind}/{id}`：查询对象版本快照。
- `GET /api/outbox`：查询待发布或已发布的 outbox 事件。
- `PUT /api/outbox/{event_id}/publish`：确认事件已由本地消费者发布。
- `GET|PUT /api/jobs`：查询或创建后台任务。
- `GET /api/jobs/{id}`：查询任务状态。
- `PUT /api/jobs/{id}/cancel`：取消排队或失败任务。
- `PUT /api/jobs/{id}/retry`：重试失败、死信或取消任务。
- `GET|PUT /api/plots`：查询或建立园区。
- `GET|PATCH /api/plots/{plot_id}`：读取或修订草稿园区。
- `PUT /api/plots/{plot_id}/confirm`：确认并冻结园区基础信息。
- `GET|PUT /api/trees`：查询或加入植株。
- `PUT /api/trees/{tree_id}/close`：标记植株退休或遗失。
- `GET|PUT /api/observations`：查询或建立季节志。
- `PATCH /api/observations/{id}`：修订草稿季节志说明。
- `PUT /api/observations/{id}/stages`：补录物候阶段。
- `DELETE /api/observations/{id}/stages/{stage}`：移除草稿中的阶段。
- `PUT /api/observations/{id}/complete`：完成并冻结季节志。
- `GET|PUT /api/comparisons`：查询或生成对比图谱。
- `GET /api/briefs` 与 `GET /api/briefs/{brief_id}`：查询编研简报。
- `PUT /api/plots/{plot_id}/briefs`：生成冻结简报。

## 数据与一致性

- 园区确认后不能直接修改基础信息；本基线不提供重新打开动作。
- 同一园区内植株编号唯一；定植年份不能早于园区起始种植年份。
- 同一植株、同一年份只能建立一份季节志。
- 完成后季节志不可增删阶段；完成前必须包含萌芽期、盛花期、坐果期和采收期。
- 比较只使用双方共同阶段，年份不同、状态未完成或无共同阶段时拒绝生成。
- 业务写入和审计、outbox、对象版本在同一 SQLite 事务中提交。
- 修改类接口使用对象 `revision` 执行乐观并发控制，旧修订号返回冲突错误。
- 已提交写入可通过 `X-Idempotency-Key` 安全重试，同一键不能复用于不同请求。
- 请求通过 `X-Actor-Id` 识别操作者，并通过能力和资源范围进行授权。
- 后台任务具有租约、尝试次数、重试时间和死信状态。
- 数据库迁移记录在 `schema_migrations`，旧版 JSON 快照仅执行一次导入。

## 生产式安全领域迁移

阶段定义、字段精度、权限范围和引用结构的演进不是一次性不可逆脚本，而是
`backend/app/migration` 提供的可分批、可双读、可停止回滚的迁移流水线。内置变更集
`phenology-v2-stage-precision-scope-reference` 同时演练四类演进：

- 阶段：在落瓣期与坐果期之间加入可选的休眠芽期（rank 55），必需阶段集合与原相对顺序不变；
- 精度：置信度由 1..5 整数扩展为 0.5 步长，旧整数全部合法且业务结论折算相同；
- 权限：引入 `plot/<id>/tree/<id>` 层级范围，旧的 `*` 与扁平授权决定保持不变；
- 引用：植株合并到规范树，成员引用经映射归一后比较与简报结论不变。

迁移接口（需要 `migration:read` / `migration:write` 能力，本机管理员默认具备）：

- `GET /api/migrations`：活动迁移、历史迁移与变更集说明。
- `PUT /api/migrations`：全量双读并规划批次，登记不兼容集合（`{ "batch_size": 20 }`）。
- `GET /api/migrations/{run_id}`：进度、对象计数、每批检查点、保留集合与引用映射。
- `PUT /api/migrations/{run_id}/batches/next`：切换下一批；`{"enqueue_job": true}` 改为后台任务。
- `PUT /api/migrations/{run_id}/stop`、`/resume`、`/rollback`、`/complete`：停止、继续、回滚、完成切换。
- `GET .../incompatible`、`.../failures`、`.../events`：不兼容对象、失败批次与迁移事件。
- `PUT .../batches/{batch_no}/retry`：重试失败批次（不影响其他批次）。
- `PUT .../trees/merge`、`.../corrections`、`.../grants/revoke`：迁移期合并、更正、授权撤销。
- `GET .../point-in-time/{kind}/{id}/{revisions}`：历史时点视图的新旧规则复算对比。

安全保证：

- **先双读后切换**：每个对象在旧规则与新规则下计算同一业务指纹，不一致即进入不兼容集合，不切换。
- **新旧写入同结论**：迁移期间实时护栏在业务事务提交前对拍，半级置信度或新阶段从旧接口写入会被整体回滚。
- **分批隔离**：单批失败只回滚该批未提交切换，已成功批次与其他批次不受影响；检查点同时比较业务结果、版本血缘、审计和 outbox。
- **可停止可回滚**：可停止或回滚尚未切换的批次；已正式提交且依赖新规则的事实（规范树、半级置信度、休眠芽）保留在 retained 集合并记录原因。
- **冻结与时点不变**：既有比较与简报在新规则下复算结论不变；任意历史版本经新旧规则复算结论一致；启动恢复对账后可继续推进，模式切换不改变业务结论。
- **后台任务化**：迁移批次是带租约、重试、死信的普通后台任务（`migration_run_batch`、`migration_recover`）。

## 测试状态

后端基础测试已启用，并与生产构建共同构成当前验证入口。三条浏览器工作流继续用于验证真实页面调用；后续测试任务可以在此基础上补充故障注入、迁移兼容和更大规模并发场景。

## 当前范围

不提供远程登录、密码或令牌体系、跨机器账号同步、外部气象数据、地图底图、数据删除、多节点部署和移动端原生应用。本机操作者与授权记录用于本地工作流，不构成远程身份系统。内置指南说明不替代农业技术结论。

## 配对关系

对应核心基线为 `solo-0007-orchard-phenology-core`。两者共享 `.project-pair.json` 中的领域契约，但可以独立构建和运行。
