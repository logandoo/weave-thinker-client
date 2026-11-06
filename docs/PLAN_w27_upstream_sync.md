# PLAN — W27 上游同步波（chatbot `6d93d919` → `7bac20d2`）| 2026-10-24

> 执行前提：上游历史已改写（git filter-repo 2026-10-17），旧 base `faf9afcab` 经
> `.git/filter-repo/commit-map` 映射到 `4c47f08b`（gen2），live 等价提交 `6d93d919`
> （message/author timestamp 全等；同步域 blob 跨代全等已证）。分类口径与改写前一致。

## Consistency Hub

| entity | canonical value/type | source of truth |
|---|---|---|
| 上游仓（只读） | `/home/user/Documents/DEV/chatbot`，ref=`master` | `upstream.toml [upstream]` |
| 同步基线 base | `6d93d919`（=faf9afcab 重写等价；波末推进） | `upstream.toml base` |
| 同步目标 target | `7bac20d2`（波开始时上游 HEAD，冻结） | `docs/upstream/upstream_state.json .upstream.target` |
| 目录映射 | frontend/src→app/src · backend/app→server/app · …（13 条） | `upstream.toml [[map]]/[[filemap]]` |
| 分类枚举 | in-sync/converged/ff-safe/local-mod/conflict/added-both/new-upstream/gone-*/local-only/excluded | `script/upstream_report.mjs classify()` |
| 本地补丁登记表 | DECISION_MATRIX §3（触碰同区域必 conflict） | `docs/DECISION_MATRIX_local_vs_upstream.md §3` |
| 不引入裁决 | D-109：上游 `DeviceExecutionPanel.vue` 及其 tab/import 接线不引入 | `tests/decisions.md D-109` + §3 SystemSettingsDialog 行 |
| 基线失败口径 | 4 既有环境性失败（test_memory_* real_db，PG 55433 未起）= D-112 | `tests/decisions.md D-112` |

## Task T0 — 基线重建 + 报告（SOP ①）

- **Files**：Modify `upstream.toml`（base + 改写事件注释）；Create `docs/upstream/UPSTREAM_SYNC_2026-10-24.md`、`docs/upstream/upstream_state.json`（工具产出）。
- **Interfaces**：Produces `upstream_state.json .files[upath]={local,category}` —— T1/T2 的唯一分类事实源。
- **Test seam**：`bash script/linux/upstream_report.sh --write` 退出 0 + counts 与 `git diff --name-only 6d93d919..7bac20d2`（378 件）勾稽。
- **Steps**：① edit upstream.toml base→6d93d919+注释 ② 跑报告 ③ 核对 counts（conflict/ff-safe/new-upstream 总和 vs 378 件域内增量）。

## Task T1 — 直采（SOP ②，ff-safe + new-upstream）

- **Files**：Modify/Create = 报告 `ff-safe`/`new-upstream` 件的 `local` 路径（app/** · server/** · tests/test_math_tokenizer.cjs）。
- **Interfaces**：Consumes T0 分类表；Produces 逐字节与 `7bac20d2:<upath>` 全等的本地文件。
- **Test seam**：采纳清单 `tests/upstream_adopt_w27.txt`（upath→sha 对账，`git hash-object` 全等率 100%）。
- **Steps**：① 过滤 D-109 不引入件 ② 逐件 `git -C /home/user/Documents/DEV/chatbot show 7bac20d2:<upath> > <local>` ③ sha 对账 ④ 清单落盘。

## Task T2 — 三方合并 + 双审计（SOP ③，conflict + added-both）

- **Files**：Modify = 报告 `conflict`/`added-both` 件的 `local` 路径；Create `tests/upstream_merge_audit_w27.md`。
- **Interfaces**：Consumes T0 分类表 + `git show 6d93d919:<upath>`（base 侧）；Produces merged 文件（语义=上游增量 ∪ 本地登记 hunk）。
- **Test seam**：每件 `git merge-file -p <ours> <base> <theirs>`；标记冲突手工裁决；双审计=① ours→merged 删除行逐条归宿 ② 对上游残差=§3 登记 hunk；冲突标记 grep 零残留 + 重复定义清剿（同名属性块/import 双形态）。
- **Steps**：① 逐件 merge ② 标记冲突裁决（§3 hunk 回植）③ CLEAN 件双审计 ④ 守卫测试跑绿（test_main_sync_wiring / test_main_device_executor_wiring / test_local_patch_imports / test_payload_stub_guard / test_config_resource_resolver / test_device_url_apiwrap / test_writable_runtime_paths / test_main_* 等）⑤ audit 文档落盘。

## Task T3 — 验证（SOP ④，COV-1 捕获环）

- **Files**：证据 `tests/upstream_sync_w27.log`、`tests/screenshots/*`、`tests/upstream_status_after_w27.txt`。
- **Interfaces**：Consumes T1/T2 后的 vendor 树；Produces 判据 #61-#64 证据。
- **Test seam**：server 全量 `../.localstack/venv/bin/python -m pytest tests/ -q`（对比基线 2804/4）· `npm --workspace app run test:unit` · `bash script/linux/project_build.sh` · `bash script/linux/check_hygiene.sh` · `bash script/linux/smoke_local.sh`（隔离配方 WEAVE_USER_DATA_DIR + PG 55434/后端 8162）+ 截图 model-native 判读。
- **Steps**：① 守卫+单件测试 ② server 全量 ③ test:unit ④ build ⑤ hygiene ⑥ smoke+截图判读（§A4.1.1：观察先行/逐判据/DOM·log 对质）。

## Task T4 — 收敛（SOP ⑤⑥）

- **Files**：Modify `upstream.toml`（base→7bac20d2）、`docs/upstream/UPSTREAM_SYNC_2026-10-24.md`（终态复跑）、`docs/upstream/UPSTREAM_TODO_*`（如有新上游缺口）、`tests/verification_log.md`、`memory/*`。
- **Interfaces**：Consumes T3 全绿；Produces 同步域清零报告 + 完成表证据。
- **Test seam**：复跑 `bash script/linux/upstream_report.sh`（base==target，conflict/ff-safe/new-upstream/added-both/gone-* 全 0）+ status 快照 before/after 对比（零新增/零我方写入）。
- **Steps**：① base 推进 ② 复跑报告 ③ status 比对 ④ 提交（描述性 commit）⑤ 记忆+索引 ⑥ final 断言。

## Plan self-review
- Coverage：判据 #59→T0 · #60→T1 · #61/#62→T2 · #63→T3①-⑤ · #64→T3⑥ · #65→T4；全覆盖。
- Placeholder scan：无 TBD/占位；每步带验证命令。
- Type consistency：`upath`/`local`/`category` 字段名与 `upstream_report.mjs` 输出一致；base/target 字符串在 Hub 单点定义。
