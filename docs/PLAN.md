# PLAN — 2026-10-08 W11：agent 输入框 Shift+Enter 跟随修复 + vibeweaver 技能本地客户端实测/评估

来源：用户指令（两任务）。任务 A = ChatInput contenteditable 在「光标已位于可视区最后一行」时按 Shift+Enter，内部滚动不跟随新光标行、鼠标无法选中新行；任务 B = 将 `/home/user/Documents/DEV/SKILLS/vibeweaver` 技能本体加入本地客户端 alice 账号做完整多轮端到端测试，并评估该 skill 是否需要独立 coding 模式与 MoA（仅评估）。
前置证据：`tests/chatinput_shift_enter/`（最小复现+候选修复实验，已入库）；根因见 verification_log。

## Consistency Hub

| entity | canonical value/type | source of truth |
|---|---|---|
| 本地客户端后端 | `http://127.0.0.1:8160`（已安装 WeaveThinker.app sidecar；DB 55432） | `~/Library/Application Support/weave-thinker-client/localstack/app/config.toml` `[server].port` |
| alice 账号 | user_id `beef0002-19ad-4544-b676-c013cc39bed4`（本地 DB `users`） | DB 55432 查询（本波实测） |
| 目标 skill 源 | `/home/user/Documents/DEV/SKILLS/vibeweaver/`（SKILL.md sha256 `2434bb8d…`） | 用户指令路径 + shasum |
| 技能上传名 | `vibeweaver`（实测：本地客户端打包 sidecar 无系统技能合并，重名限制不触发；D-74 修订） | 8160 `GET /api/skills` 实测 + D-74 |
| 技能上传 API | `POST /api/skills/upload`（zip，顶层目录名=技能名；`.py` 触发 executable 警告 → `force=true`） | server/app/api/skills.py:434-533 |
| 修复文件/函数 | `app/src/components/ChatInput.vue` `scrollCaretIntoView()` + 新增 `measureCaretLineRect()` | 本波 diff |
| caret 行盒 margin | `8`（既有常量，不新引入） | ChatInput.vue:1426 |
| dist 测试基址 | `VITE_API_BASE=http://127.0.0.1:8160`（仅在测试构建期，config.toml 用后复原） | D-75 |
| E2E 测试入口 | `tests/ui_chatinput_shift_enter.py`（dev Electron + CDP，沿用 ui_note_cmd_s.py 模式） | 既有 harness |
| 评估报告 | `docs/AUDIT_2026-10-08_vibeweaver_coding_mode_moa.md` | 本波交付物 |
| 验证命令 | `bash script/linux/project_build.sh` · app `test:unit` · `.localstack/venv/bin/python tests/ui_chatinput_shift_enter.py` | package.json / 既有 log |

## Task 1 — 根因调查 + 回归测试先行（RED）

- **Files**: 既有 `tests/chatinput_shift_enter/{repro.html,repro2.py,probe_fix2.py}`（证据，已入库）；新增 `tests/ui_chatinput_shift_enter.py`。
- **Interfaces**: Consumes 本地客户端后端 8160（登录/建会话）+ dist（8160 基址）+ dev Electron；Produces RED 证据（修复前 UI 实测：`caret 不可见 / 点击不到新行`）。
- **Test seam**: 真实组件 `.chat-input`（contenteditable）在真实客户端壳内——DOM rect/scrollTop + 截图；不新增生产代码缝。
- **Steps**:
  1. 最小复现（已完成）：确认 Chromium 对行尾 `\n` 边界的折叠 Range 返回空 rects/零 bounding（`tests/chatinput_shift_enter/probe_fix.trace.log`）。验证：M1 空、M2 ZWSP marker 得 24px 行盒。
  2. 候选修复实验（已完成，`probe_fix2.py` PASS）：marker 回退 + 既有 margin 滚动，连续 4 次 Shift+Enter 均可见；⌘Z 撤销不受 marker 污染。
  3. 写 `tests/ui_chatinput_shift_enter.py`（含技能 E2E 复用 session helper）→ 对未修复源码跑出 RED。验证：测试 rc=1 且失败点=判据 #1。

## Task 2 — 修复落地（GREEN，最小变更）

