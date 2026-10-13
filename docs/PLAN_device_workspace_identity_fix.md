# PLAN — 设备执行 workspace_ctx 跨栈失配：客户端修复方案（身份绑定 + 契约重定义）

> 状态：方案书 v1（Class: DOC；实施待授权）· 2026-10-12
> 上游诊断：chatbot 仓 `memory/diagnosis_conv_e8e9494f_device_workspace_mismatch_20261012.md`（⏳ 未实施，待授权）
> 本次现场复现：`tests/e8e9494f_repro_*.json` + `tests/e8e9494f_*.png`（alice 登录 60 生产，深链
> conv e8e9494f，错误文案 `workspace_mismatch: workspace_ctx 'alice-cafef00d' != local 'bob-beef0001'`
> 在案；工具栏同时误显「执行: 云端」）。

## 0. 症状与根因（客户端侧病灶，已逐点核对源码）

云端会话（execution_target=device:ecb093…）下发的全部工具调用被本机 executor 拒绝。根因链中
**客户端侧**三处病灶：

| # | 位置 | 病灶 |
|---|---|---|
| 1 | `server/app/services/device_executor_service.py:1022-1036` `_resolve_identity()` | 属主解析取 `User.created_at ASC LIMIT 1`（**首个本地用户**=bob-beef0001），不是「本设备在为谁执行」。外部先例（UHP remote-harness 指南）：*Bind every remote environment to an explicit session/profile owner. Do not infer owner identity from a mutable process-global default.* |
| 2 | `device_executor_service.py:440-443` + `server/API.md:6744` | `workspace_ctx` 名门比对**本地**逻辑名 `{slug(local_username)}-{local_user_id[:8]}`；云端按**云端身份**算 `alice-cafef00d`（`remote_dispatch_router.py:145-155`）。W16 设计假定（`docs/PLAN_device_executor.md` §1.1「逻辑名对齐」）要求两侧同名同 uuid——真实部署中两栈 UUID 空间独立，永不相等 |
| 3 | `server/app/api/client_exec.py:75-86` 配对记录 | `device_exec.json` 只存 cloud_url/device_id/name/platform/token/tls_verify/created_at——**无任何云端/本地身份**，设备无从知道为谁执行、期望名门值是什么 |

云端附带缺陷（不属本仓，handoff）：`conversation.py:983-1014` GET detail 丢 `execution_target` →
前端 `chat.ts:1567` meta=null → 工具栏误显「执行: 云端」（截图实证）；`ui_preferences.default_execution_target`
在 `ChatInput.vue:1444/1783-1791` 被新会话静默采纳。

## 1. 归属判定（评估必答项，docs/DECISION_MATRIX_local_vs_upstream.md 模板）

```
[归属判定]
- 运行时: 双端（身份解析+名门=设备侧；workspace_ctx 产生/pair 身份字段/UI 标签=云端）
- 文件类别: A client-only（device_executor_service.py · device_pairing_service.py · client_exec.py · API.md 设备执行器节）+ 上游契约面（client_devices.py pair 响应 · conversation.py）
- 结论: 双端（顺序: 客户端身份绑定+契约重定义 → 上游小改+deploy_60 → 同步波取回契约增量 → 重打包 DMG）
- 交付动作: 上游 deploy_60 + 同步波（API 契约/API.md 对照）+ 重打包 DMG
- 验证面: 本地 server 单测+假云端回路（本仓）· 60 真联调（重配对后 conv e8e9494f 重跑）· 打包冒烟
```

## 2. 方案对比（Design Gate A）

### 方案 A — 配对时交换云端身份 + 名门按「云端身份」比对（推荐）
- 上游小改：`POST /api/devices/pair` 响应 `DevicePairResponse` 增 `user_id`/`username`（属主云端身份；
  `client_devices.py:128` 一行级）。**或**等价：pair 响应直接携带 `workspace_ctx` 期望值。
- 客户端 `client_exec.pair` 将 `cloud_user_id`·`cloud_username`·`local_user_id`（=配对发起者
  `current_user.id`）写入 `device_exec.json`（0600 模式不变）。
- `_resolve_identity()` 重写为显式绑定链：**配对记录 `local_user_id` → sync 绑定
  （`SyncState.cloud_username == 配对 cloud_username` 的 user_id）→ fail-closed**（新错误码
  `identity_unbound`；不再回落「首个用户」）。
- 名门契约重定义（API.md 同步改写）：期望值=**云端身份**导出
  `{slug(cloud_username)}-{cloud_user_id[:8]}`，与入站 `workspace_ctx` 严格相等；本地
  `root.name` 不再参与比对（仅作本地目录名）。
- 旧配对记录（无身份字段）→ fail-closed + status 面 `identity_bound:false` → 面板提示重新配对。
- 云端 UI 小改（handoff）：`conversation.py` get_conversation 补 `execution_target`。

**优点**：名门保持 fail-closed 严格对齐；两侧期望值同源自「云端身份」（跨栈唯一稳定身份）；
不依赖 sync 绑定存在；一次配对持久生效。**缺点**：pair 响应契约要加字段（上游小改+一次部署）；
旧记录需重配对（fail-closed 可接受）。

