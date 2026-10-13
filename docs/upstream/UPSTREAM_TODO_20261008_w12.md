# 上游（chatbot）修改清单 — W12 客户端波（vibeweaver 完整运行 P0–P2）| 2026-10-08

> **用途**：本文件可直接交给上游 chatbot 会话执行。内容 = 客户端 W12 波（`weave-thinker-client`，head `b2fb80f`）在**共享镜像**中落地的修复/新功能，需上游同化（chatbot 仓 + 部署 60）。
> **路径约定**：本文件用**上游路径**（`backend/...`、`frontend/...`）；本仓 vendor 对应 `server/...`、`app/...`。
> **基线关系（重要）**：本仓服务器镜像基线 = 上游 HEAD `3966e1422`（W10 同步点）。下述**清单 A** 的 10 个文件在两端**逐字节一致**——上游可直接取本仓净补丁 1:1 套用：
> ```bash
> git -C /home/user/Documents/DEV/weave-thinker-client diff ebfac39..b2fb80f -- server/app/... 
> # 对应上游路径 backend/app/...（文件内容同基线，无三方冲突面）
> ```
> **红线**：本波**未触**红线 B 文件（`sync_capture.py`/`sync_apply.py`/同步 API/schema 未动）→ 无需逐字节红线移植。
> **上下文材料（本仓，供上游评审阅读）**：`tests/verification_log.md`（W12 节）· `tests/review_package_w12*.md`（双对抗评审 2 轮）· `tests/fcv_report.md`（10/10）· `docs/AUDIT_2026-10-08_vibeweaver_coding_mode_moa.md` 附录 A/B · `docs/DECISION_MATRIX_local_vs_upstream.md` §3 登记表。

## 概览

| # | 项 | 优先级 | 性质 | 云端（60）是否受影响 |
|---|---|---|---|---|
| A1 | `skills.py` description 块标量解析（`description: \|` → 字面 `"\|"`） | **P1** | 真 bug | ✅ Web 端上传技能同样受害 |
| A2 | `agent_loop.py` 收尾门禁抽取 + coordinator `direct_reply` 通路补门禁 | **P1** | 真 bug（enforcement 绕过） | ✅ 云端 vibeweaver 执法层同样被绕 |
| A3 | `vibeweaver_gate_service.py`：资源根物化 + python3 回退 + 项目根注册表/hint 归属 | **P1** | 真 bug（多项目工作区盲区） | ✅ 云端多项目工作区同盲区 |
| A4 | `vibeweaver_audit_service.py`：`skill.activated` 消费 + C17 名称判定 + terminal 写观察 | P2 | 观察面修复/增强 | ✅ 生效（log-only） |
| A5 | `skill_tools.py`：`_get_skills_dir` 走资源解析 + `skill.activated` producer session_id | P2 | 冻结部署正确性/事件修复 | ⚠ 云端源码态不显症；为镜像一致性必采纳 |
| A6 | `backend/skills/vibeweaver/**` 镜像刷新（SKILL.md `2081a455` → `2434bb8d`）+ UPSTREAM.md | P2 | 版本同步 | ✅ Web 用户应获新版技能 |
| A7 | `backend/tests/**` 测试移植（5 文件） | P1（随 A） | 保证断言面 | — |
| B | **工作区根用户级覆盖**（API/service/prefs/前端） | 产品裁决 | 新功能（共享面） | ✅ 若采纳须配白名单（见 B 的安全要求） |
| C | 仅本仓（`localstack/import_config.py` 模板补齐 / DMG / 证据） | — | 无上游映射 | 不适用 |

---

## A. 必改（共享镜像；补丁 1:1 可套）

### A1 — 技能 description YAML 块标量解析
- **上游文件**：`backend/app/api/skills.py`（与本仓基线逐字节一致）
- **现状**：`_process_skill_folder`（~L184）与 `.md` 上传路径（~L499）用朴素解析 `line.startswith('description:')` → `description: |` 被存成字面 `"|"`（块标量内容丢失），列表/编目/UI 描述面失效。
- **变更**：新增 `_parse_skill_md(content)`（转调 `skill_tools._parse_frontmatter`，已支持 `|`/`>`/行内三种），两处调用点替换为它。
- **源**：本仓 `746dd5d`（净补丁 `git diff ebfac39..b2fb80f -- server/app/api/skills.py`）
- **测试**：`backend/tests/test_skills_api_frontmatter.py`（新增，4 腿；含 `_process_skill_folder` zip 路径回归）
- **验收**：`.md` 与 `.zip` 两条上传路径（含 `description: |`、`description: >`）复读 API `description` 为真实文本（本仓 FCV 已 live 双路径复现）。

