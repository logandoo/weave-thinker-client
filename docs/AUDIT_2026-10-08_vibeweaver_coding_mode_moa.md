# AUDIT — vibeweaver 技能本地客户端实测 与「独立 coding 模式 / MoA」必要性评估

- 日期：2026-10-08 · 类型：评估报告（**仅评估，产品代码零改动**）· 环境：本地客户端（后端 `127.0.0.1:8160` / DB 55432，账号 alice）
- 输入：用户指令「将 `/home/user/Documents/DEV/SKILLS/vibeweaver` 添加到 alice 账号中进行测试，并且判断单纯针对这个 skill，是否有添加独立 coding 模式和 MoA 的需要」；追加指令「程序生成到 Documents/DEV/test 目录里」（D-78）
- 关联工件：`tests/ui_vibeweaver_skill_e2e.py`（验收 E2E）· `tests/w11_skill_e2e.sse.txt`（两轮 SSE 原始轨迹，含 turn1 skill_view 段）· `tests/w11_skill_e2e_evidence.md`（摘要）· `tests/w11_todo_cli_test.txt`（主机独立复跑）· `tests/w11_skill_autotrigger_probe.txt`（自动触发探针）· `tests/screenshots/w11_skill_{panel,turn1,turn2}.png`

---

## 0. 结论摘要

| 问题（仅针对 vibeweaver 这一 skill） | 判定 | 强度 | 核心理由 |
|---|---|---|---|
| 是否需要**独立 coding 模式**（产品级新模式/入口）？ | **非必需（能力面）**；但**技能自动触发/默认绑定是真实缺口** | 实测（正反两面；触发探针为单样本——见 §7） | 通用 agent 模式下技能全流程 100% 可执行（含真实编码+测试）；但**不点名技能则根本不加载**——缺口在触发与绑定，不在模式 |
| 是否需要 **MoA**（mixture-of-agents 支持）？ | **不需要**（对整技能流程）；保留现有单点工具为可选项 | 机制分析（未跑 MoA 对照实验——见 §7） | 技能=工具驱动、状态化的迭代流程；MoA=无工具并行的答案聚合，二者语义不兼容，且成本倍增 |

---

## 1. 实测设置与判据

- 技能本体上传（D-74）：`SKILL.md` + 10 伴读 + `scripts/`（6 个脚本），共 17 条目；`POST /api/skills/upload?force=true` → 账号技能 `vibeweaver`（id `d07f91a9-…`），DB `skill_files` 16 行（SKILL.md 存 `content`）。
- 验收判据（`tests/acceptance.md` W11 B 节 #5/#6）：#5 技能入账号（API 列表/详情 + DB 文件数 + 客户端技能面板 UI 三证）；#6 完整多轮端到端（≥2 轮实际触发技能 + 按技能流程在主机 `~/Documents/DEV/test/` 生成可运行实现 + 主机独立复跑）。
- Verifier：`model-native [image]`（本轮 probe PASS）+ DOM/API/DB 交叉证据。

## 2. 实测结果

### 2.1 技能入账号（判据 #5）—— 全通过

- API：`GET /api/skills` 含 `vibeweaver (source=user)`；`GET /api/skills/by-name/vibeweaver` 返回 SKILL.md 全文（首行 `# Skill: vibeweaver — Binding Contract + Companion Router`）；
- DB：`skill_files` = **16**（10 伴读 + 6 脚本）；
- UI：系统设置 → 技能管理 面板中 `vibeweaver` 在列（截图 `w11_skill_panel.png`）。

### 2.2 完整多轮端到端（判据 #6）—— 全通过（交付终次运行会话 `d3a3f3a7-…`；本波至少两次全绿，LLM 产出规模有浮动：25 文件/25 测试 与 12 文件/19 测试，均独立复跑通过）

