# PLAN — W19 设备执行契约增量同步（client 端，2026-10-11）

> 执行者零上下文可读。规格源：`docs/DEVICE_EXEC_SYNC_W19.md`（S1–S4）+
> **已部署上游代码只读抽取**（`/home/user/Documents/DEV/chatbot/backend`，记忆教训 #11：
> 对接前以已部署代码为唯一事实源）。基线：W16b 交付树（server 全量 2183/0）。

## Consistency Hub（改名先改本表，再 grep 旧拼写至零命中）

| entity | canonical 值 | source of truth |
|---|---|---|
| 设备白名单 16 工具 | `workspace_read workspace_write workspace_edit workspace_glob grep diff word_count terminal process git execute_code skill_run_script provide_file provide_folder citation_render pdf_export` | 上游 `app/services/remote_dispatch_router.py:35-45` `DEFAULT_REMOTE_ALLOWED_TOOLS`；本仓镜像=`config.py client_exec_allowed_tools` |
| 白名单配置键 | `[client_exec] allowed_tools`（列表；显式空表=deny-all） | 主方案 §6.5（键拼写勿改）· `config.py:1524-1538` |
| fs/\* result 单包 | `{ok: true, data_b64: str, sha256: str}`（空文件 `data_b64:""`） | 上游 `device_session_service.py:255-287` `fetch_file_bytes` |
| fs/\* result 分块汇总 | `{ok: true, transfer_id: str, sha256: str, size: int, chunks: int}` | 同上 `_assemble_transfer`（读 ok/transfer_id/sha256/size） |
| chunk 通知 params | 恰 `{transfer_id, seq, data_b64, sha256, last}`（末块 last:true） | 同上 `_on_chunk`/`_assemble_transfer` |
| RPC 信封 | `{jsonrpc:"2.0", id, method, params}`；id ≡ call_id；params 不含 call_id | 上游 `rpc_call`（device_session_service.py:127-151） |
| 三新方法名 | `fs/zip` · `fs/office-html` · `fs/office-pdf` | 上游 `client_devices.py:318/339/355` |
| 三新方法 params | 恰 `{"path": "<workspace 相对>"}`（无 offset/limit/call_id） | 上游 `client_devices.py:330/351/368` `_device_fetch_bytes` |
| fs/zip 输入 | workspace 相对**目录**路径（上游 `validate_rel_path`：非空、无 `..`/`.` 段、无盘符/UNC） | 上游 `client_device_service.py:28-42` |
| office 错误码 | `office_preview_unavailable`（=本地 501 语义）· `office_preview_failed`（=本地 400/422 语义）· 路径类沿用 `workspace_path_invalid/outside/not_found` | 本仓/上游 `files.py` detail codes（**沿用不新造**）；错误串形状 `"<code>: <msg>"` = `device_executor_service._reply_call` 败路 |
| 目录 type | `"dir"`（fs/list 目录条目；文件条目仍 `_guess_type`） | PLAN_device_execution_mode §5（DEVICE_EXEC_SYNC_W19 §1 S3）|
| CHUNK_SIZE | 262144（256KiB；单包阈值=首读实况定路） | `device_executor_service.py:42` |
| 打包器 | `workspace_files_service.build_folder_zip(root, target_rel, dest)` → `{name, size, file_count}` | `workspace_files_service.py:128` |
| soffice 链路 | `office_preview_service.convert_to_pdf/html(src, *, soffice, cache_dir, timeout, max_cache_mb, cache_identity, cache_stat)` → `Path`；异常 `OfficePreviewUnavailable|OfficePreviewFailed` | `office_preview_service.py:1032/:1088`；配置键 `office_preview_*`（config.py:1552-1572） |
| TOCTOU 冻结 | `open_regular_nofollow`（O_NOFOLLOW fd）→ `spool_fd_to_temp`（前缀 `wt_office_src_`，与 `_prune_stale_spools` 同词表） | 现 `files.py:103/134`（本波移入 office_preview_service 共用） |

