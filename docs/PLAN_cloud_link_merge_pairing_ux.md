# PLAN — 系统设置「云端关联」合并 + 设备配对语义显式化（2026-10-14）

> 零上下文执行者可读。Lane L · Class: CODE · Mode: AUTO。判据=tests/acceptance.md #20-#26。
> cap=5  stall=3×（每子问题迭代上限/停滞线）。

## 目标

1. 系统设置「同步」「设备执行」两 tab 合并为单个「云端关联」tab（acceptance #20）。
2. 云端地址统一：面板只有一处地址输入/展示，绑定与配对同源（#21）。
3. 绑定态可直接改设备名称（#22）；配对语义显式化：改名/重新配对/重置三动作分离（#23）。
4. 重配对不丢名称、device_id 连续性不回归（#24）。
5. 后端 `POST /api/client-exec/rename` + API.md 契约 + 上游 handoff 登记（#25）。
6. 零回归（#26）。

## Consistency Hub（一次写定，处处引用）

| entity | canonical 值/类型 | source of truth |
|---|---|---|
| tab key / label | `cloud-link` / `云端关联` | app/src/components/SystemSettingsDialog.vue tabs |
| 统一地址行 testid | `cloud-link-addr`（input，未绑定态）/ 同 testid 只读行（绑定态） | 本文件 Task 3 |
| rename 端点 | `POST /api/client-exec/rename`，body `{name: str}` → `200 {device_id: str, name: str, renamed: true}`；`400`（空名/未配对含墓碑） | server/API.md「设备执行器」节（Task 1 先写契约） |
| rename 客户端函数 | `renameDeviceExec(payload: {name: string}): Promise<{device_id: string; name: string; renamed: boolean}>` | app/src/api/clientExec.ts |
| rename 语义 | 仅改本地绑定记录 `name`（0600 文件）；**不**调云端（上游无改名端点——handoff 登记） | server/app/api/client_exec.py docstring + memory handoff |
| 配对 name 回落链 | `req.name` → 旧绑定记录 `name` → `f"{req.platform or 'device'}"` | server/app/api/client_exec.py pair() |
| 绑定态判定 | `status.paired` ⇔ 绑定记录含 `token`（load_binding() 口径） | server/app/services/device_executor_service.py status() |
| 已配对态新 testid | `device-exec-name-edit`（input）· `device-exec-name-save`（按钮）· `device-exec-repair`（展开钮）· `device-exec-repair-code`（重配对码 input）· `device-exec-repair-confirm` · `device-exec-repair-cancel` · `device-exec-binding-info`（只读绑定行容器） | 本文件 Task 3 |
| 保留 testid（不改名不改义） | `sync-account` `sync-status` `sync-devices` `sync-conflicts` `sync-bind-user` `sync-bind-pass` `sync-bind-tls-skip` `sync-bind-submit` `sync-bind-error` `sync-resync` `sync-panel-error` `sync-last-error` `device-exec-status` `device-exec-error` `device-exec-online-dot` `device-exec-code` `device-exec-name` `device-exec-tls-skip` `device-exec-pair-submit` `device-exec-pair-notice` `device-exec-pair-error` `device-exec-reset` `device-exec-last-error` `device-exec-identity-unbound` `device-exec-cloud-identity` `device-exec-expected-ctx` `device-exec-build` | 现 SyncPanel.vue / DeviceExecPanel.vue |
| 删除 testid | `device-exec-cloud-url`（配对地址输入）· `sync-bind-url`（绑定地址输入）→ 统一由 `cloud-link-addr` 取代 | 本文件 Task 3 |
| 显式语义文案（判据 #23 断言用） | 重新配对展开说明含「清除并替换当前绑定」+「改名称请用「保存名称」」；重置说明含「清除本地凭据」+「需重新配对才能恢复」 | 本文件 Task 3 |
| device_id 连续性（W21） | pair 请求链 req(`"new"`=逃生口)→旧绑定/墓碑（仅同 cloud_url）→空；≥500 去 id 降级一次标 `device_id_changed` | server/app/api/client_exec.py pair()（**行为不改**，仅 name 回落链动） |

## Task 1 — 后端 rename 端点（TDD，§A4.7 契约先行 + §A4.8 RED→GREEN）

**Files**
- Modify: `server/API.md`（「设备执行器」节新增 `POST /api/client-exec/rename` 小节——契约是 oracle，先写）
- Create: `server/tests/test_client_exec_rename.py`
- Modify: `server/app/api/client_exec.py`（新增 rename 路由；pair name 回落链见 Task 2）