### A2 — 收尾门禁抽取 + coordinator `direct_reply` 补门禁
- **上游文件**：`backend/app/services/agent_loop.py`（逐字节一致）
- **现状**：`_completion_guardrails_tail` 不存在；coordinator `route == "direct_reply"` 分支 `_stream_direct_reply(...)` 后**直接 `return`** → loop_guard 残余反馈与 vibeweaver stop 门禁（gate 收尾 + audit 终局核对）**整体被绕过**（云端同样）。
- **变更**：
  1. 抽取内联收尾块为 `AgentLoop._completion_guardrails_tail(state, conversation, user, assistant_content="", reasoning_content="") -> (events, blocked)`（主循环原位置改为调用，语义不变：blocked 即注入反馈 `continue`）；
  2. `direct_reply` 分支改为：累积流式 `{"content": ...}` 为 `_dr_content` → 调用收尾门禁 → 未拦则 `return`，被拦则落入主循环续跑（预算沿用 hook_service）；
  3. helper 的 loop_guard 分支加 `if assistant_content` 守卫（不得追加空 assistant 草稿）。
- **源**：本仓 `746dd5d`（抽取+接线）+ `d3be42d`（正文累积+空草稿守卫）
- **测试**：`backend/tests/test_agent_loop_guardrail_tail.py`（新增，4 腿）
- **验收**：dev 实测 `direct_reply` 回合出现 `agent_step stop_hook 收尾门禁` 且后端日志 `stop hook 'vibeweaver_gate' blocks completion (blocks=1,2)`（本仓 `tests/w12_dev_probe.py` 可复跑）。

### A3 — gate 服务：资源根物化 + python3 回退 + 项目根注册表/hint 归属
- **上游文件**：`backend/app/services/vibeweaver_gate_service.py`（逐字节一致）
- **现状与变更**（净补丁见下）：
  1. `_ensure_assert_script`：`backend_root/skills/...` → `get_config().resolve_resource("skills","vibeweaver","scripts","assert_artifacts.py", bundled_first=True)`（冻结/旁路部署正确；可执行资产防数据目录投毒）；
  2. `check_gate_async`：`shutil.which("python3")` 缺失 → 回落内联证据地板（弱宿主不再静默放弃门禁）；`run_assert(root, runner)` 显式传 runner；
  3. 新增 `_RECENT_ROOTS` 注册表（`remember_project_root`/`recent_project_root`，失活失效、容量上限 200）与 `resolve_session_root(workspace_path, key, starts, prefer_hints)`：
     - tool 调用（`_tool_after_hook`）`prefer_hints=True`：**按本次写入路径归属裁决**（相对路径按 bound 归一；无提示/不可解析时回退注册表/工作区根——git 等无路径写工具仍受 RED-latch）；
     - stop/RED-latch 检查 `prefer_hints=False`：注册表优先（bound 校验、缺失 fail-closed），兜底从 workspace_path 向上搜索；
     - 目的：修复「子目录项目对 stop 门禁/RED-latch 隐形」与「先触达 A 后写 B 被按 A 误判」。
- **源**：本仓 `746dd5d` + `d3be42d` + `7474ee2`
- **测试**：`backend/tests/test_vibeweaver_gate_hooks.py`（新增 6 腿：stop 子目录/失活失效/hint 优先/相对路径归一/无提示回退/资源物化+python 回退）
- **验收**：本仓 dev 探针（工作区内 active 项目 + `workspace_write` → `GATE-BLOCKED`；日志 `materialized tests/assert_artifacts.py`）。