## Task 1 — S1 白名单镜像 +2 工具（4 处同源）

**Files**：Modify `server/app/core/config.py`（`client_exec_allowed_tools` 缺省列表）·
`server/config.toml` · `server/config.toml.example` · Test `server/tests/test_config_client_exec.py`
（`DEFAULT_ALLOWED_TOOLS` 钉）+ `server/tests/test_device_executor_service.py`（`exec_env` 夹具列表=同一 Hub 实体）。
**Interfaces**：Produces `cfg.client_exec_allowed_tools: list[str]`（16 名，序=Hub 序）。
Consumes：无（纯数据）。
**Test seam**：`server/tests/test_config_client_exec.py`（既有 4 点同源钉）+ executor 白名单闸行为腿。
**Steps**：
1. RED：`test_config_client_exec.py` 的 `DEFAULT_ALLOWED_TOOLS` 改 16 名（含 `citation_render`/`pdf_export`）→ 跑该文件见 FAIL（现缺省=14）。
2. GREEN：config.py 缺省 + config.toml + config.toml.example 各补 2 名（Hub 序末两位）→ 跑该文件 PASS。
3. `exec_env` 夹具 `allowed_tools` 同步 16 名；新增行为腿：`tools/call {tool:"pdf_export"}` 与 `{tool:"citation_render"}` 不再命中 `tool_not_allowed`（闸放行→工具自身回执），未知工具拒绝腿保持。
4. 验证：`cd server && ../.localstack/venv/bin/python -m pytest tests/test_config_client_exec.py tests/test_device_executor_service.py -v`。

## Task 2 — 抽函数共用（files.py soffice 链路 → office_preview_service）+ S2 三 RPC

**Files**：Modify `server/app/services/office_preview_service.py`（+`open_regular_nofollow`、`spool_fd_to_temp`、`convert_workspace_document`）·
`server/app/api/files.py`（office 两端点改调共用函数；`_safe_unlink`/`_frozen_response_path` 保留）·
`server/app/services/device_executor_service.py`（3 方法）·
Test `server/tests/test_office_preview_service.py`（共用函数腿）+ `server/tests/test_device_executor_service.py`（RPC 腿）。
**Interfaces**：
- Produces `async def convert_workspace_document(src: Path, *, kind: Literal["pdf","html"], soffice: str, cache_dir, timeout: int, max_cache_mb: int) -> Path` —
  冻结（nofollow fd→fstat→spool）→ convert_to_pdf|html（cache_identity=str(src)、cache_stat=fstat(mtime_ns,size)）→ finally 删 spool。
  异常：`OSError`（源打不开/非常规文件→调用方 404 / workspace_path_not_found）·
  `OfficePreviewPreparationError(OfficePreviewError)`（spool 失败→调用方 500 / internal_error）·
  `OfficePreviewUnavailable`（501 / office_preview_unavailable）· `OfficePreviewFailed`（422 / office_preview_failed）。
- Produces `open_regular_nofollow(path) -> int` · `spool_fd_to_temp(fd, suffix, cache_dir) -> Path`（自 files.py 迁入，签名不变）。
- Consumes：`build_folder_zip`（Task 1 无涉）、`_send_chunks`（W16b 既有，result 形状=Hub 行 3/4）。
**Test seam**：共用函数腿（真 soffice，`REAL_SOFFICE` skipif 仿既有 office 测试）+ executor RPC 腿（FakeCloud 真 websockets）。
**Steps**：
1. API.md 先行（§A4.7 Step 2）：设备执行器节补三方法契约 + 16 白名单 + 错误码表（本 PLAN Hub 为唯一取值源）。
2. doc↔code 一致性复审 1 遍（Step 3）：新文档字段 vs 上游 `client_devices.py`/`device_session_service.py` 逐字段核对（本波=新增面，无陈旧端点）。
3. RED（§A4.8，新行为腿必须先见失败）：
   a. `test_device_executor_service.py` 三方法腿（现 `method_not_found`）：fs/zip 单包+分块重组逐字节、fs/office-pdf %PDF 魔数、fs/office-html 表标记、四类错误串、随连接取消/绑定门/逃逸拒；
   b. `test_office_preview_service.py` 共用函数腿（现函数不存在：真 docx→pdf、真 xlsx→html、OSError/PreparationError/Unavailable/Failed 分型）。