**Interfaces**
- Consumes: `device_pairing_service.load_binding_record()` / `save_binding(record)`（既有，0600 落盘）；`device_executor.status()` 口径（paired=token 在档）
- Produces: `POST /api/client-exec/rename` — request `{name: str}`；response `200 {device_id, name, renamed: true}`；errors `400 detail="name 不能为空"` · `400 detail="未配对，无法修改设备名称"`（无 token 记录/墓碑/无记录同此）

**Test seam**: FastAPI TestClient + 依赖覆盖 `get_current_user` + `LOCALSTACK_HOME` 临时目录种子 `app/device_exec.json`（既有 `server/tests/test_client_exec_pairing.py` 同款接缝，不新增 seam）

**Steps**
1. [C] RED：写 `test_client_exec_rename.py` 三例（①已配对改名 200 且再读 status.name=新名 ②空名/纯空白 400 ③未配对或墓碑 400），`../.localstack/venv/bin/python -m pytest tests/test_client_exec_rename.py -q` → 看 3 failed（404 无路由），输出粘 verification_log
2. [C] API.md 写 rename 契约小节（字段/错误/语义「仅本地，云端无改名端点=handoff」）
3. [C] GREEN：client_exec.py 加 rename 路由（load_binding_record → 无 token → 400；name 校验 → save_binding → 返回），复跑 pytest 3 passed + `tests/test_client_exec_pairing.py` 全绿（不回归）
4. [C] pair name 回落链（#24 后端腿）：RED 补一例「重配对空名保持旧名」→ 改 pair() name 解析 → GREEN

**verify**: `cd server && ../.localstack/venv/bin/python -m pytest tests/test_client_exec_rename.py tests/test_client_exec_pairing.py -q`

## Task 2 — 前端合并面板（#20/#21/#22/#23/#24 前端腿）

**Files**
- Create: `app/src/components/CloudLinkPanel.vue`
- Modify: `app/src/components/SystemSettingsDialog.vue`（tabs 数组：删 `sync`+`device-exec` 两项、加 `cloud-link`；图标用 sync 循环箭头+显示器合成（沿用两枚既有 path 合并）；内容区 `v-else-if activeTab === 'cloud-link'` 渲染 CloudLinkPanel；删两组件 import）
- Modify: `app/src/api/clientExec.ts`（加 `renameDeviceExec`；`DeviceExecStatus` 不变）
- Delete: `app/src/components/SyncPanel.vue` · `app/src/components/DeviceExecPanel.vue`（无他处引用，grep `SyncPanel\|DeviceExecPanel` 零命中为验证）

**Interfaces**
- Consumes: `../api/sync` 全部既有函数 · `../api/clientExec` 全部函数 · `../composables/useDataRefresh`（refreshAllData/waitSyncComplete）
- Produces: `<CloudLinkPanel />`（无 props，自包含）；DOM 契约=Consistency Hub testid 表

**CloudLinkPanel 区块顺序（自上而下）**
1. `云端地址`：`cloud-link-addr` 行——同步未绑定=可编辑 input（预填序：account.server_url → 设备绑定 cloud_url → 空）；同步已绑定=只读展示 `account.server_url`+提示「如需更换请先解绑」；设备已配对且其绑定 cloud_url ≠ 上方地址 → 提示「设备绑定的云端地址与上方不一致——重新配对可归一」
2. `云端账号`（sync-account）：未绑定=表单 `sync-bind-user`/`sync-bind-pass`/`sync-bind-tls-skip`/`sync-bind-submit`（**无**地址输入——用区块1地址；区块1为空时按钮 disabled+提示）；已绑定=服务器/账号/最近同步只读 + `sync-resync` + 解绑按钮
3. `设备执行`（device-exec-status）：
   - 状态行（在线灯 `device-exec-online-dot`、配对、云端身份 `device-exec-cloud-identity`、名门期望值 `device-exec-expected-ctx`、构建 `device-exec-build`、最后心跳、`device-exec-last-error`、`device-exec-identity-unbound`）
   - **未配对**（!status.paired）：配对表单 `device-exec-code` + `device-exec-name` + `device-exec-tls-skip` + `device-exec-pair-submit`（「配对并启用」）+ `device-exec-pair-notice`/`device-exec-pair-error`；表单下一行小字「云端地址：{区块1值}（统一地址，可在上方修改）」
   - **已配对**（status.paired）：
     - `device-exec-binding-info` 只读绑定行（云端身份/云端地址/最后心跳）
     - 改名行：`device-exec-name-edit`（预填当前 name）+ `device-exec-name-save`（「保存名称」→ renameDeviceExec → 成功刷新 status + `device-exec-pair-notice` 显示「名称已保存」）
     - `device-exec-repair`（「重新配对」）→ 展开：说明文案含「重新配对将**清除并替换当前绑定**（使用全新一次性配对码重新注册；尽力保持 device_id 连续）」「仅修改名称请用「保存名称」，无需重新配对」+ `device-exec-repair-code` + `device-exec-repair-confirm`（「确认重新配对」→ 复用 pair 提交链，device_id 传 status.device_id）+ `device-exec-repair-cancel`
     - `device-exec-reset`（「重置本地凭据」）+ 说明文案含「清除本地凭据并断开执行连接（云端设备记录保留）；需重新配对才能恢复执行」