| 轮次 | 输入 | 产物 | 证据 |
|---|---|---|---|
| 1（计划） | 点名技能 + 目标目录 `/home/user/Documents/DEV/test/w11-todo-cli`（Node CLI 待办清单，零依赖） | 3,486 字符计划：`skill_view` 加载全文（含伴读/脚本清单）→ 环境只读探测 → ZERO 分解（已知/未知/约束）→ 方案与验收判据 | SSE turn1 含 `skill_view`+`vibeweaver`（16 个工具事件）；截图 `w11_skill_turn1.png` |
| 2（实现） | 确认并实现 + 终端跑测试 | 4,534 字符回执 + **12 个文件（不含 .git）**的项目：`bin/todo.js` · `src/store.js` · `tests/`（19 用例，含 `.test.js` 与 `acceptance.md` 验收文档）· `script/linux/project_test.sh` · `package.json` | 截图 `w11_skill_turn2.png`；SSE dump（turn2 56 个工具事件） |
| 主机独立复跑（我方执行） | `node --test`（由 harness 发现测试入口并执行） | `tests 19 / pass 19 / fail 0`（exit 0） | `tests/w11_todo_cli_test.txt` |

- 结论：**通用 agent 模式 + 账号技能已足以端到端执行 vibeweaver 全流程**（ZERO 分类、Lane/Class、COV-2 script-only、`tests/red_evidence/` RED 留痕、`tests/golden/` 黄金输出、`script/` 构建/检查/测试脚本、终端 transcript）。技能内的 `{VW_DIR}` 资产（如 `assert_artifacts.py`）被正确取用（SSE 可见相应工具事件）。

### 2.3 自动触发探针（补充诊断）—— **负面结果**

- 会话 `1660eb46-…`（独立探针会话），提示词含明确软件任务但**不点名技能**：「我准备用 Node.js 写一个命令行计算器小工具…先说说你的实施思路」。SSE 231 KB、0 次 `skill_view`、0 次 `vibeweaver` 出现；模型给出 1,922 字符通用计划。
- 对照 2.2：显式点名时 `skill_view` 被调用。**结论：本账号当前配置下 vibeweaver 不会被自动触发。**
- 归因（双证）：
  1. **描述解析缺陷（产品 bug）**：SKILL.md 的 `description: |` 是 YAML 块标量；`server/app/api/skills.py` 的朴素 frontmatter 解析把描述存成字面 `"|"`（API 复读证据），技能编目/注入的触发面信息为无效值；
  2. **无 force_inject**：本地客户端配置无 `[agent.skills]` 段（`force_inject` 机制存在于仓库 server 代码但未配置），编目注入依赖模型自觉——无描述可用时几乎不可能自觉。

### 2.4 其他产品观察（评估范围，未修改）

- 打包态本地客户端 `GET /api/skills` 不合并系统技能（`list_system_skills` 为空——打包路径解析未生效）；本环境里账号用户技能是唯一可用载体。
- 默认工作区在 `Application Support/.../user_workspaces/alice-beef0002`（用户不可见）；本次能直写 `~/Documents/DEV/test` 依赖 `super_admin_bypass=true`。**用户想要「程序生成到 Documents/DEV/test」这一期望本身，暴露的是"项目目录可选/可见"缺口，而非模式缺口。**

## 3. 判定一：独立 coding 模式 —— 非必需；触发绑定是缺口

**3.1 能力面（不需要新模式的证据）**：§2.2 证明技能全流程在现有 agent 模式内可完成（含真实代码、脚本化构建、测试与证据链）。模式不会给技能带来能力增量——技能本身就是流程文本，通用 agent 已具备其依赖的全部工具（workspace/terminal/code_execution/git/skill_run_script）。

**3.2 触发面（真实缺口）**：§2.3 证明不点名即不加载。若产品期望「编码类任务默认按 vibeweaver 走」，最小必要改动是**绑定与触发**，而非新模式：
1. 修 `description` 解析（YAML 块标量/多行描述）——共享镜像代码（§5 归属：上游+部署 60，client vendor 随同步波）；
2. 账号级启用 `[agent.skills].force_inject = ["vibeweaver"]`（配置面，双端各自配置）或提供「技能启用/默认绑定」UI 开关（前端+上游）；
3. （建议）技能编目注入时对超长技能做摘要/路由注入，避免 49KB 全文的成本。