4. GREEN：office_preview_service 迁入 2 助手 + 新增 `convert_workspace_document`（内部逻辑=files.py 现 331-367/423-459 逐行搬移，异常分型保持）；files.py 两端点改调共用函数（HTTP 映射 404/500/501/422 一字不改）；
   device_executor_service：`_session`/`_handle_message` 认 `fs/zip|fs/office-html|fs/office-pdf`（入 `_fs_tasks`）→ `_handle_fs` 增 zip/office 两支：
   zip=`build_folder_zip(ws_root, rel, tmp)` → `_send_chunks(tmp)` → finally 删 tmp；office=`convert_workspace_document` → `_send_chunks(产物)`；
   错误映射=Hub 行 11；路径闸=既有 `_resolve_in_workspace` + is_dir/is_file 分型。
5. 验证：`pytest tests/test_device_executor_service.py tests/test_office_preview_service.py tests/test_workspace_files_service.py -v` + 全量。

## Task 3 — S3 fs/list 目录 type 规范 `"dir"` + 本地 SPA 兼容键

**Files**：Modify `server/app/services/workspace_files_service.py`（WFS 目录条目 type）·
`app/src/composables/filePreview.ts`（`BACKEND_TYPE_KIND` 增 `dir: 'folder'`）·
Test `server/tests/test_workspace_files_service.py`（type 钉改 `"dir"`——`- test-change:` 登记）+ `app/tests/workflows/test_file_preview_kinds.cjs`（`classifyFile('x','dir')==='folder'` 增腿）。
**Interfaces**：Produces 目录条目 `{…, is_dir: true, type: "dir"}`（文件条目 type 不变）。Consumes：云端 `deviceUrls.ts DIR_TYPES`（首值 `dir`，已宽容）；本地 `FolderBrowserDialog`（走 `is_dir`，不受影响）。
**Test seam**：`test_workspace_files_service.py`（服务层钉）+ app 单测（classifyFile 兼容）。
**Steps**：
1. RED：`test_workspace_files_service.py:47` 钉改 `type=="dir"` → FAIL（现发 "folder"）。
2. GREEN：WFS 目录条目 `"folder"`→`"dir"`；SPA `BACKEND_TYPE_KIND` 增 `dir: 'folder'`（保留 `folder` 键=旧附件模型 `FolderAttachment.type` 仍用）。
3. app 单测增 `classifyFile('any','dir')==='folder'` 腿；验证 `bash script/linux/check_hygiene.sh` 或 app test:unit + server pytest 两文件。

## Task 4 — S4 API.md 补记（随 Task 2 Step 1 落）+ 终态验证

**Files**：Modify `server/API.md`（设备执行器节）。**Steps**：与 Task 2 Step 1 合并；收尾跑 server 全量 + app test:unit + `python3 tests/assert_artifacts.py --class CODE --final`。

## 自评（内联）
- 覆盖：S1=Task 1；S2=Task 2；S3=Task 3；S4=Task 2 Step1/Task 4；零改动项（vision/asr、审计、DEFERRED、W16b 面）=不触碰即保持。
- 占位符扫描：无 TBD/无「类似 Task N」/无未定义签名。
- 类型一致：`convert_workspace_document` 签名在 Task 2 Interfaces 定义、Steps 4 与测试同名同参；Hub 行 3/4 键集与 `_send_chunks` 现输出逐键一致（已核 device_executor_service.py:583-622）。