4. `同步状态`（sync-status）· 5. `设备管理`（sync-devices）· 6. `冲突记录`（sync-conflicts）· 7. `sync-panel-error` + 刷新按钮（沿用 SyncPanel 逻辑）

**Steps**
1. [C] 写 CloudLinkPanel.vue（SyncPanel 脚本逻辑整体搬入 + DeviceExecPanel 状态机改造为三态：unpaired/paired/reparing 展开；scoped CSS 各区块自带——不跨组件泄漏）
2. [C] SystemSettingsDialog.vue 换 tab；删两旧组件文件；`grep -rn 'SyncPanel\|DeviceExecPanel' app/src` 零命中
3. [C] clientExec.ts 加 renameDeviceExec

**verify**: `bash script/linux/project_build.sh`（BUILD OK + tsc 无错）；DOM 断言交 Task 3

## Task 3 — UI 测试契约更新（#20-#24 判别性断言，§V2 test-change=用户规格变更授权，加断言不减断言）

**Files**
- Modify: `tests/ui_device_exec_panel.py`（tab 点「云端关联」；断言改：`cloud-link-addr` 在、`device-exec-cloud-url` 不在；新增**绑定态**场景——预种子 `$LOCALSTACK_HOME/app/device_exec.json`（0600，含 token/device_id/name/cloud_url）后重开面板 → 断言 `device-exec-name-edit` 预填名 + `device-exec-name-save` 在 + `device-exec-repair` 展开后文案含「清除并替换当前绑定」+ `device-exec-reset` 文案含「清除本地凭据」+ 配对表单 `device-exec-code` 不在）
- Modify: `tests/ui_sync_panel.py`（tab 点「云端关联」；其余断言全保）

**Steps**
1. [C] 两测试改到新契约（断言只增不减：旧断言凡语义保留的逐条搬移；`device-exec-cloud-url`/`sync-bind-url` 两条由 `cloud-link-addr` 断言替代并在 trace 注明 test-change 授权=用户 #21）
2. [C] 跑两测试至 PASS

**verify**: `.localstack/venv/bin/python tests/ui_device_exec_panel.py` · `VITE_API_BASE=http://127.0.0.1:8161 bash script/linux/project_build.sh && .localstack/venv/bin/python tests/ui_sync_panel.py`

## Task 4 — 证据波 + 评审 + FCV + 收尾

**Steps**
1. [C] §A4.1 循环：截图（未配对态/绑定态/重配对展开态）→ model-native §A4.1.1 判读 → FAIL 诊断修复至 ALL pass
2. [C] server 全量 + app test:unit + BUILD OK（#26）
3. [C] COV-8 双对抗评审（Lane L）→ Critical/Important 修复 → scoped re-review
4. [C] FCV（tests/fcv_brief.md → 独立复跑 → tests/fcv_report.md）
5. [C] coverage_matrix.md / memory / handoff 登记 / assert_artifacts --final / 8 列表

**verify**: 全部见 verification_log.md 对应条目

## 归属判定（DECISION_MATRIX 用户裁定必答）

| 事项 | 归属 | 说明 |
|---|---|---|
| tab 合并 + 统一地址 UI | **仅本地** | 纯客户端面板重构，零协议改动 |
| rename 端点 + 本地改名 | **仅本地** | 绑定记录 name 展示名；不动云端 |
| 云端设备列表改名 | **双端（上游先行）** | chatbot 仓 client_devices.py 无改名端点——handoff 清单登记，不虚构调用；落地后本地面板可加「同步到云端」 |
| pair name 回落链 | **仅本地** | 纯客户端配对请求行为 |

## 自审（C3）

- 覆盖：#20→Task 2 · #21→Task 2+3 · #22→Task 1+2+3 · #23→Task 2+3 · #24→Task 1+2 · #25→Task 1 · #26→Task 4 — 逐条有行
- 占位符扫描：无 TBD/无「类似 Task N」/无未定义引用 — pass
- 类型一致：`renameDeviceExec({name}) → {device_id, name, renamed}` 三处（API.md/clientExec.ts/测试）同签名；testid 表单点真源 — pass