**3.3 UX/工程面（可选增强，与技能解耦）**：长实现轮的工具循环对用户不透明（turn 2 为一次巨型工作段）；产物落在主机目录后缺少"文件树/diff/一键打开"；默认工作区不可选。若有"coding 增强模式"提案，收益主要在此层（可见性/工作目录/长任务治理），应作为通用产品能力评估，而不是"为 vibeweaver 而建"。

**3.4 结论**：**不需要独立 coding 模式（能力上）；需要最小触发/绑定方案（若要让编码任务默认遵循该技能）。**

## 4. 判定二：MoA —— 不需要（整流程）

- 现状：`[agent.moa].enabled=false`、`reference_providers=[]`；工具 `mixture_of_agents` 已注册（toolset=reasoning，需 ≥2 provider）。
- 适配性：MoA 的语义是「多模型独立作答 → 聚合模型择优综合」，**不承载工具调用与状态**；vibeweaver 的产出靠工具循环（读写/执行/取证）与迭代收敛产生。把整流程交给 MoA 会（a）割断工具态，（b）成本与延迟倍增，（c）把确定性的流程证据变成聚合文本。
- 有价值的落点（可选）：单点决策（架构 A/B 抉择、调试假设排序）可作为**工具内的一次调用**，由主 agent 在循环中显式触发——现有 `mixture_of_agents` 工具即此形态，保留、默认关，按账号 provider 情况按需开启；**不需要产品级 MoA 模式**。
- 结论：**不需要 MoA 支持；保留现有单点工具即可。**

## 5. 归属判定（本仓评估必答项，按 docs/DECISION_MATRIX_local_vs_upstream.md）

| 建议项 | 运行时 | 文件类别 | 结论 | 交付动作 |
|---|---|---|---|---|
| 修 `description` YAML 块标量解析 | 云端+本地共用 | C 共享镜像（`server/app/api/skills.py`） | **仅上游+部署60（client vendor 随同步波）** | chatbot 修复 → deploy_60 → 同步波 → DMG |
| `force_inject`/技能默认绑定开关 | 各端行为 | A 配置面 + C（前端开关属共享镜像） | **双端（配置各自生效；UI 开关走上游+同步）** | 配置（本地 0600 config）· 可选 UI 上游 |
| 工作目录可选/产物可见性 | 本地为主（桌面） | A（desktop/本地 UI）+ C（服务端 workspace 面） | **双端按层拆（本地优先）** | 评估立项后重打包 DMG |
| MoA 维持现状（默认关、单点可选工具） | 双端共用 | C | **无需改动** | 无 |

## 6. 复现命令与证据路径

```bash
# 技能入账号核验（API/DB/UI）
WT_TEST_PASS=… .localstack/venv/bin/python tests/ui_vibeweaver_skill_e2e.py
# 生成项目独立复跑（交付终次 FCV 运行：tests 19 / pass 19 / fail 0；另一次全绿运行 25/25）
node --test ~/Documents/DEV/test/w11-todo-cli/tests/
# 原始轨迹/摘要
tests/w11_skill_e2e.sse.txt · tests/w11_skill_e2e_evidence.md · tests/w11_skill_autotrigger_probe.txt
```

## 7. 局限与未覆盖面（诚实声明）

- 自动触发探针为单提示词样本（1 conversation、1 prompt），结论「不点名不触发」对本配置成立；未穷举不同措辞；
- 未实测开启 `force_inject` 后的触发率（评估范围内不做配置变更）；
- MoA 判定为机制分析（其 disabled 现状 + 工具语义），未运行多 provider MoA 对照实验。

---

# 附录 A（2026-10-08 追查）：完整运行 vibeweaver 的修改清单

依据：hook 实触发核查（tests/w11_vibeweaver_hook_probe.log / *.sse.txt 探针）与代码逐点核对。**仅评估，未改产品代码**。

## 已经正确工作的部分（无需修改）

