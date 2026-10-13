# AUDIT 2026-10-03 — dsh-libreoffice-kit 对本项目 LibreOffice 压缩的适用性评估

> 类型：S1 Spike / C4 只读评估（零代码改动）· Verifier: direct read (non-web)
> 问题：DeepSeek 开源的 `dsh-libreoffice-kit` 能否有效帮助本项目进行 LibreOffice 的「压缩」。

## 1. 范围与方法

- 本项目侧：通读 office 预览链路（server → electron → 打包），收集体积事实。
- 上游侧：`upstream.toml` 映射判定同步域归属。
- 目标库侧：GitHub 仓库（`deepseek-ai/dsh-libreoffice-kit`）源码 + docs/packaging.md + npm 包页一手取证。
- 只读边界：未改任何产品代码/配置/脚本；写入仅限本报告、tests/acceptance.md、tests/verification_log.md、memory/。

## 2. 项目现状（证据）

| 事实 | 证据 |
|---|---|
| 预览转换 = Python 服务 spawn headless `soffice` 子进程（`--convert-to pdf` / `html`），私 profile、信号量 1、磁盘缓存 | `server/app/services/office_preview_service.py:932-955, 1001-1029` |
| 表格类主预览路径是 **Calc HTML 导出**（非 PDF），自带白名单 sanitizer + 列宽注入 | `office_preview_service.py:19-20, 1088-1158` |
| 自研 CJK 字体注入（生成 fonts.conf + 随包 NotoSansCJKsc + 别名/语言回退；v9 缓存版本） | `office_preview_service.py:785-915`，缓存版本 `:84` |
| macOS 打包把**完整** `/Applications/LibreOffice.app` `ditto` 整拷进 `dist_lo/`，**零裁剪** | `script/linux/package_macos.sh:144-147` |
| 打包接线：dist_lo → extraResources → local-stack.cjs 解析 soffice_path 注入 Python 配置 | `electron-builder.yml:34-35`、`desktop/local-stack.cjs:75-83` |
| **体积事实：`dist_lo/LibreOffice.app` = 804MB（解包）；DMG = 720MB；`dist_installer/mac-arm64` = 1.9GB** | `du -sh`（2026-10-03 实测） |
| 代码内已有先例裁定：参照 deepseek-harness `dsh-office-to-pdf`，"The Node kit is not portable to this Python stack" | `office_preview_service.py:3-5`（2026-09-19） |

## 3. dsh-libreoffice-kit 是什么（一手证据）

- `deepseek-ai/dsh-libreoffice-kit`，2026-09-30 开源（**仅 3 天**），MPL-2.0，117★，DeepSeek Harness 内部组件外放；两周内 0.0.1→0.1.5 共 9 个版本，API 高速演进期。
- 定位：**Node.js 库**（`@deepseek-ai/libreoffice-kit`，Node ≥22.19），Office→PDF/PNG 渲染 + xlsx 重算，附带 CLI `dsoffice`。
- 「压缩」的真实含义：**压缩的是它自带的精简 LibreOffice 引擎 payload，不是用户文档**。README「What was reduced, and why」：禁用 Java/Python/Base/桌面组件、静态链接、剪资源——macOS ARM64 引擎下载 98.85→60.49MB、解包 301→269MB。
- 引擎分发：`@deepseek-ai/libreoffice-kit-{darwin-arm64,darwin-x64,win32-x64,win32-arm64,wasm}` 预构建包（npm 公开 + GitHub Release XZ）；**Linux 只有 WASM**（native linux 配方仅为开发用途）。
- **引擎可执行文件是私有 helper `bin/libreoffice-kit`，docs/packaging.md 明文 "No system soffice is invoked"**——不是可用 `--convert-to` 调用的 soffice 二进制，无法从 Python 直接驱动。
- 格式矩阵（`packages/entry/src/operations.ts:6-9`）：
  - `doc/docx/odt → pdf/docx/odt/txt`
  - `xls/xlsx/ods → pdf/xlsx/ods/csv`
  - `ppt/pptx/odp → pdf/pptx/odp`
  - **无 HTML 输出；无 RTF 输入**（XSLT 导出滤镜已被剪除）。
- 字体机制：无内置字体；`fontDirectories`/`fontFallbacks`/`missingFonts` 显式控制（fontkit 读元数据、按字节传给引擎）——目标与本项目的 fontconfig 注入相同，思路现代。

## 4. 核心判定

**它能压缩的是「随包 LibreOffice 引擎」这一层（804MB → 269MB 解包，-67%），这正是本项目 DMG 720MB 的最大单一构成。方向对症。但它不是即插即用：**

