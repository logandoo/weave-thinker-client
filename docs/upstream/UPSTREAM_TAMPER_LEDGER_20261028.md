# 上游仓变更登记与补救清单（W33 · 2026-10-28）

> **红线**：`/home/user/Documents/DEV/chatbot` 对本仓只读（memory/project_port_mandate.md）。
> 本文 = 用户指令「对上游的修改 / 上游修改可能造成的冲突 / 上游应该继续的修改全部列出」的**全量登记**。
> 补救动作**由用户执行**——本文所有命令均为可复制粘贴件，登记方（本会话）不执行、不写上游。

---

## §0 本会话工具面审计（对上游的一切操作=只读，可复算）

本会话（W31 同步波 + W32 联动波 + W33 登记波）对 chatbot 仓执行过的 git 子命令**全集**：

| 子命令 | 用途 | 写面 |
|---|---|---|
| `git -C chatbot status --short` | 波前/波末红线核对 | 无 |
| `git -C chatbot log --oneline/--format` | 增量判定（rev-list 等价） | 无 |
| `git -C chatbot diff [--stat] <rev>..<rev> -- <paths>` | 三方对照取上游侧改动 | 无 |
| `git -C chatbot show <rev>:<path>` | 取上游文件字节（重定向落点=本仓或 /tmp，**不落上游**） | 无 |
| `git -C chatbot ls-tree -z <rev>` | 批量 blob sha（upstream_report.mjs 内） | 无 |
| `git -C chatbot rev-parse <rev>:<path>` | filemap 对照（upstream_report.mjs 内） | 无 |
| `git merge-file <merged> <base> <up>` | 三方合并——**输入输出全在 /tmp/w31sync、/private/var/.../w31b**，纯文件操作非仓操作 | 无 |
| `git -C chatbot stash list` | 红线核对 | 无 |
| `shasum`（管道读） | 直采 sha 全等证明 | 无 |

- **无** `checkout/add/commit/restore/reset/clean/stash push/rebase/filter-branch` 任一调用。
- `script/upstream_report.mjs` 写面审计：对上游只调 `gitUp(['ls-tree'…])` 与 `execFileSync('git',['-C',up,'rev-parse',…])`（读类）；一切 `writeFileSync` 落点在**本仓**（`docs/upstream/upstream_state.json`、`docs/upstream/UPSTREAM_SYNC_*.md`）。
- **交叉污染取证（最硬证据）**：对上游全部 146 项脏件逐一 `grep` 本仓内容签名（`settleRelayedTurn`/`cloud_relay_service`/`W32 执行环境云端联动`/`D-123`/`exec_env_relay`）→ **0 命中**。上游脏件不含本仓任何产物内容。

**结论**：本会话对上游零写入，且无内容污染。§1 登记的 146 项全部归属**上游仓内并行会话**（见 §2 归属证据）——但按用户指令仍全量登记、逐条给补救命令，供用户裁决。

---

## §1 上游仓变更全量登记（快照 2026-10-28 ~03:0x +0800）

### A. 已跟踪文件修改（M 类）：**当前 0 项**

波中两个观测点曾见 M 类（`backend/app/api/chat_stream_transport.py`、`backend/app/services/deathmatch_workspace.py`、`backend/tests/test_deathmatch_grilling_sota.py`、`backend/tests/test_deathmatch_obligations.py`、`tests/decisions.md`、`tests/.evidence.lock`、`tests/acceptance.md`、`tests/coverage_matrix.md`、`tests/verification_log.md`），当前 `git diff`（工作树 vs HEAD）为空。**逐件去向（评审 fix-round 修正——原「均被提交吸收」以偏概全）**：4 件工作流文件（.evidence.lock/decisions/coverage_matrix/verification_log）确被 §1.C 提交吸收；`backend/` 4 件被 `62db8e6f/d32d0dee` 等吸收；**唯 `tests/acceptance.md` 未经任何提交**（`git log 4cb33ddaa..HEAD -- tests/acceptance.md` 空；其最后改动=f18cb14c@10-27 18:49 早于 base）——M 态经 **restore/回退类非提交路径**消失，成因未证。判定=并行会话自有工具链动作（其 vibeweaver 门禁/锁机制 restore 自家证据文件同型，参见本波 audit-fix 先例），非本会话；**此单点状态跃迁在案未归因**，如需终局裁定可查其会话工具日志。