- hook 安装：gate {tool:1,stop:1} + audit {tool:2,stop:1,events:1}（sidecar 启动日志）。
- 运行时实触发（探针/真实 E2E 三线）：gate tool.after（GATE-BLOCKED）、gate/audit stop（收尾门禁 blocks=1/2）、audit 文本 loop-guard（armed 1/2、2/2）。
- 技能编目注入存在（`build_skills_system_prompt`，chat.py:2185）；`skill_view` 支持按 `path` 取技能文件（B3，系统/用户两路）；用户技能脚本上传标记可执行（DB is_executable=t）且 `skill_run_script` 有用户分支。
- `config.resolve_resource()` 只读资源解析器已存在且被 `skill_evolution_service` 正确使用。

## P0 — 技能「可见、可描述、可强制装载」

| # | 修改点 | 文件:行 | 现状/影响 | 修法 | 归属（DECISION_MATRIX） |
|---|---|---|---|---|---|
| 1 | 系统技能目录走 `backend_root` 而非资源解析器 | `server/app/tools/skill_tools.py:16-18` | 打包态指向用户配置目录 → `list_system_skills()` 恒空 → 系统技能（随包 vibeweaver）不可见；`is_system_skill`/`resolve_skill`/system 分支 `skill_run_script`/force_inject/`vw_dir` 全受影响 | `_get_skills_dir = config.resolve_resource("skills")`（对齐 skill_evolution_service.py:17） | C 共享镜像 → 上游+部署60 + vendor 同步波 |
| 2 | gate 的 assert_artifacts 物化路径同病 | `server/app/services/vibeweaver_gate_service.py:117-120` | 打包态物化失败 → 退化为内联证据地板（阻断仍在，但拿不到正典脚本；探针原文 "tests/assert_artifacts.py is missing"） | `config.resolve_resource("skills","vibeweaver","scripts","assert_artifacts.py")` | C → 同上 |
| 3 | 技能 description 块标量解析缺陷 | `server/app/api/skills.py`（`_process_skill_folder` 与 .md 上传两处朴素 frontmatter 解析） | `description: \|` 被存成字面 `"|"`（本账号实测）→ 编目/UI/自触发面失效 | 支持 block scalar（收集缩进块）或改真 YAML 解析 | C → 同上 |
| 4 | 客户端配置缺 `[agent.skills] force_inject` 等段 | 本地客户端配置生成链（`localstack/gen_config.py` 用模板生成——模板有；但 `localstack/import_config.py` 整份拷贝旧源配置、不做模板补齐 → 现网客户端配置 0 处） | 自触发无保障（基准：available 自触发率不足，需 force_inject；且 force_inject 仅系统技能生效——依赖 #1） | 导入脚本补模板新段合并；或为新装/升级重新生成并显式加 `force_inject=["vibeweaver"]`（`[hooks]`/`[vibeweaver]` 段显式化） | 双端配置面（本地生成链 + 上游部署示例已备） |
| 5 | 随包系统技能版本落后 | `server/skills/vibeweaver`（SKILL.md sha `2081a455…`，10-01 镜像）vs 目标 `2434bb8d…`（10-08） | #1 修复后客户端将启用旧版技能 | 按 UPSTREAM.md 流程刷新镜像（repo + 上游 chatbot 镜像）→ 重打包 DMG | 双端（mirror + DMG） |

## P1 — enforcement 完整性

| # | 修改点 | 位置 | 现状/影响 | 修法 |
|---|---|---|---|---|
| 6 | stop/RED-latch 找项目根只从 workspace_path 出发 | gate `_stop_hook`（:376-390）、audit `_audit_stop_hook`（:555-567）、audit `_audit_tool_before`（:515-538） | 子目录项目不被识别 → 收尾复查与 RED 写入封锁对"工作区内多项目"失效（tool.after 因带文件路径不受影响，行为不一致） | StopContext 增「本轮写路径/项目根」提示（agent_loop 已有 state 可传），或工作区服务解析当前项目 |
| 7 | `skill.activated` 事件无消费者 | 发布：skill_tools.py:367；订阅全服务仅 audit 的 message.delta（vibeweaver_audit_service.py:593） | 悬空信号（注释声称"audit 会话筛选信号"，实际未接线） | 接线（audit 用技能激活筛选会话）或删除 |
| 8 | terminal 写不设门（设计边界，与 OpenCode 插件一致） | gate `_GATED_TOOLS`（:66） | 探针/E2E 均通过 terminal 写文件即绕过完成门禁 | 保持（可选：audit 侧观察性告警，不阻断） |
| 9 | 无工具一次性回答不经 stop 钩子 | agent_loop 收尾路径（工具循环分支内） | 纯 chat 路径不跑收尾门禁（探针 v1 实证） | 可选评估：active 项目工作区在纯 chat 收尾时也跑 idle 检查 |