### A4 — audit 服务：`skill.activated` 消费 + C17 + terminal 写观察
- **上游文件**：`backend/app/services/vibeweaver_audit_service.py`（逐字节一致）
- **变更**：
  1. `_event_listener` 消费 `skill.activated`（记录 `skill_activation_for(session)`）；
  2. `_audit_stop_hook`：active 项目被写（注册表有记录）且**激活名 != "vibeweaver"** → C17 告警一次（log-only）；root 解析走 `resolve_session_root`；
  3. `_audit_tool_before` root 解析：`prefer_hints=True`（写归属）/ RED-latch 写入封锁复用注册表；
  4. `_audit_tool_after`（terminal）：保守写模式（`>`/`tee`/`cp`/`mv`/`dd`/`truncate`/`install`）+ 会话有 active 项目（注册表或工作区根兜底）→ 每会话一次告警日志（不阻断、不改结果）。
- **源**：本仓 `746dd5d` + `d3be42d`
- **测试**：`backend/tests/test_vibeweaver_audit.py`（新增 7 腿：激活记录/producer/C17 名称判定/RED-latch 子目录/空 id 修复/git 回退/terminal 观察）

### A5 — skill_tools：资源解析 + producer 修复
- **上游文件**：`backend/app/tools/skill_tools.py`（逐字节一致）
- **变更**：
  1. `_get_skills_dir()` → `config.resolve_resource("skills")`（冻结布局系统技能可见；dev 语义不变）；
  2. `skill_view` 的 `skill.activated` 事件 `session_id` 从 `conversation` 对象回退取 id（agent 派发 kwargs 无 `conversation_id`，此前事件 session_id 恒空 → audit 永不记录）。
- **源**：本仓 `746dd5d` + `d3be42d`
- **测试**：`backend/tests/test_skill_tools_vibeweaver.py`（新增 2 腿：resolve 调用点契约/frozen 优先级）

### A6 — 内置镜像刷新（vibeweaver）
- **上游文件**：`backend/skills/vibeweaver/**` + `UPSTREAM.md`
- **现状**：上游 SKILL.md sha `2081a455…`（2026-10-01 镜像）→ 目标 `2434bb8d…`（2026-10-08）。
- **变更**：按上游镜像 SOP 从源（`~/Documents/DEV/SKILLS/vibeweaver` 或 `~/.config/opencode/skills/vibeweaver` 的最新副本）重新镜像：SKILL.md + 10 伴读 + `scripts/{assert_artifacts,mm_probe,scan_secrets}.py` + `scripts/probe_vision.png`（不含 OpenCode 专属 JS 插件/自测架）；更新 UPSTREAM.md 注记（本仓 `server/skills/vibeweaver/UPSTREAM.md` 可直接参考，注意移除其中「客户端 W12」措辞）。
- **源**：本仓 `746dd5d`（`server/skills/vibeweaver/**`，13/13 文件与源 sha 一致）
- **验收**：镜像文件 sha 对账；打包/部署后 `GET /api/skills` 的 `system:vibeweaver` description 为真实文本（本仓打包侧车已实证）。

### A7 — 测试移植（随 A1–A5）
| 上游测试文件 | 类型 | 腿数 |
|---|---|---|
| `backend/tests/test_skills_api_frontmatter.py` | 新增 | 4 |
| `backend/tests/test_agent_loop_guardrail_tail.py` | 新增 | 4 |
| `backend/tests/test_vibeweaver_gate_hooks.py` | 修改+新增 | +6（原文件存在；`test_assert_script_materialized_from_skill` 需按资源契约适配——见本仓 `- test-change` 记录） |
| `backend/tests/test_vibeweaver_audit.py` | 修改+新增 | +7 |
| `backend/tests/test_skill_tools_vibeweaver.py` | 修改+新增 | +2 |
> 本仓对应净补丁：`git diff ebfac39..b2fb80f -- server/tests/`。

---

## B. 产品裁决后再改（工作区根用户级覆盖）

**背景**：客户端 W12 已实现（配置/API/前端全链，本地客户端实测通过）；是否上游同化需产品裁决（多租户安全面）。

