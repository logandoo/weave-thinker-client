# Weave Thinker 客户端 — 项目实现方法说明（架构与上游同步原理）

> 面向读者：需要理解「这个客户端到底是什么、数据在哪、为什么要做上游同步」的使用者与协作者。
> 本文所有主张均可在本仓代码中找到对应文件：引用格式 `文件:行`；关键行为有测试/冒烟证据（tests/verification_log.md 各波在案）。

## 1. 一句话定义

**Weave Thinker 客户端是一套完整本地应用**（Electron 桌面壳 + 完整 Vue SPA + 本地 FastAPI 后端 + 本地 PostgreSQL），与云端产品（chatbot 仓）是**同一产品的两端**：功能与修复主要在云端仓开发，按「同步波」移植进本仓（本仓为自包含 vendor 副本，不修改上游仓）。

## 2. 访问模型：不是网页壳，是本地全栈实现

打开 DMG 安装后的 App，运行时发生的事：

1. **UI 完全本地**：Electron 壳以自定义协议 `app://` 从包内 `app/dist` 供给 SPA（`desktop/main.cjs:4,94-130`；SPA fallback 支持 history 路由；`/api` 路径绝不回退 HTML——防止相对调用被 index.html 静默吞掉返回 200）。
2. **后端完全本地**：启动时 spawn 随包 sidecar `Resources/sidecar/weave-server`（PyInstaller 冻结的完整 FastAPI 服务，`desktop/local-stack.cjs:49`），并拉起 embedded PostgreSQL（数据目录在用户 profile，默认端口 8159/55432，**带端口协商与 owner 校验**——`desktop/local-stack.cjs:198,208,272-279`）。
3. **前端找后端的路径**：SPA 经 `VITE_API_BASE` 调本地后端（构建期由 `config.toml → script/sync_config.mjs → app/.env` 注入；运行期按协商端口注入）。
4. **能力编排在本地执行**：聊天、笔记、工作台、技能/工具注册、MCP、终端、git（随包内置源码编译版）、浏览器自动化（随包 playwright chromium）、ffmpeg、语音通路的编排与音频闭环、文件预览（LibreOffice 为可选随包件）——进程与数据都在**本机**；其中**识别/合成等模型推理调用云端 API**（ASR=DashScope、TTS=MiMo、LLM/嵌入=所配置服务商，默认云端；经 `config_model.toml` 可指向自建/内网服务）。
5. **数据在本地**：用户数据落本地 PostgreSQL；云端只是同步副本。
6. **云端参与的部分**：LLM/嵌入/ASR/TTS 推理默认走云端 API（需要网络；可配置内网服务）；多端一致性经 `sync_client`（`server/app/services/sync_client.py`）周期性与云端做事件级双向同步。「断网可用」的实际范围=界面/本地数据/本地工具/编辑与同步队列照常，**依赖云推理的功能（对话推理/语音识别合成/嵌入）离线时降级或暂不可用**；断网期间的记忆嵌入待联网后云端生成（产品根本原则 memory/product_mandate.md）。

### 与「直接访问某个网页」的区别

| 维度 | 纯网页壳（套壳浏览器） | 本项目 |
|---|---|---|
| UI 来源 | 远端网页 | **包内 app/dist，本地供给（app://）** |
| 后端/数据 | 全在远端 | **本地 sidecar + 本地 PostgreSQL** |
| 断网 | 基本不可用 | **本地面完整可用**（界面/数据/工具；云推理与云同步降级） |
| 本地工具（终端/git/浏览器自动化/ffmpeg/文件处理） | 不可用 | **本地子进程真执行** |
| 多端一致 | 服务端天然一致 | **sync_client 事件同步保证同账户全端一致** |
| 移动端 | 另一个网页 | Capacitor 复用**同一份 SPA**（`app/`，平台差异收敛到 `app/src/native/bridge.ts`） |

## 3. 为什么需要「上游同步」，机制是什么

### 3.1 源仓关系（为什么不是各写各的）

- `chatbot` 仓 = 云端 Web + 服务端（产品的正源实现）。
- 本仓 = 在其之上做**多端移植**（评估报告方案 A）：UI 复用 ≈100%、共享后端模块以 vendor 副本形式镜像（README.md「结构」节）。
- 共享域映射表由 `upstream.toml` 维护：8 个目录（`frontend/src↔app/src`、`backend/app↔server/app`、`backend/tests↔server/tests` 等）+ 11 个单文件（`main.py`、`package.json`、配置模板等）。
- **上游一直在演进**（修 bug/加功能），而这些功能大多属于共享域 → 必须周期性把上游增量搬进来；同时本仓有自己的平台适配补丁（桌面壳、同步客户端接线、随包件、打包流水线），不能简单整仓覆盖。

### 3.2 同步波（每轮怎么做）

1. **三方分类报告**（`script/upstream_report.mjs`，上游仓只读）：对 `base..target` 的每个映射文件做 base/本地/上游 三方字节比较，产出类别：
   - `ff-safe`（本地未动、上游更新）→ **逐字节直采**（`git show target:path`）
   - `new-upstream`（上游新增）→ 直采
   - `conflict`（双方都改）→ `git merge-file --theirs` 三方合并（用户裁定：冲突以上游为准）
   - `local-mod`/`local-only`（仅本地）→ 保留
   - `converged`/`in-sync` → 无需动作