- **Files**: Modify `app/src/components/ChatInput.vue`（scrollCaretIntoView 回退链 + `measureCaretLineRect()`；isComposing 守卫）。
- **Interfaces**: Consumes 既有 `isComposing`/`inputAreaRef`；Produces `measureCaretLineRect(range): DOMRect | null`（仅当折叠 Range 无有效 rect 时由 scrollCaretIntoView 调用；测后移除并还原选区）。
- **Test seam**: 同 Task 1 的 E2E；另跑 app `test:unit` 回归（含 test_voice_toggle_wiring 静态断言）。
- **Steps**:
  1. 编辑 ChatInput.vue（test-first 已由 Task 1 的 RED 满足：E2E 针对真实组件）。
  2. `bash script/linux/project_build.sh`（dist 保持 8160 基址）→ 重跑 E2E 至全绿（含连续换行/点击/撤销/Enter 发送腿）。
  3. app `test:unit` 全绿。

## Task 3 — 技能上传到本地客户端账号

- **Files**: 生成物 `tests/vibeweaver_dev_upload.zip`（不入库，随测试清单）；账号 DB 行（user_skills/skill_files，外部状态）。
- **Interfaces**: Consumes `POST /api/auth/login`（alice）+ `POST /api/skills/upload?force=true`；Produces 可复读的账号技能 `vibeweaver`（含 files）。
- **Test seam**: HTTP API 双证（列表 + by-name 详情文件清单/sha 对比源文件）；UI 面板可见（Task 4 截图）。
- **Steps**:
  1. 打包技能本体（SKILL.md + 10 伴读 + scripts/，排除 tests/memory/.git/.DS_Store）为 zip，顶层目录 `vibeweaver/`。
  2. 登录 8160 → 上传 → `GET /api/skills/by-name/vibeweaver` 核对文件数/内容；`GET /api/skills` 与 DB `skill_files` 双证。

## Task 4 — 完整多轮端到端（UI，真实 LLM）

- **Files**: `tests/ui_chatinput_shift_enter.py`（会话腿复用）+ 证据 `tests/screenshots/w11_*`·`tests/w11_chat_e2e.trace.log`。
- **Interfaces**: Consumes Task 2 的修复构建 + Task 3 的账号技能；Produces 判据 #5/#6 证据（技能可见；≥2 轮对话真实调用技能加载）。
- **Test seam**: 真实客户端 UI：SkillsPanel 列表 + ChatArea 会话（agent SSE 轨迹/DOM tool 卡片）。每步超时 120s；LLM 单轮超时余量 180s。
- **Steps**:
  1. 冷启动 dev Electron（isolated profile）→ 注入 alice 登录态 → 技能面板断言 `vibeweaver` 在列（API 交叉核对）。
  2. 新建会话 → 第 1 轮：任务指令（显式点名技能）→ 断言 agent 调用了技能加载工具（DOM/SSE 可见），且回复体现该技能工作流（如判定/分级/计划项）。
  3. 第 2 轮：按技能流程提供下一步输入（如确认/选择）→ 断言会话连续、技能上下文延续（≥2 轮）。
  4. 截图（面板/会话末态）+ trace 落盘。

## Task 5 — 评估报告（仅评估，零产品代码改动）

- **Files**: 新增 `docs/AUDIT_2026-10-08_vibeweaver_coding_mode_moa.md`。
- **Interfaces**: Consumes Task 4 实测证据 + 本仓机制事实（skill_tools/force_inject/hooks/MoA tool/deathmatch 模式/delegation 配置）；Produces 两问判定（独立 coding 模式？MoA？）与理由/证据。
- **Test seam**: 文档 read-back + 关键事实 grep 核对（如 `mixture_of_agents` 工具注册、`agent.moa.enabled`、skill force_inject 语义、deathmatch 用途）。
- **Steps**:
  1. 事实采集：`server/app/tools/skill_tools.py`（技能加载/注入语义）· `server/app/services/moa_service.py`+`tools/mixture_of_agents.py`（MoA 现状与开关）· `server/app/core/config.py`（force_inject/moa/delegation 配置）· deathmatch（语义=死磕模式，非 coding 模式）。
  2. 基于多轮 E2E 实测（技能在通用 agent 模式下的真实表现）逐项判定：独立 coding 模式必要性（对照现有机制能否达成目标、缺口是什么、成本）；MoA 必要性（技能属流程性方法论，MoA 本质=多模型答案聚合——是否命中其收益面）。
  3. 报告 read-back + `docs-drift` 判定。

## 计划自审

- Coverage：判据 #1/#2/#3/#4 → Task 1/2；#5/#6 → Task 3/4；#7 → Task 5，全覆盖。
- Placeholder scan：无 TBD/「类似 Task N」；每步含验证动作（命令或复读断言）。
- Type consistency：`vibeweaver` / `8160` / `scrollCaretIntoView` / `measureCaretLineRect` / `tests/ui_chatinput_shift_enter.py` 全篇单源（Hub 行）。
