# PLAN_w35_upstream_sync — chatbot `4cb33ddaa` → `477c4263`（deathmatch R16 波）同步实施计划

> 零上下文可执行。Wave: W35 · 2026-10-29 · Mode: AUTO · Lane: L · Class: CODE。
> 红线：`/home/user/Documents/DEV/chatbot` 只读（仅只读 git 命令）。

## Consistency Hub（共享实体一行真源）

| entity | canonical value/type | source of truth |
|---|---|---|
| 上游 base（波前） | commit `4cb33ddaa` | `upstream.toml` `base` |
| 上游 target（波末推进到） | commit `477c4263`（=477c42639db495d1ed1381f9414a0fc6a8e852f2） | 上游 `master` HEAD（波内锁定；若上游再动=下波 delta） |
| 目录映射 | frontend/src→app/src · frontend/public→app/public · backend/app→server/app · backend/skills→server/skills · backend/scripts→server/scripts · backend/tests→server/tests · backend/Fonts→server/Fonts · frontend/tests/workflows→app/tests/workflows | `upstream.toml` `[[map]]` |
| 单文件映射 | frontend/{package.json,vite.config.ts,tsconfig*.json,index.html,e2e/deathmatch_feedback_scope.spec.ts}→app/… · backend/{main.py,requirements.txt,API.md,config*.toml.example}→server/… · tests/test_math_tokenizer.cjs · tools/opensource_strip.py · docs/PROMPT_CHANGELOG.md | `upstream.toml` `[[filemap]]` |
| 测试解释器 | `.localstack/venv/bin/python`（3.13；系统 python3=3.9.6 不可用） | W28/W32 口径 |
| 基线断言 | 3621 passed / 1 failed（D-128 隔离）+ test:unit 49 链 all pass | `tests/w35_baseline_pytest.log` · `tests/w35_baseline_testunit.log` |
| 域内增量 | 22 件 = 19 直采 + 4 三方合并 | 本 PLAN §Steps + `docs/upstream/upstream_state.json` |

## Files（本波精确路径）

- Modify（直采，19）：`server/app/api/chat_stream_transport.py` · `server/app/prompts/deathmatch.py` · `server/app/services/deathmatch_{evidence,grilling_flow,plan,service,status,verification,workspace,write_through}.py` · `server/tests/refactor_move_manifest_20261019.json` · `server/tests/test_deathmatch_{contract_v2,grilling_sota,obligations,step_bypass,write_through_provenance}.py` · `app/e2e/deathmatch_feedback_scope.spec.ts`
- Create（直采，1）：`server/tests/test_deathmatch_r16_fail_closed.py`
- Modify（三方合并，4）：`app/src/components/Sidebar.vue` · `app/src/components/ZenHistoryDrawer.vue` · `app/src/stores/chat.ts` · `app/tests/workflows/test_deathmatch_review_r2.cjs`
- Meta（波末）：`upstream.toml`（base 推进 + 波注记）· `docs/upstream/UPSTREAM_SYNC_2026-10-29.md`（工具生成）· `docs/upstream/upstream_state.json`（工具生成）· `docs/upstream/UPSTREAM_TODO_20261029_w35.md`（handoff）· `docs/DECISION_MATRIX_local_vs_upstream.md`（§3 视合并结果补登）· `tests/{acceptance.md,verification_log.md,working_note.md,decisions.md,fcv_report_w35.md}` · `memory/fix_w35_upstream_sync_20261029.md` + `memory/MEMORY.md`

## Interfaces

- Consumes：`script/upstream_report.mjs` CLI（--write 产出报告+JSON）；`git -C <up> show <rev>:<path>`（只读）；`git merge-file`（本地三方合并，base 从上游 base commit 提取）；`.localstack/venv/bin/python -m pytest`；`npm --workspace app run test:unit`；`bash script/linux/project_build.sh`。
- Produces：直采件 hash-object==上游 target blob（可复算断言）；合并件=ours(base→local hunks)+theirs(base→upstream hunks) 并集；`tests/w35_*` 证据件。

## Test seam

- 既有 seam 即可（server pytest 全量 · test:unit 链含 test_deathmatch_review_r2 · project_build）。无新增 seam。
- 测试件变更仅两类：① 直采上游测试件（语义属上游，登记 `- test-change: 直采`）② 合并件 test_deathmatch_review_r2.cjs（本地 hunk=既有登记的路径映射，随波吸收上游 +3 检查，登记 `- test-change: 合并`）。

## Steps（每步≤5min 各带验证命令）

1. 权威分类：`bash script/linux/upstream_report.sh` → 核对与手工分类（19 直采+4 合并）一致。验证：报告表格动作类计数 + JSON `docs/upstream/upstream_state.json`。
2. 直采 19 件：`git -C <up> show master:<p> > <local>` 逐件后 `git hash-object <local>` == `git -C <up> rev-parse master:<p>`。验证：19/19 全等（脚本化逐行记录进 log）。
3. 三方合并 4 件：base=`git -C <up> show 4cb33ddaa:<p>`，`git merge-file -p L base U > merged`（冲突则逐 hunk 裁决）→ 写回。验证：`grep -c '<<<<<<<' `=0 + 与上游 target diff 抽查残差只含本地登记 hunk。
4. 合并件语义抽查（W28 假干净防线）：chat.ts 保留 settleRelayedTurn/桩守卫/imports；review_r2.cjs 保留 server/ 路径；Sidebar/ZenHistoryDrawer 保留 W26 常量与注记。验证：grep 逐条命中（log 记录）。
5. 测试面跑批（独占树）：`cd server && ../.localstack/venv/bin/python -m pytest tests/ -q` · `npm --workspace app run test:unit` · `bash script/linux/project_build.sh`。验证：≥基线（3621/1 D-128 隔离）+ 49 链 + BUILD OK。
6. 波末收口：报告复跑（动作类清零）→ `upstream.toml` base→477c4263 + 波注记 → 上游 `status --porcelain` 快照比对零写入。
7. 文档/记忆/评审/封印：UPSTREAM_SYNC/UPSTREAM_TODO/DECISION_MATRIX §3/memory + A4.9 双对抗评审（Lane L）+ FCV 复跑 + `evidence_seal.py` 重封 + `assert_artifacts.py --final` exit 0。

## Self-review

- Coverage：分类→直采→合并→测试→收口→评审→封印全链有步；上游零写入有验证步。
- Placeholder scan：无 TBD/「类似上步」；每步有验证命令。
- Type consistency：Consistency Hub 中 target/base/解释器口径与 acceptance #113-#117、working_note 一致。