### 方案 B — 复用 sync 绑定取身份（纯客户端，零上游依赖）
- `SyncState` 增列 `cloud_user_id`（`PUT /api/sync/account` 核验登录后经 `/api/users/me` 或 JWT sub
  取回落库；`migrations.py` 同步）。
- `_resolve_identity()` 经 sync 绑定映射本地用户；期望名门值由 SyncState 云端身份计算。
**优点**：不动上游。**缺点**：设备执行被绑死在 sync 绑定（无绑定=不可用）；「配对身份」与
「sync 身份」两套可能不一致（配对用户 A、sync 绑定用户 B）需额外一致性校验；DB 列迁移面。

### 方案 C — 云端采纳设备上报名（反转名门）✗ 否决
设备 `initialize` 上报本地名、云端回显——校验变自证（tautology），失去「防云端误路由到错误
设备/用户」的名门价值；上游改动面更大。

### 裁决
**A 为主干，B 作为身份源 fallback 一环**。期望值永远按云端身份算；本地身份只决定「在哪个
本地用户 workspace 执行」。身份源优先级：配对记录 → sync 绑定 → fail-closed（`identity_unbound`）。
外部先例佐证：Tasq ADR-004（remote identity=(issuer,subject) 显式绑定、binding 与 store 路由
失配即 deny）· Microsoft agent-governance（fail closed，未知即拒绝）。

## 3. 客户端修改清单（按文件）

| 文件 | 改动 |
|---|---|
| `server/app/services/device_pairing_service.py` | 配对记录增 `cloud_user_id`/`cloud_username`/`local_user_id` 字段（`load_binding` 兼容旧格式→缺字段视为未绑定身份） |
| `server/app/api/client_exec.py` | `pair` 落身份三字段；`status` 暴露 `identity_bound` |
| `server/app/services/device_executor_service.py` | `_resolve_identity()` 重写（显式绑定链三段解析，fail-closed）；`_do_tools_call` 名门改为「期望 `workspace_ctx`=云端身份导出值」严格比对（:440-443）；错误码 `identity_unbound` 入协议 |
| `server/API.md`（设备执行器节） | `workspace_ctx` 语义改写：期望值=云端身份导出 `{slug(cloud_username)}-{cloud_user_id[:8]}`（跨栈不要求 uuid 相等）；错误码表补 `identity_unbound` |
| `app/src/components/DeviceExecPanel.vue` | `identity_bound:false` 时提示「重新配对以绑定执行身份」 |
| `server/tests/test_device_executor_service.py` | TDD RED 先行：身份解析腿（配对身份→用户选择→fallback→fail-closed）+ 名门腿（云端身份期望值严格比对/旧记录拒绝） |
| `server/tests/test_client_exec_pairing.py` | 配对记录身份字段持久化腿 |
| `upstream.toml` + `docs/DECISION_MATRIX_local_vs_upstream.md` §3 | local-mod 登记 + 上游 handoff 项登记 |

## 4. 上游 handoff（chatbot 仓并行会话 / deploy_60）

1. `client_devices.py` pair 响应 + `user_id`/`username`（或直接下发 `workspace_ctx`）。
2. `conversation.py:983-1014` get_conversation 补 `execution_target`（消除「执行: 云端」误导；
   `MessageBubble.vue:1421` 预览取目标同受益）。
3. （产品面，可选）`ChatInput.vue:1783-1791` `default_execution_target` 静默采纳 → 显式提示。

## 5. 修复后行为与边界（预期）

- conv e8e9494f 的工具调用将绑定到 **local alice**（配对发起者/sync 绑定用户）的 workspace
  （`alice-beef0002` 或其 `ui_preferences.workspace_root` 覆盖），不再误用 bob。
- ⚠ 既定安全面不变：remote_call 下 `super_admin_bypass` 失效（W16 §2.1），terminal 等工具的
  **工作区外绝对路径**仍受包含性检查约束（`terminal.py:200-298`，以 `config.workspace_root` 为界）。
  访问本机任意路径需工作区根覆盖（W12 P2#10）——本次修复不放开该边界。**待实测的次要疑点**：
  W12 用户级 `workspace_root` 覆盖若落在 `config.workspace_root` 之外，`_resolve_working_dir`
  在 remote_call 下可能仍拒绝（本地 UI 被 super_admin_bypass 掩盖）——实施波一并实测，若属实
  另开小波（归属：客户端 A 类）。
- 用户消息里 `/Users/...` 绝对路径的可执行性取决于上述边界，与 workspace_mismatch 是两个独立
  拦截点（前者=安全沙箱设计，后者=本次缺陷）。

## 6. 验证计划（实施波）

1. TDD RED：身份解析/名门用例先行（假云端回路 FakeCloud 夹具复用 W16 测试栈）→ 实现 → GREEN。
2. 兼容腿：旧 `device_exec.json`（无身份字段）→ `identity_unbound` fail-closed + status 提示。
3. 多用户腿：bob+alice 双用户 → 按绑定选 alice workspace。
4. 重置/换绑腿：reset 后拒新调用；换绑身份后名门随新身份。
5. 真联调（用户授权后）：60 重配对 → conv e8e9494f 重发工具调用 → `client_exec_audit` ok=true。
6. 回归：server 全量 + app test:unit + 打包冒烟（DMG 重打）。