> ⚠ 观测点历史（防后人误读「曾有 M 是我写的」）：`tests/working_note.md` 与 `tests/deathmatch_feedback_20261027/` 在**本会话第一个工具调用时即已存在**（早于本会话任何写操作）。

### B. 未跟踪件（?? 类）：145 项 + 1 项 `? agent_skills`

| 组 | 项数 | 内容 | mtime 窗口 | 归属 |
|---|---|---|---|---|
| `tests/deathmatch_feedback_20261027/` | 143 | 死磕波评测证据（monitor_*.png / conversation_*.json / probe_*.py / controlled_eval_*.json 等） | Oct 27 17:51 → Oct 28 02:47 | 上游并行会话 |
| `backend/agent_memories/0dd2ddea-…/` | 1 目录 | 上游会话的记忆目录 | Oct 28 01:39 | 上游并行会话 |
| `tests/working_note.md` | 1 | **自述「Working Note — 死磕五症 R15 cap/stall」**（见 §2-3 内容签名） | Oct 28 | 上游并行会话 |
| `agent_skills` | 1（`?`） | **嵌套 git 仓**（`agent_skills/.git`，HEAD→refs/heads/main）——`?` 前缀即嵌套仓 porcelain 实态；波前即在。⚠ 其 `.git` mtime 在评审期 09:07→09:11 移动=**有进程在内跑 git**（其 app skills 机制），非惰性件 | 早于本会话 | 上游既有 |

### C. 上游新提交（本仓 base `4cb33ddaa` 之后，共 6 个——登记不追采，下波 delta）

| commit | 时间 | 摘要 | 同步域影响 |
|---|---|---|---|
| `4b35d3de` | 10-28 01:30 | fix(deathmatch): harden contract validation and mode transitions | backend deathmatch 族 |
| `53710c23` | 10-28 01:41 | test(deathmatch): verify R12 controlled delivery and latency | tests |
| `62db8e6f` | 10-28 02:00 | fix(deathmatch): replace shell gates with structured exact files | backend deathmatch 族 |
| `11077815` | 10-28 02:23 | fix(deathmatch): fail closed on invalid exact contracts | backend deathmatch 族 |
| `d32d0dee` | 10-28 02:44 | fix(deathmatch): stop invalid contracts before agent execution | chat_stream_transport 等 |
| `8dfd7f15` | 10-28 02:57 | docs(deathmatch): pause at review cap with fresh-brain handoff | 文档 |
| `1b12d92f` | 10-28 09:11 | backup: before changes（**评审期实证：并行会话已复工开新波**——R15 paused 续作启动） | 其 tests/paused_state.md −29 等 |

> ⚠ 数字随其活动漂移（本表 6→7 即评审期发生的）：以 `git log 4cb33ddaa..HEAD` 当次输出为准。截至收口 `master` = `1b12d92f`。

合并 diffstat（`4cb33ddaa..8dfd7f15`，同步域，±=churn）：`chat_stream_transport.py +37/−2` · `prompts/deathmatch.py ±11` · `deathmatch_grilling_flow.py ±113` · `deathmatch_service.py ±8` · `deathmatch_workspace.py ±251` · `refactor_move_manifest_20261019.json ±6` · `test_deathmatch_grilling_sota.py ±111` · `test_deathmatch_obligations.py ±236` · **`frontend/src/stores/chat.ts +9`** · `frontend/e2e/deathmatch_feedback_scope.spec.ts +31`（§4a filemap 件，前版漏列——评 A-Minor1 修正）。

