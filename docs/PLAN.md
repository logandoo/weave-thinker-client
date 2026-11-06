# PLAN — 2026-10-14 W23：上游对齐（chatbot 1f6c3d4e7→faf9afcab）+ Linux .deb 打包链（LINUX_DEB_UPSTREAM_FIXES 1–7）

来源：用户指令（两任务，顺序固定）。任务 A = 上游 chatbot 有更新（90 commits），按同步波 SOP 对齐；
任务 B = 按 `docs/LINUX_DEB_UPSTREAM_FIXES.md` 修改代码，范围经用户裁定为「完整落地打包链」：
缺陷 1–7 全修 + 新建 `script/linux/package_linux.sh` 与 `electron-builder-linux.yml`。
本机 macOS 不能交叉编译 Linux 侧车（PyInstaller 无跨平台）→ deb 构建验证面如实标注 UNVERIFIED。

## Consistency Hub

| entity | canonical value/type | source of truth |
|---|---|---|
| 可写运行时基址 | `config.backend_root`（=`config_path.parent`；打包态=`<userData>/localstack/app`，开发态=`server/`） | `server/app/core/config.py:251` |
| audio_files 目录 | `Path(config.backend_root) / "audio_files"` | LINUX_DEB_UPSTREAM_FIXES 缺陷 1/2 |
| output_files 目录 | `str(config.backend_root / "output_files")` | LINUX_DEB_UPSTREAM_FIXES 缺陷 1/4 |
| drain token 路径 | `os.path.join(str(config.backend_root), ".drain_token")`（0600 语义不变） | LINUX_DEB_UPSTREAM_FIXES 缺陷 1 |
| fonts 目录 | 保持 `os.path.join(os.path.dirname(__file__), "Fonts")`（只读随包资源，不迁 backend_root） | LINUX_DEB_UPSTREAM_FIXES 缺陷 1 注 |
| asr 录音目录 | `Path(get_config().backend_root) / "audio_files"`（与上同目录） | LINUX_DEB_UPSTREAM_FIXES 缺陷 2 |
| desktopName | `"WeaveThinker"`（根 `package.json` 顶层键） | LINUX_DEB_UPSTREAM_FIXES 缺陷 7 |
| productName / executableName | `WeaveThinker` | `electron-builder.yml` |
| syncDesktopName | `true`（electron-builder v26 开关；v27 起行为常开并移除该键——本仓 `electron-builder@^26.15.3` 需显式开启） | LINUX_DEB_UPSTREAM_FIXES 缺陷 7 + websearch（electron.build v26 linux 文档） |
| pyinstaller 版本 | `6.22.3`（`pip install pyinstaller==6.22.3` 固定，替换浮动安装） | LINUX_DEB_UPSTREAM_FIXES 缺陷 3 |
| pginstall 收集 | `--add-data "$EP_DIR/pginstall${SEP}embedded_postgres/pginstall"`（整树；SEP=`:` 除 Windows） | LINUX_DEB_UPSTREAM_FIXES 缺陷 3 |
| 上游同步目标 | chatbot `faf9afcab`（推进后即 upstream.toml `base`） | upstream.toml + docs/upstream/UPSTREAM_SYNC_2026-10-14.md |
| deb 产物 | `dist_installer/WeaveThinker-<version>-amd64.deb` | LINUX_DEB_UPSTREAM_FIXES 验证节 |
| vendor 路径映射 | 上游 `frontend/`→本地 `app/`、`backend/`→`server/`（合并后 grep 必查） | upstream.toml [[map]] + memory/fix_upstream_sync_wave_20261004.md |

## Task A1 — 直采 ff-safe + new-upstream（111 件）

**Files**：Modify — 41 ff-safe + 70 new-upstream 映射对（清单：`docs/upstream/UPSTREAM_SYNC_2026-10-14.md`）；无新文件类型，全部为 `server/`、`app/`、`server/tests/`、`app/tests/` 映射域内文件。
**Interfaces**：Produces — 与上游 `faf9afcab` 逐字节一致的直采文件；后续 A2/A3 依赖其为合并基件。
**Test seam**：报告复跑（分类计数）+ `rg -n "frontend/|backend/" <直采件>` 布局 grep（命中即按惯例改路径不改断言，测试文件转 local-mod 并登记）。
**Steps**：
1. 对每个 ff-safe/new-upstream 对执行 `git -C /home/user/Documents/DEV/chatbot show faf9afcab:<from> > <to>`（字节级写入）。验证：`bash script/linux/upstream_report.sh` 两分类归零。
2. 直采件布局 grep `frontend/|backend/`（排除注释文案），命中改为 `app/`/`server/` 相对路径。验证：`rg -n "(\.\./)*frontend/|(\.\./)*backend/" server/tests app/tests | wc -l` 趋零（历史既有个案除外，逐条记录）。
3. ours→merged 删除行审计（A2 一并做）前置：直采前保存 `git stash create` 无必要——直采不触碰 local-mod 件。