## P2 — 体验/产品（前评估 §3.3 已列，此处归并）

| # | 修改点 | 现状 | 归属 |
|---|---|---|---|
| 10 | 工作区根可选/可见（编码任务目标目录，如 ~/Documents/DEV/test） | 默认工作区在 Application Support 隐藏目录；写用户目录需 `super_admin_bypass=true`（本账号为真）；通用用户不可 | 本地为主（desktop/UI）+ 服务端 workspace 面（C） |
| 11 | force_inject 仅系统技能（A4.9 I6 安全守卫：用户内容不得以 authoritative 注入） | 用户上传的 vibeweaver 不能 force-inject | 如需改变=产品安全裁决（不建议绕过） |

## 验证面遗留（建议补测试，非阻塞）

- 本轮 gate/stop/loop-guard 触发为一次性探针；建议固化为 `server/tests/` 回归用例（hook 链级）。
- RED latch / audit BAD 路径在打包客户端从未实际发生（全历史计数 0），未做运行时演练。

## 建议实施顺序

`#1+#2（资源解析，一处模式）→ #3（描述解析）→ #5（镜像刷新）→ #4（配置段/导入合并）→ #6（项目根提示）→ #7/#9（信号收口）→ #10（产品 UX）`

---

# 附录 B（2026-10-08）：P0–P2 实施追踪（W12 波）

用户裁定「P0–P2 全做，vibeweaver 作为系统默认内置技能」。附录 A 的全部条目已实现并验证；对应关系：

| 附录 A # | 实施 | 证据 |
|---|---|---|
| P0#1/#2 | skill_tools/gate 走 resolve_resource；打包侧车实测 11 系统技能（原 0）+ gate 物化日志 | verification_log W12 iter 1/5/6 |
| P0#3 | `_parse_skill_md`（块标量）；打包列表 description 实测真实文本 | iter 1/6 |
| P0#4 | import_config 模板补齐 + 客户端配置合并 | iter 1/7 |
| P0#5 | 镜像刷新（2434bb8d…）+ DMG `0f635a91…` + scratch 12/12 | iter 6/7 |
| P1#6 | 项目根注册表（子目录可见） | iter 2 |
| P1#7/#8 | skill.activated/C17 + terminal 写观察 | iter 2 |
| P1#9 | 收尾门禁共用（direct_reply 曾绕过；dev 实测 blocks=1,2） | iter 2/5 |
| P2#10 | 工作区根覆盖（API+UI） | iter 3/5 |
| P2#11 | 保持（force_inject 仅系统技能——安全守卫不变；现系统技能即内置 vibeweaver，默认注入路径已通） | §4/D-74 |

**归属**：P0/P1 为实现于本仓共享镜像（`server/app`）的本地补丁（DECISION_MATRIX §3 登记，待同步波向上游同化 + 部署 60）；P2#10 本地为主（desktop/UI）+ 服务端面；P0#5 镜像刷新属双端维护件。DMG 已重打包供客户端安装。

**W12 评审收口补记（安全）**：工作区根覆盖在共享镜像中属「任意目录写」原语；本地单用户客户端默认不限（`[workspace].override_allowed_roots` 缺省），**多租户部署（60）须配置白名单前缀**后方可视为安全默认；该取舍已入 DECISION_MATRIX §3 登记与 W12 记忆档。
