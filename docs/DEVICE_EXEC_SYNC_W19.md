# client 端同步修改清单 — 设备执行契约增量（W18/W19，2026-10-10）

> 来源：chatbot 仓 W18 盲区补齐 + W19 P1 收尾波（主契约 `docs/PLAN_device_execution_mode.md` §5/§7）。
> 对照基线：本仓 W16b executor 实现现状（`server/app/services/device_executor_service.py` 等，
> 核对证据见本文件 §3）。**结论：3 项需改、1 项建议、其余 W16b 已满足。**

## 1. 必须同步（契约缺口）

### S1. 白名单镜像补 2 工具（缺口：citation_render / pdf_export）
云端远端白名单现为 **16** 工具（W18 G2 把 workspace FS 读写类补齐）：
```
workspace_read workspace_write workspace_edit workspace_glob grep diff word_count
terminal process git execute_code skill_run_script provide_file provide_folder
citation_render pdf_export          ← 新增
```
需改：
- `server/app/core/config.py`（CFG:1524-1538 默认列表）+ `server/config.toml:569-574`
  + `server/config.toml.example:665-670` 各加 `citation_render`, `pdf_export`
- 测试镜像钉（`server/tests/test_config_client_exec.py`）同步 16 名
- 提示：fail-closed 语义下漏配=云端这两个工具在设备会话被拒（安全但缺功能）；云端有
  漂移告警可对照（`remote_dispatch_router` 一次性 warning）

### S2. 新 RPC 方法 ×3（fs/zip · fs/office-html · fs/office-pdf）
云端 W19 起代理这三个端点到设备（前端 zip/office 已接线，缺设备侧实现会 502）：

| method | params | result（与 fs/download 同形） |
|---|---|---|
| `fs/zip` | `{path}`（目录，workspace 相对） | `{ok, data_b64\|transfer_id, sha256?, size?}` — zip 字节 |
| `fs/office-html` | `{path}`（xlsx/xls/ods 等） | 同上 — HTML 字节（表格预览） |
| `fs/office-pdf` | `{path}`（docx/pptx/…） | 同上 — PDF 字节 |

实现建议（零新依赖，全部现成）：
- zip：复用 `server/app/services/workspace_files_service.py:128 build_folder_zip`
  （本地 HTTP `files.py` 已在用），产物字节走既有 `data_b64`/chunk 通道；
- office：复用 `server/app/api/files.py:289/:377` 的 soffice 转换链路抽函数后共用
  （设备随包带 LibreOffice，`dist_lo/`）；
- 大文件走既有 chunk+逐块 sha256+`last` 通道（W16b 已实现，无需改）。

### S3. （核对项）fs/list 目录 type 字符串
本仓 `WFS:98` 目录 type=`"folder"`；云端前端归一已宽容接受
`dir/directory/folder/d/D/…` + `is_dir`（`frontend/src/utils/deviceUrls.ts` DIR_TYPES），
**现行为可用**。建议：后续改为契约规范值 `"dir"`（PLAN §5），避免双端各自宽容。

## 2. 建议同步

### S4. executor API.md 补记
`server/API.md:6700-6713` 仅记 6 方法——补 S2 三方法 + 白名单 16 名，避免下次核对误判缺口。

## 3. 已满足（W16b 实测核对，无需改）

- 线格式：jsonrpc 信封 / id≡call_id / `{ok,content}` / data_b64+chunk+`last` ✓
- `tools/cancel` 杀进程（terminal killpg / execute_code / process owner_id）✓
- `timeout_seconds` = max(本地, 请求) ✓ · heartbeat 通知 + 指数退避重连 + 401/1008 停等重配对 ✓
- 安全面：`_`-前缀哨兵剥离 · `super_admin_bypass` remote 强制失效（execution_context.py）·
  strict PermissionContext · workspace_ctx 严格名门 + 相对路径包含性 ✓
- skill_run_script 内联 `script`/`script_ext` 物化执行 ✓
- **vision/asr 混合（W19）零改动**：云端直接用既有 `fs/download` 取字节，网关调用留云端
- **远端审计（W19 P3）零改动**：纯云端表 `client_exec_audit`
- **DEFERRED 语义（W18 G2）零改动**：job_*/browser*/workspace_snapshot 本就不在镜像白名单，
  fail-closed 自动拒绝（云端同语义）

## 4. 既有待实测（本仓 memory 已记录，保持）
- 真实云端 `/api/device/ws` 集成联调 + 真实 WSS 证书链（现仅 scheme 映射单测 + ws:// 回环）
- 建议联调路径：chatbot 侧 `tests/w16_e2e_device_exec.py` 的假设备脚本可先做协议级对照，
  再换真 executor 连 `./scripts/start.sh` 起的本地云端跑同一 E2E 清单