## Task A2 — 28 个 conflict 件三方合并（保留 §3 登记 hunk）

**Files**：Modify — 冲突清单 28 件（API.md、conversation.py、skins.py、config.py、database.py、migrations.py、voice_service.py、registry.py、skill_tools.py、main.py、app/package.json、chat.ts、workspaceFiles.ts、ChatInput.vue、FileAttachment.vue、FilePreviewDialog.vue、FolderAttachment.vue、FolderBrowserDialog.vue、MessageBubble.vue、NotebooksList.vue、NotesList.vue、SystemSettingsDialog.vue、WysiwygEditor.vue、useMarkdown.ts、stores/chat.ts、三主题 css）。
**Interfaces**：Consumes — A1 直采结果（同域邻件）；Produces — 合并后本地树（本地 hunk 全保留 + 上游新性全进）。
**Test seam**：守卫测试（`server/tests/test_main_sync_wiring.py`、`test_main_device_executor_wiring.py`、`test_local_patch_imports`、`test_payload_stub_guard`、`test_config_resource_resolver.py` 本地腿）+ 双审计输出落 `tests/upstream_merge_audit_w23.md`。
**Steps**（逐件重复，件件验证）：
1. 提取 base：`git -C /home/user/Documents/DEV/chatbot show 1f6c3d4e7:<from> > /tmp/w23/base`；ours=本地现件；theirs=`git show faf9afcab:<from>`。
2. `git merge-file ours base theirs` → 解冲突标记；冲突区裁决规则=DECISION_MATRIX §3 登记 hunk 优先保留（本地语义），其余按「以上游为准」取上游。
3. 审计 a（ours→merged 删除行）：`diff <(sed ...) 本地旧件` 找出被合并静默删除的本地行——每一删除行必须能指认是登记 hunk 的故意替换还是吞特性（吞特性=回植）。
4. 审计 b（对上游残差）：`diff <(git show faf9afcab:<from>) <合并件>` 残差只应剩本地登记 hunk（+已知 vendor 路径改写），逐条指认。
5. 布局 grep 同 A1-2。验证命令：`cd server && ../.localstack/venv/bin/python -m pytest tests/test_main_sync_wiring.py tests/test_main_device_executor_wiring.py tests/test_config_resource_resolver.py -q`。

## Task A3 — 测试全绿 + base 推进 + 报告清零

**Files**：Modify — `upstream.toml`（base→faf9afcab）、`docs/upstream/UPSTREAM_SYNC_2026-10-15.md`（复跑报告）、merge-induced 测试修复件（若有，test-change 登记）。
**Interfaces**：Consumes — A1+A2 树；Produces — 同步域清零的交付树。
**Test seam**：`bash script/linux/project_build.sh` + `cd server && ../.localstack/venv/bin/python -m pytest tests/ -q` + `npm --workspace app run test:unit` + `bash script/linux/upstream_report.sh`。
**Steps**：
1. 三件套跑绿；失败→归因（合并错误=修代码；上游自有缺陷=三类分野处置并记录），`diagnosis:` 入 log。
2. `upstream.toml` base=`faf9afcab`；复跑报告确认 conflict/ff-safe/new-upstream/added-both/gone-*=0。
3. 提交同步波 commit。

## Task B1 — RED：可写路径回归测试（A4.8）

**Files**：Create — `server/tests/test_writable_runtime_paths.py`。
**Interfaces**：Produces — 4 个测试：`test_asr_audio_dir_uses_backend_root`（monkeypatch config_path→tmp，`ASRService.get_audio_files_dir()` 应为 tmp/audio_files）、`test_main_startup_dirs_use_backend_root`（main.py 源中断言 audio/output/drain 三路径以 `config.backend_root` 为基、Fonts 保持 `__file__`）、`test_export_and_browser_output_dir_use_backend_root`（两服务源断言 output_files 基址=backend_root、`dirname(__file__)` 派生模式绝迹）、`test_dev_mode_paths_unchanged`（开发 config 下 backend_root/audio_files==server/audio_files，钉「开发态路径不变」契约）。
**Test seam**：`cd server && ../.localstack/venv/bin/python -m pytest tests/test_writable_runtime_paths.py -q`。
**Steps**：
1. 写 4 测试并运行 → asr 腿 + 源断言腿 **RED**（现码 `dirname×3`/`dirname×2` 模式）→ 失败输出逐字入 log。
2. 不改测试进入 B2。

## Task B2 — 缺陷 1/2/4（server 写盘路径）+ 5/6（卫生/文档）