---

## §2 归属判定与证据（每条可复验）

1. **内容签名 0 命中**（§0）：上游脏件不含本仓产物签名——排除「本会话写入上游」。
2. **提交作者与提交序**：6 个新提交作者 = `Developer`，提交信息为 deathmatch 工作流（fix/test/docs 交替，含 R12/R15 评审轮术语）——与本仓 W31/W32 工作面（同步/执行环境）完全正交。
3. **内容自述**：上游 `tests/working_note.md` 第一行 =「Working Note — 死磕五症 R15 cap/stall（2026-10-28）」，正文含其自身 cap/stall 状态、`tests/paused_state.md`、⛔ 记忆、"R11-R15 已达 5 轮评审修复上限"——**典型 vibeweaver 波工作记忆**，与本会话的 working_note（已蒸馏删除）无交集。
4. **时间线**：`tests/deathmatch_feedback_20261027/` 最早件 Oct 27 17:51（**早于本会话起点** Oct 28 ~00:40）；其最晚件 02:47 与 6 个新提交（01:30–02:57）连续——同一会话的活动轨迹。
5. **波中 M 观测与后续提交对得上**：波中所见 M 文件（chat_stream_transport/deathmatch_workspace/test_deathmatch_*）正是随后 `62db8e6f/d32d0dee` 等提交的改动文件——在途编辑被其本人提交，非本会话动作。
6. **工具面复算**（§0）：本会话命令全集只读；merge-file 的输入输出在 /tmp 副本上（`/tmp/w31sync` 15 组 + `…/T/opencode/w31b` 12 组三元组实物在两仓之外，mtime 00:47–01:00 落波窗内——评审 B 复核确认）。
7. **外部决定性检查（评审 B 补强，防自证）**：`git -C chatbot reflog` 仅 `commit:` 条目——**任何进程均无 checkout/reset/merge/stash 状态改写**；波内全量 mtime 扫描（00:30 起）无归属外写入（余项=上游 app 运行时产物 chatllm.pid/user_workspaces/frontend/dist 等）。这是 §0 自证之外的独立通道。
8. **引文修正（评 A-Minor2）**：working_note 首行含 `# ` 前缀原文=「`# Working Note — 死磕五症 R15 cap/stall（2026-10-28）`」；H2 引文完整原文=「老用户升级面的配置键迁移（上一条缺口）未实现亦未验证。」
8b. **签名碰撞注记（FCV 复核补）**：五签名 grep 的 `D-123` 词面与**上游自家 incident-60 决策编号**（其 review_package* 内 D-122/D-123，9-21 产物）相撞——命中 1 件属上游自有内容非本仓污染；其余 4 签名 0 命中，污染结论不变。另 FCV 时点观测：上游已复工至 **R16**（status 158 / M 11 / working_note=「死磕 R16 fresh-brain 合同清偿」），本节各快照数字的漂移警示由此进一步坐实。
9. **取证口径修正（评 A-Minor3）**：§0/§3a 的 grep 面=**146 项 status 条目**（含目录条目递归）× 5 个签名（settleRelayedTurn/cloud_relay_service/W32 执行环境云端联动/D-123/exec_env_relay）；评审复核另做全盘递归（含 gitignored `.log` 与 agent_memories 内容）同签名 0 命中。

**判定**：146 项 = 上游仓内**并行 vibeweaver 会话**（死磕波，当前处 R15 cap/stall paused 态）的产物。如用户掌握相反证据（如命令日志显示本方写入），按 §3 全量补救命令执行即可回滚/隔离。

---

## §3 补救命令（登记不执行；按安全级分组，**由用户执行**）

