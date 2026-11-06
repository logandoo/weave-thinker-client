# UPSTREAM_TODO — 2026-10-29 W35 同步波 handoff

> 交付用户（chatbot 仓只读红线：本仓不代改上游，改动由用户/上游会话执行）。

## A. 上游应继续项（carry-forward，未销账）

- **T1（W28，未销账）**：上游 `backend/config.toml.example` 模板缺 F7 `max_consecutive_iterations = 60` / F4 `execute_code = 300` 改名值——模板生成的实例会静默失去 execute_code 停滞看门狗（D-108 同类）。本仓 `server/config.toml.example` 已按 D-118 修好；上游侧待用户排期。
- **T2（W30，未销账）**：升级面键迁移缺口（安装器升级路径的键迁移），详见 memory/fix_w28_upstream_sync_20261026.md W30 追加。

## B. 上游在途（登记不吸收，W33 tamper ledger 模式）

- 上游工作树未提交件（`tests/upstream_status_w35.txt` 全量在案）：`frontend/src/components/{ChatInput,LoginView,WysiwygEditor}.vue`（M）等 + `tests/deathmatch_feedback_*` 未跟踪会话产物 + `agent_skills` 嵌套仓 `.DS_Store`。并行会话在途，**本波不吸收**；下波 delta 以落提交为准。
- 上游波内 HEAD 恒 `477c4263`（零写入实证同上文件）。

## C. 本仓待办（非上游）

- **D-128 修法**：`server/app/services/cloud_link_service.py` `_login` 把代理/网关 5xx 误分类为 `LinkAuthError`（用户看到「凭据失效请重新绑定」，实为不可达）——建议 5xx/网关响应映射 `LinkUnavailable`（对齐 API.md「云端不可达→500」）+ `test_cloud_devices_cloud_unreachable_500` 测试缝确定性化（monkeypatch 注入 ConnectError）。行为语义变更，须独立评审波。
- **W35 观察**：窗口态探针 2 轴漂移（`tests/w35_stall_falsify_uistate.log`）：① menu toggleFullScreen 路径未生效（setfs 路径全绿）② 「顶栏条为系统黑条」像素断言对当前 macOS 失配（刘海条渲染变更嫌疑）。疑探针/断言对 OS 的漂移，建议立波修探针或按新 OS 行为更新断言口径。

## D. 本波销账

- 无新增上游 handoff（域内 22 件全同化，同步域清零：报告动作类全 0，`docs/upstream/UPSTREAM_SYNC_2026-10-29.md`）。

## E. 评审追加（W35 A4.9 fix-round）

- **T3（评审 A-Minor，上游自有行为 follow-up）**：`backend/app/services/deathmatch_workspace.py` `_read_pair` 接受绝对路径且无 workspace-containment 检查（同波上游新加的 `_workspace_realpath` 收紧 helper 未覆盖此处）——exact-file 判据可把宿主任意文件内容拉进验证上下文。**base 4cb33ddaa 同貌=上游意图非本波引入**；建议上游对 `_read_pair` 复用 containment（companion：`subprocess.run(cmd, shell=True)` gate runner 已有 CodeExecutionService 沙箱+N3 锚）。
- 评审材料自足性：`tests/review_w35.diff` 仅含 tracked diff；新件 `server/tests/test_deathmatch_r16_fail_closed.py`（untracked）内容以 `tests/w35_directcopy_reconcile.txt` hash 全等为准（评审 A 已独立复证）。