**Files**：Modify — `server/main.py`（startup_event）、`server/app/services/asr_service.py`（get_audio_files_dir，删无用 `import os` 若不再用）、`server/app/services/interactive_browser_service.py`（619/665 两处）、`server/app/services/export_worker.py`（128）、`.gitignore`（+.cache/）、`README.md`（打包段+Linux）。
**Interfaces**：Consumes — Hub「可写运行时基址」行；Produces — B1 测试 GREEN。
**Test seam**：B1 测试 + `cd server && ../.localstack/venv/bin/python -m pytest tests/ -q`。
**Steps**：
1. 按文档补丁逐件修改（补丁原文为准；字体目录不动）。
2. B1 测试 GREEN + server 全量零回归。

## Task B3 — 缺陷 3（build_sidecar.sh pginstall 整树 + pyinstaller 钉版）

**Files**：Modify — `script/linux/build_sidecar.sh`。
**Interfaces**：Consumes — Hub「pyinstaller 版本」「pginstall 收集」行；Produces — Linux 侧车含 80 个 PG 扩展 .so（macOS 侧同款断言见 B5 待验清单）。
**Test seam**：bash 语法闸（`bash -n`）+ 本机 macOS 构建冒烟（侧车仍可建，收集面含 pginstall 断言脚本钩子）+ `tests/sidecar_pginstall_assert.sh`（构建后断言 `dist_sidecar/weave-server/_internal/embedded_postgres/pginstall/lib/dict_snowball*` 在场——macOS 环境同样适用）。
**Steps**：
1. 应用文档 diff（钉 `pyinstaller==6.22.3`、EP_DIR 解析、`--add-data` 整树、缺 pginstall 即 fail-fast）。
2. `bash -n script/linux/build_sidecar.sh` + 实跑 `bash script/linux/build_sidecar.sh` 并断言 pginstall 树在产物中。

## Task B4 — 缺陷 7 + 打包链（electron-builder-linux.yml + package_linux.sh）

**Files**：Modify — `package.json`（+`"desktopName": "WeaveThinker"`）；Create — `electron-builder-linux.yml`、`script/linux/package_linux.sh`。
**Interfaces**：Consumes — `electron-builder.yml`（macOS 版结构参照：appId/productName/files/extraResources/sidecar 映射）、`script/linux/build_sidecar.sh`（B3）、`script/linux/project_build.sh`；Produces — `dist_installer/WeaveThinker-<ver>-amd64.deb`（Linux 上）。
**Test seam**：`python -c "import yaml…"` 配置解析闸（或 node smol-toml/yaml 等价）+ `bash -n` + `node -e` 探针（package.json desktopName 在场断言）+ electron-builder schema 校验（`npx electron-builder --help` 不验 schema；用配置加载尝试 `npx electron-builder --linux deb --dir` dry 在 macOS 会走 mac 路径——故本机只做静态断言，实跑入待验清单）。
**Steps**：
1. `package.json` 增 desktopName（顶层，与 workspaces 平级）。
2. 写 `electron-builder-linux.yml`：linux.target=deb、category=Utility、icon=build/icon.png、**`syncDesktopName: true`**（注释注明 v26 语义/v27 移除）、沿用 electron-builder.yml 的 appId/productName/files/extraResources 侧车映射。
3. 写 `script/linux/package_linux.sh`：对齐 package_macos.sh 四段流程（SPA→侧车→BUILD_ID→electron-builder `--config electron-builder-linux.yml` deb），产物 `dist_installer/`，日志 `logs/package_linux.log`；注释声明目标机依赖（系统 git/ffmpeg 或后续随包，deferred）与非 root 安装验证方法（引 LINUX_DEB_UPSTREAM_FIXES 验证节）。
4. 静态断言：desktopName/syncDesktopName 在位（守卫并入 B1 测试文件或独立 `tests/test_linux_packaging_config.py`）。

## Task B5 — 终验 + 双评审（Lane L §V4）+ FCV

**Files**：Modify — 评审修复件（若有）；Create — `tests/coverage_matrix.md`、`tests/fcv_brief.md`、`tests/fcv_report.md`、`tests/upstream_merge_audit_w23.md`。
**Steps**：
1. 全量三件套 + `bash script/linux/check_hygiene.sh`。
2. 双对抗 fresh-context 评审（git diff 83c0600..HEAD）→ 收口 Critical/Important → scoped re-review。
3. FCV brief → 子代理独立复跑 → `Fresh-verify: pass`。
4. memory（A7.9）+ Memory Gate（A7.10）+ 完成输出（A4.4）。

## Plan self-review

1. Coverage — 判据 #27–#30→A1/A2/A3；#31/32→B1/B2；#33→B3；#34→B2；#35→B2；#36/37→B4；#38→B5。全覆盖。
2. Placeholder scan — 无 TBD/待定；「deferred」项（git/ffmpeg 随包、config.data_path() 收敛、Linux 实机 deb 构建）显式出表为待验/建议项而非隐藏。
3. Type consistency — Hub 行内路径拼写在 B1 测试与 B2 实现间一致（`backend_root / "audio_files"`、`backend_root / "output_files"`、`.drain_token`）；`syncDesktopName` 拼写一致。