### 3a. 零风险（只读复验——建议先跑，确认本文结论）
```bash
git -C /home/user/Documents/DEV/chatbot log --format='%h %an %ci %s' -8
git -C /home/user/Documents/DEV/chatbot diff --stat            # 当前应为空
git -C /home/user/Documents/DEV/chatbot status --short | wc -l # 146
grep -rl "settleRelayedTurn\|cloud_relay_service\|W32 执行环境" /home/user/Documents/DEV/chatbot --include='*.py' --include='*.ts' --include='*.md' | head   # 应为空
```

### 3b. 低风险（未跟踪件归档——**建议先归档勿删**）
```bash
mkdir -p ~/chatbot-tamper-archive-20261028
cd /home/user/Documents/DEV/chatbot
tar czf ~/chatbot-tamper-archive-20261028/untracked.tgz \
  tests/deathmatch_feedback_20261027 tests/working_note.md backend/agent_memories
```
> ⚠ **勿直接删**：`tests/working_note.md` 是并行会话**在途任务状态**（其自述"默认续作：fresh-brain 独立修复"）——删除会毁掉其续作依据；`tests/deathmatch_feedback_20261027/` 是其评测证据（其 verification_log 引用）。若确认要清：先等其波次收口，或经其会话本人归档。`backend/agent_memories/` 内含**已跟踪文件**（整目录删除会产生 D 条目变更）；`? agent_skills`=嵌套仓且有活跃进程（§1.B），**勿动**。

### 3c. 高风险（如判定需整体回退其提交——**评审 B 判 Critical：原命令有实时数据丢失风险，已安全化重写**；影响并行会话，默认不执行）

> ⚠⚠ **执行前置条件（缺一即停）**：①上游已**静默 ≥24h**（无新提交/无新 M——其 09:11 已复工开新波 `1b12d92f`，活靶不可打）②先跑 3a 复验 ③保全分支打在 **HEAD**（不是固定哈希——固定旧 tip 会让随后新提交成悬空对象）。

```bash
cd /home/user/Documents/DEV/chatbot
# ① 前置检查：HEAD 必须等于登记值（收口登记=1b12d92f…），不等 → 【停】，有新提交=会话在途
CUR=$(git rev-parse HEAD); echo "$CUR"
# ② 静默期确认：应输出空
git log --since=24.hours --oneline | head
# ③ 保全（HEAD 锚定，覆盖当前与未来一切提交）
git branch backup-chatbot-20261028 HEAD
# ④ 回退：--keep 保留未提交改动不覆盖（替代 --hard 的直接原因见下）
git reset --keep 4cb33ddaa
```
> **销毁面清单（reset 会回退的已跟踪在途件）**：`tests/paused_state.md`（R15 暂停态/续作依据）、`tests/.evidence.lock`、`tests/verification_log.md`、`tests/decisions.md`、`memory/fix_forbidden_*`、`docs/AUDIT_*`、`tests/test_baseline.json` 等（`8dfd7f15/1b12d92f` 所携）——**与其 5a「勿动」直接冲突**，故本项默认不执行。
> 若其会话跑过 add -A 式备份提交，证据已变跟踪件——丢弃工作树会**从磁盘删除**（恰为 3b ⚠ 所禁）；此为用 `--keep` 而非 `git reset --hard` 的直接原因。reset 后其未跟踪件仍在（3b 才处理）。

### 3d. 不需要补救的部分（明确记账）
- **M 类 0 项**：无已跟踪文件处于修改态，无 `git restore` 可执行/需执行。
- **stash 0 项**：无需弹栈。
- **`? agent_skills`**：嵌套仓、波前即在、内有活跃进程（§1.B）——**不需要也不应当**由本清单处置。
- **本仓零外来污染**（措辞修正：原「git status 干净」在写入时点即不成立——W33 证据件在途）：本仓无外来写入；W33 登记件随 733cb90 提交、其后仅本波证据链工具态（D-125 lock-rebase 在途），无需回退本仓。

---

## §4 冲突预报（下一波同步域：base `4cb33ddaa` → target `8dfd7f15f`）

`node script/upstream_report.mjs` 复算：`conflict:1 ff-safe:9`（in-sync:970 / local-mod:97 / local-only:60 / excluded:695）。