- **上游文件（若采纳）**：
  - `backend/app/api/user_settings.py`（新增 `GET/PUT /api/users/me/workspace`；PUT 先 `_ensure_workspace_directories` 预检可写 → 再持久化 → ensure 失败回滚默认；支持 `[workspace].override_allowed_roots` 白名单）
  - `backend/app/services/user_preferences.py`（**新增**：ui_preferences 合并读写，None=删键）
  - `backend/app/services/workspace_service.py`（`workspace_root_override`/`default_workspace_root`/`resolve_user_workspace_root`；`ensure_user_workspace` 读取覆盖）
  - `backend/app/api/skins.py`（两处写点改 `apply_prefs_patch` 合并写——修复「皮肤保存清空其他偏好键」的既有隐患）
  - `frontend/src/api/userSettings.ts`（`WorkspaceRootInfo` + 两方法）
  - `frontend/src/components/WorkspaceSettingsPanel.vue`（**新增**）+ `SystemSettingsDialog.vue`（新增「工作区」页签）
  - `frontend/package.json` `test:unit` 追加 `tests/workflows/test_workspace_root_setting_wiring.cjs`
- **应用注意（这 3 个文件与上游**非**逐字节同基线）**：
  1. `backend/config.toml.example`：上游同名文件是 769B 的 `[sync]` 片段（非全量模板）→ **不要直接套用**；若采纳 B，由上游按自身模板约定补 `override_allowed_roots` 文档行（本仓模板见 `server/config.toml.example` `[workspace]` 段，含注释示例）。
  2. `frontend/src/components/SystemSettingsDialog.vue`：本仓基线与上游有既存本地差异（本仓多 'sync' 页签等）→ 页签/面板补丁按上游版本手工套用。
  3. `frontend/package.json`：名称/版本/测试链均不同（本仓含本地测试项）→ 仅追加 wiring 测试一项。
- **安全要求（硬性）**：这是一个「任意绝对路径写 + 自动建目录」原语。
  - 若上游/60 采纳：**必须**在部署配置设 `[workspace].override_allowed_roots`（前缀白名单；空=不限仅适用于单用户本地客户端），或对多租户直接禁用该端点。
  - 若不采纳：本功能保持**仅本仓客户端**（前后端成对）；已在 `docs/DECISION_MATRIX_local_vs_upstream.md` §3 登记为常驻本地补丁，同步波按登记表保留。
- **源**：本仓 `746dd5d` + `d3be42d`（PUT 加固/白名单/回滚）
- **测试**：`backend/tests/test_workspace_root_override.py`（2 腿）+ 前端接线 11 断言。

---

## C. 仅本仓（无需上游）

- `localstack/import_config.py` 模板补齐（本仓工具，无上游映射）。
- DMG 打包链与产物、`tests/**` 证据、W12 报告/记忆/登记表。

---

## 部署（60）与回程

1. **deploy_60**：当前 60 部署（09-30 `a5c8d5551`）**连 10-01 的 vibeweaver 集成波都不含**——一次部署可把「集成层 + 本清单 A + 新版镜像」带给 Web 用户；至少 A1（上传描述）与 A6（技能版本）直接改善 Web 体验。
2. **配置核对（部署时）**：确认 60 的 `backend/config.toml` 含 `[agent.skills] force_inject = ["vibeweaver"]`（上游开发机已有，部署环境需核对）；若采纳 B，加 `[workspace].override_allowed_roots`。
3. **回程**：上游采纳后，下一同步波按既有 SOP 把上游版本拉回本仓收敛（登记表冲突按 §3 规则处理）。

## 上游验收清单

- [ ] A1：`.md`/`.zip` 两路径块标量描述复读为真实文本；`backend/tests/test_skills_api_frontmatter.py` 全绿。
- [ ] A2：`test_agent_loop_guardrail_tail.py` 全绿；一次 `direct_reply` 回合在 gate 红时被拦（日志 `stop hook … blocks completion`）。
- [ ] A3：`test_vibeweaver_gate_hooks.py` 全绿；若 60 有冻结/弱宿主形态，核对系统技能列表与 gate 物化。
- [ ] A4：`test_vibeweaver_audit.py` 全绿；C17 告警只在「未激活 vibeweaver」时出现。
- [ ] A5：`test_skill_tools_vibeweaver.py` 全绿；`skill.activated` 事件 session_id 非空。
- [ ] A6：镜像 sha 对账；`GET /api/skills` 的 vibeweaver 描述正确。
- [ ] A7：`pytest backend/tests`（至少所列 5 文件）全绿；`test_assert_script_materialized_from_skill` 按资源契约适配（勿保留旧 `backend_root` stub）。
- [ ] B（若采纳）：白名单配置生效（越界 400）；`test_workspace_root_override.py` + 前端 wiring 全绿；否则本仓登记表保留。
