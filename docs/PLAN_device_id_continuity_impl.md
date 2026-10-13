# PLAN.md — 重配对 device_id 连续性 + 构建世代可见性（下游待修清单，零上下文可执行）

> 依据用户下发清单 P0#1-3 / P1#4-5 / P2#6-7（2026-10-12 联调反馈）· Class: CODE · Lane: L

## Consistency Hub

| entity | canonical | source of truth |
|---|---|---|
| pair body 键 | `{code, name?, platform?, device_id?}`（device_id 仅非空时发） | device_pairing_service.register_with_cloud docstring 契约（本波闭合 doc↔code 漂移） |
| device_id 来源优先级 | `req.device_id` → 旧绑定记录（含 reset 墓碑）`device_id` → 空（首配） | 清单 #1-#3 |
| reset 语义 | **只清 token**（墓碑保留 device_id/身份键/cloud_url）；load_binding（凭据面）仍 fail-closed | 清单 #3「旧 device_id 须在 reset 前留存」 |
| 状态新键 | `build_id: str`（env `WEAVE_BUILD_ID` → `resource_root/BUILD_ID` → `"dev"`）· `impl_marker: "W20-device-identity"` · `cloud_username`/`cloud_user_id`/`workspace_ctx_expected`（墓碑面） | 清单 #4/#6 |
| 上游协同（不在本仓） | `register_device` 同 id 同属主重注册=同行换 token 保身份；异属主 403（现 500） | 清单 ⚠ 协同依赖 → upstream.toml handoff |

## T1 后端：device_id 连续性
**Files**：Modify `server/app/services/device_pairing_service.py`（register_with_cloud 带 device_id；reset 墓碑；`load_binding_record` 原始读取）· `server/app/api/client_exec.py`（PairRequest.device_id + 优先级链）；Test：`server/tests/test_client_exec_pairing.py`（扩展）。
**Steps**：1 RED（pair body 含 device_id 腿/复用旧绑定腿/reset 墓碑腿）→ 2 实现 → 3 GREEN `pytest server/tests/test_client_exec_pairing.py -q`。

## T2 后端：status 构建世代
**Files**：Modify `server/app/api/client_exec.py`（build_id/impl_marker 并入 status）· `script/linux/package_macos.sh`（打包时写 BUILD_ID=日期+git sha 进 dist_sidecar/_internal）；Test：status 形状腿（并入 T1 文件）。
**Steps**：1 RED（status 含 build_id/impl_marker）→ 2 实现（env→resource_root/BUILD_ID→dev）→ 3 GREEN。

## T3 前端：传 device_id + 身份展示 + 重配对提示
**Files**：Modify `app/src/api/clientExec.ts`（payload device_id + status 新键类型）· `app/src/components/DeviceExecPanel.vue`（handlePair 传 status.device_id；云端身份/名门期望值展示；配对成功提示）。
**Steps**：1 实现 → 2 `cd app && npm run test:unit` 全绿 → 3 `bash script/linux/project_build.sh`。

## T4 契约/登记
**Files**：Modify `server/API.md`（Pair 请求 device_id 语义/reset 墓碑/status 新键/换绑保 device_id）· `upstream.toml`（handoff：register_device 同 id 重注册语义 + repair 脚本退役条件）。
**Steps**：1 改写 → 2 read-back → 3 全量回归 + FCV。

## Plan self-review
Coverage：清单 7 项全覆盖（#1-3=T1/T3 · #4=T2 · #5=T4 · #6-7=T3）+ 协同项=handoff ✓ · Placeholder 无 ✓ · 类型一致（device_id: str 穿线一致）✓。