### 4a. pending 件逐条

| 分类 | 文件 | 预报 |
|---|---|---|
| **conflict** | `frontend/src/stores/chat.ts` | **本波唯一真冲突点**。上游 +9 hunk 三处：`_deathmatchIntentSeq` 声明（~:493）、`selectConversation` 快照 `deathmatchIntentAtStart`（~:1807）、恢复腿加 `&& _deathmatchIntentSeq === deathmatchIntentAtStart` 守卫（~:1918）。本仓同文件持 W3/W26/W31/W32 本地 hunk（settleRelayedTurn+四尾锚/默认目标/插话串行/apiUrl 包裹）。**区域不重叠，预判可三方并集自动合并**，但因双侧均改同文件且本仓 hunk 密集，合并后必须跑 `test_exec_env_menu_w32`+`test_interjection_flush_refresh`+`test_deathmatch_poll_guard` 全链复核。 |
| ff-safe | `backend/app/api/chat_stream_transport.py` | 本仓同件持 W31 直采态（base 全等）→ 直采 8dfd7f15 版即可 |
| ff-safe | `backend/app/prompts/deathmatch.py` | 同上 |
| ff-safe | `backend/app/services/deathmatch_grilling_flow.py` | 同上 |
| ff-safe | `backend/app/services/deathmatch_service.py` | 同上 |
| ff-safe | `backend/app/services/deathmatch_workspace.py` | 同上 |
| ff-safe | `backend/tests/refactor_move_manifest_20261019.json` | 同上 |
| ff-safe | `backend/tests/test_deathmatch_grilling_sota.py` | 同上（**新采测试件**，波内须 `- test-change:` 登记） |
| ff-safe | `backend/tests/test_deathmatch_obligations.py` | 同上 |
| ff-safe | `frontend/e2e/deathmatch_feedback_scope.spec.ts` | 同上（域外件 filemap 跟踪件） |

### 4b. 上游**在途未跟踪件**对未来同步的潜在冲突

- `tests/working_note.md` / `tests/deathmatch_feedback_20261027/` / `backend/agent_memories/`：均不在 `[[map]]` 同步域 → **不会**进入本仓同步面；仅影响上游仓卫生。
- ⚠ **二阶风险**：并行会话仍在写（最后活动 02:57）；若其后续把 `frontend/src/stores/chat.ts` 再改（R15 后续修复其自述"默认续作"），该件的冲突面会**持续增长**——建议下波同步前先确认其波次已收口（`git status` 无新 M、无新提交静默 24h）。
- ⚠ **历史改写风险**（W27 前科）：若其或任何人再跑 filter-repo 类历史改写，base 哈希链会再次断——下波务必先 `upstream_report.mjs` 跑 base 可解析性再采。

### 4c. 本仓共享件（vendored 镜像）的本地 hunk = 冲突源清单

| 共享件（[[map]] 域内） | 本仓 W31/W32 本地 hunk | 下波处置 |
|---|---|---|
| `app/src/stores/chat.ts` | settleRelayedTurn+限频早退 · resolveDefaultExecutionTarget 云端注册表腿 · 四条 turn 尾锚 · import 扩充 | 三方合并保本地；上游 hunk（deathmatch intent 守卫）直采 |
| `app/src/components/ChatInput.vue` | W32 菜单三选一重写 · 显示映射 · selectLocalTarget 守卫 · refreshExecContext · 联动预热 · 形状防御 | 保本地（§3 登记 W32 行）；上游 deathmatch 侧 hunk 并集 |
| `app/src/components/MessageBubble.vue` | self 目标文件面本地化 + 响应式 priming | 保本地 |
| `app/src/types/index.ts` | `ClientDevice.is_self?` 字段 | 保本地（向后兼容字段） |
| `app/package.json` | test:unit 链追加 `test_exec_env_menu_w32.cjs` | 并集（上游再加条目时同 W31 package.json 处理法） |
| `server/main.py` | `CloudRelayMiddleware`（**注册序=CORS 内层**） | 保本地（§3 登记）；上游 hunk 并集 |
| `server/app/services/remote_dispatch_router.py` | 自目标短路（device:<self> → 本地执行） | 保本地（§3 登记）；若上游改派发路由须重审 |
| `server/API.md` | 「Cloud Device Registry」两端点契约节 | 保本地（该文件本有 client-exec 本地节先例） |
| `app/src/api/clientExec.ts` · `app/src/utils/selfDeviceId.ts` · `server/app/api/client_exec.py` · `server/app/services/cloud_link_service.py` · `server/app/services/cloud_relay_service.py` | **local-only 新件/客户端专属**（上游无对应） | 不冲突（local-only:60） |
| `server/app/services/sandbox_env.py` · `spawn_registry.py` · 相关 tests | W31 上游直采（sha 全等） | 下波随 ff-safe 直采 |