1. **不能作 soffice drop-in**（无私有 soffice CLI；`bin/libreoffice-kit` 只能被其 Node API 驱动）→ 与本项目的 Python + soffice 子进程架构**栈不匹配**（与 2026-09-19 既有裁定一致，本次以新事实更新：kit 现已公开 + 预构建引擎）。
2. **两个功能硬缺口**：a) 无 HTML 导出 → xlsx/xls/ods 主预览路径（2026-09-21 波的核心交付）直接失效；b) 无 RTF 输入 → `SUPPORTED_OFFICE_EXT` 中 `.rtf` 失效。
3. **配套资产需重建**：v9 CJK 字体修复要在 kit 的 fontDirectories/fallbacks 模型下重做并重验证（kit 自述不保证与 MS Office 一致、missingFonts 非完整缺字报告）；xlsx fitToPage zip 补丁可保留（转换前处理），SinglePageSheets 滤镜选项无 API 出口，HTML 列宽注入随 HTML 路径一起失效。
4. **服务端（部署60，Linux）只有 WASM 引擎**——纯 CPU 模拟转换，服务端并发转换性能未证实。

## 5. 路线评估（≥2 方案 + 拒绝理由）

| 路线 | 内容 | 收益 | 代价/风险 | 裁决 |
|---|---|---|---|---|
| A. 迁移转换通路到 kit | Electron 主进程或独立 Node sidecar（≥22.19）包 JSON-RPC，Python 调用；word/ppt/表格全走 kit→PDF（前端已有 PDF 渲染），PNG 渲染可替换/增强缩略图 | DMG 预计 −180~250MB（需实测）；去系统 LO 依赖；字体 API 成熟 | HTML 预览需重建（表格退回 PDF 分页）或弃；RTF 丢失；CJK 重验证；改双端同源文件须上游协同；Linux 服务端 WASM 性能；0.1.x API 未稳 | **条件可行**——体积收益最大，工程量大 |
| B. 借其构建配方自建精简 LO.app | 用仓库 `engine/native/configure.mjs` + `scripts/slim-native.mjs` 本地构建，保留 soffice CLI | 理论同量级瘦身、架构零改动 | 配方与其私有 helper 绑定（去 launcher、静态链接、静态 UNO 注册），**不产出常规 soffice 安装**；等于 fork-and-maintain + LO 构建工具链 | **拒绝**——配方不可直接产出 soffice CLI |
| C. 本地裁剪完整 LibreOffice.app | package_macos.sh 在 ditto 后删语言包/字典/help/自带示例（保留 soffice 与 CJK 字体目录） | 估计 804→550~650MB，DMG −100MB 左右；**零架构改动、零功能风险** | 收益有限；每次 LO 升级要维护裁剪清单 | **低成本备选**——若目标只是缩 DMG，选这条 |

## 6. 归属裁定（2026-10-03 评估必答项）

- **换转换引擎（路线 A）**：改 `server/app/services/office_preview_service.py` → **双端同源（C 共享镜像）**（upstream.toml `backend/app → server/app` 命中；上游文件存在，本地含 v9 CJK 常驻补丁）→ 必须上游先行/双侧同改，本地单改必被同步波冲掉。
- **打包瘦身（路线 B/C）**：`script/linux/package_macos.sh`、`electron-builder.yml`、`dist_lo/` → **仅本地**（上游 chatbot 不打包 LibreOffice）。

## 7. 结论

**有条件有效。** dsh-libreoffice-kit 是目前唯一现成的「预构建精简 LibreOffice 引擎」来源，对本项目最痛的 804MB 引擎体积收益真实（解包 −67%）；但 (1) Python 栈不匹配、必须引入 Node 桥，(2) HTML 表格预览与 RTF 两个功能缺口，(3) CJK 修复需重建，(4) Linux 服务端仅 WASM，(5) 开源仅 3 天 API 未稳——五项成本决定了它不是"换个二进制"级别的瘦身。若不动预览架构只求缩 DMG，本地裁剪（路线 C）性价比更高。若未来愿意把 office 预览整体迁 Node 侧并接受 PDF-only 表格预览，kit 是正确的长期底座，建议等 0.1.x API 稳定窗口再启动（届时按双端同源协议上游先行）。

## 8. 未覆盖项（named）

- 转换保真度/CJK 实测：未安装 engine 包跑样张（评估只读边界 + 避免污染打包环境）。
- DMG 净缩量：未实测，-180~250MB 为按解包差的压缩比外推。
- Linux WASM 引擎在部署60 的转换吞吐：未测。
- Electron 随包 Node 版本是否 ≥22.19：未核对（Electron 主进程 Node 绑定其版本；独立 node 运行时需随包）。