2. **假干净审计**（本项目最重要的工程纪律，多波实证）：
   - *ours→merged 删除行审计*：--theirs 会「自动消化冲突」，把本地新增整段替换且 rc=0——必须逐件核对被删行；已抓出过的形态：apiUrl import 被吞（运行时 ReferenceError）、⌘S 契约整块丢失、test:unit 两入口回潮、`sync_workspace_api` import 别名被吞（收集期 NameError）。
   - *对上游残差审计 + 布局 grep + 标记矩阵*：确认保留面（登记表补丁）与新增测试的 vendor 布局适配。
3. **契约回归**：构建 + app 单测 + server 全量 + UI 契约套件（⌘S）+ smoke 12 项 + DMG 打包 + 安装副本 scratch 冒烟。
4. **基线推进 + 清零复跑**：`upstream.toml` 的 `base` 推进到当波 target，复跑报告确认同步类目清零；上游工作树零写入（快照比对）。
5. **归属判定**（`docs/DECISION_MATRIX_local_vs_upstream.md`）：任何改动先问「仅本地 / 仅上游+部署 60 / 双端」：
   - A 类 client-only（桌面壳、本地同步接线）→ 只在本仓改
   - B 类红线（同步事件语义 `sync_apply/sync_capture`）→ 必须双端同改、逐字节移植
   - C 类共享镜像（其余 frontend/backend 文件）→ 按语义归属，冲突按「以上游为准」
   - D 类文档/构建 → 各自仓内

### 3.3 为什么这么重（价值）

同一产品两端演进，最大的风险不是「搬不过来」，而是**搬错**：静默覆盖本地平台补丁、半合并出死代码、上游自身缺陷被当成本地回归。于是本仓把「同步」做成有证据链的工程流程：分类器 → 直采/三方合并 → 双审计 → 契约测试 → 冒烟/打包实证 → FCV 独立复核，且维护一张**常驻本地补丁登记表**（chat.ts 的 `apiUrl` 包装与载荷双保险、voice 保活 SSOT、`main.py` 同步接线、config 的同步属性、payload 路由守卫测试等）——每次同步波对照此表逐项核销。

## 4. 运行时拓扑（示意）

```
[Electron 壳 desktop/]
   ├─ app:// → app/dist（Vue SPA，本地供给，离线可跑）
   ├─ IPC Bridge（原生能力：对话框/剪贴板/系统菜单/窗口探针…）
   └─ local-stack.cjs
        ├─ spawn sidecar: Resources/sidecar/weave-server（FastAPI 冻结二进制）
        ├─ embedded PostgreSQL（数据目录=用户 profile；端口协商+owner 校验）
        └─ 随包件接线：git / ffmpeg / playwright chromium /（可选）LibreOffice
                    ▲
        SPA ──HTTP──┘（127.0.0.1:<协商端口>，VITE_API_BASE）
                    │
[本地 sidecar：工具/技能/MCP/语音/文件处理/agent 全在本机执行]
                    │ sync_client（事件级双向同步，30s 周期/唤醒即跑）
                    ▼
[云端 chatbot / deploy-60]（账户数据权威副本；LLM/嵌入推理 API）
```

## 5. 交付与验证

- **交付物**：macOS DMG（`script/linux/package_macos.sh`）：Electron + sidecar + git/ffmpeg/playwright chromium 随包（LibreOffice 可选）。未签名=本机/内测可用；对外分发需签名+公证。
- **证据协议**：每次打包后做「安装副本 scratch 冒烟」——挂载 DMG → 拷出 → 隔离 profile/端口启动 → 12 项检查（mounted/bridge/weaverCompat/read/wrote/listed/apiFallback/traversal403/localHealth/runtimeBase/runtimeReachable/screenshot）+ 截图判读 + 侧车 `SyncClient started` 日志（tests/installer/pack_macos_proof_*.log）。
- **多端构建**：Windows/Linux 脚本在 `script/windows|linux/`（pid 文件生命周期，host-safe 停栈）；移动端为 Capacitor 脚手架（`mobile/`）。

## 6. 常见问题

- **「看起来像浏览器打开了一个网页？」** 界面是本地资产（app:// 供给），不是远端页面；断网时界面/数据/本地工具照常（云推理类功能除外）。
- **「为什么修复要在两个仓库做？」** 同一功能可能同时属于云端语义（上游）与本地运行时（本仓）；B 类红线要求两端逐字节一致，C 类按归属。上游修好后通过同步波进本仓（或收敛/直采）。
- **「上游缺陷怎么办？」** 上游仓只读。发现上游自身缺陷（或不自洽）时：本地不发明分叉，登记到 `docs/upstream/UPSTREAM_TODO_*.md` 交付上游会话修复，下波同步归位（本项目已实证多轮：清单→上游实现→converged/超集替换）。