---

## §5 上游应该继续的修改（handoff 清单）

### 5a. 上游**在途勿动**清单（并行会话自有，本仓不代庖）
1. **死磕五症 R15 paused 收口**：其 `working_note.md` 自述默认续作=fresh-brain 独立修复全部 Important（raw lossy normalization · last_verification/settled 未清 · replan obligations 未武装 · excerpt symlink · exact 内部绝对路径 · 旧 plan directive · resume 纠正不可见）+ RED→GREEN + 双评审 + controlled/FCV。**补救=不打断、不代提交、不清其证据**。
2. 其 `tests/paused_state.md` 与 ⛔ `memory/fix_forbidden_deathmatch_contract_shortcuts.md` 在其仓内——随其波收口。

### 5b. handoff 未销账项（应由上游在其仓内继续修改）
| # | 项 | 证据 | 上游应做的修改 |
|---|---|---|---|
| H1 | **W28 T1：`backend/config.toml.example` 缺 F7/F4 键**（实证复核仍开：该件 grep `execute_code`/`max_consecutive_iterations` **0 命中**，本仓同件 W28 D-118 已修为 `execute_code = 300`/`max_consecutive_iterations = 60`） | upstream.toml W28 注记 · memory/fix_w28 | 模板补 `execute_code = 300`（F7：键名=注册名）+ `max_consecutive_iterations = 60`（F4）；由其模板生成的实例才不静默失去停滞看门狗 |
| H2 | **W30 追加：老用户升级面配置键迁移未实现亦未验证**（`memory/fix_w28_upstream_sync_20261026.md:57` 原文） | 同上 | 配置加载侧做旧键→新键迁移/告警（`code_execution`→`execute_code` 等），否则老实例升级后看门狗仍失效 |
| H3 | （可选）W32 新契约如上游要对齐：`/api/client-exec/cloud-devices*` 为**客户端专属代理**（上游无 client-exec 域），**无需上游改动**；`remote_dispatch_router` 自目标短路亦为本地栈专属语义（云端无「自身设备身份」），**不回传** | 本波 §3 登记行 | 无（维持分野） |

### 5c. 本仓侧应继续（登记补正，本波已做）
- `docs/DECISION_MATRIX_local_vs_upstream.md` §3 补 W31/W32 本地 hunk 登记行（见 §4c 清单）——本波同步落盘。

---

## §6 复验入口（本文结论的一键复算）

```bash
# 总量
git -C /home/user/Documents/DEV/chatbot status --short | wc -l    # 146
git -C /home/user/Documents/DEV/chatbot diff --stat | wc -l       # 0（M 类已清）
# 归属
git -C /home/user/Documents/DEV/chatbot log --format='%h %an %s' 4cb33ddaa..HEAD
head -3 /home/user/Documents/DEV/chatbot/tests/working_note.md
# 下波同步面
node script/upstream_report.mjs   # conflict:1 ff-safe:9（target 随其活动漂移，以当次输出为准）
```
