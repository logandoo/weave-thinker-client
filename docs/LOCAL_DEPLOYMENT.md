# 本地化部署（桌面端全栈自包含）

目标：用户安装后即获得**完整前端+后端+数据库+记忆存储**的个人多用户系统，零外部依赖（无需 Docker、无需系统 PostgreSQL、无需预装 Python——生产形态见 §4）。

## 1. 数据库部署方案对比（2026-09 检索结论）

| 方案 | 跨平台 | pgvector | 用户依赖 | 结论 |
|---|---|---|---|---|
| **PyPI `embedded-postgres` 轮子**（采纳） | macOS arm64/x64 · Linux x64/arm64 · Windows x64（PG 18.6 + pgvector 0.8.6，11–17MB/轮） | ✅ 内置 0.8.6 | 零（pip 依赖，随后端 venv/打包内置） | **主方案**：后端是 Python，天然契合；本机 macOS 已实证（PG 18.6 + vector 0.8.6 + HNSW） |
| npm `embedded-postgres`（zonky 二进制） | 同上 | ❌ 无 pgvector | Node 侧依赖 | 弃：pgvector 需自行编译分发 |
| `@boomship/postgres-vector-embedded` | 同上 | ✅ | GitHub Releases 分发（受限网络不可达）；自述"非生产级" | 弃 |
| Docker Compose（仓库既有 `pgvector/pgvector:pg16` + app 镜像） | 三平台（需装 Docker Desktop/Podman） | ✅ | Docker 安装 + 许可 | **备选/服务器形态**：企业部署与服务器场景保留；不作为桌面默认（安装门槛违背"安装即完整"） |
| SQLite + sqlite-vec | ✅ | 生态弱 | 零 | 弃（见 DATABASE_DESIGN D-4：pgvector 是记忆系统底座，迁移=重写检索层） |

## 1.1 导入既有部署配置（测试/迁移）

把既有可用的 `config.toml` / `config_model.toml` 直接用于本地栈（保留 LLM/搜索密钥，仅本地化
`[server]`/`[database]` 两段）：

```bash
python3 localstack/import_config.py \
    --source-config <既有>/config.toml \
    --source-model  <既有>/config_model.toml \
    --target server --force            # 或 --target .localstack/app（侧车/打包默认 home）
bash script/linux/localstack.sh backend-stop && bash script/linux/localstack.sh backend-start
```

安全：目标文件 0600 + gitignore（`server/config*.toml`、`LOCALSTACK_HOME/app/`）；工具只输出段级确认，不打印键值。
本机实证（2026-09-23）：registry 错误 0 · `/api/models` 9 别名 · embedding 探测 200 · 记忆子系统未禁用 · MCP 56 工具。

## 2. 本地方案架构

```
Electron (desktop/)
  └─ local-stack.cjs ──spawn──> script/*/localstack.sh start|backend-start|stop
                                  │
                                  ├─ PG: .localstack/venv (embedded-postgres 轮子)
                                  │     localstack/pg_server.py（initdb + pg_ctl 自管，
                                  │     127.0.0.1:55432，数据 .localstack/pgdata）
                                  └─ 后端: server/（vendor 副本）
                                        localstack/gen_config.py → server/config.toml
                                        uvicorn 127.0.0.1:8159（LOCALSTACK_PYTHON 指定解释器）
SPA (app/dist) ← VITE_API_BASE=http://127.0.0.1:8159（config.toml 注入）
```

- 数据目录：`.localstack/pgdata`（开发）/ 打包后移至用户数据目录（W-L2 完成）
- 认证：PG 仅监听 127.0.0.1 + 本机 trust（个人设备形态；多用户=同机多账号，账号隔离由后端承担）
- 记忆：向量检索走 PG pgvector（embedding 端点由 `server/config_model.toml` 配置，本地/远程皆可；未配置时 memory 子系统按既有逻辑降级禁用）

## 3. 运行手册（开发形态）

```bash
bash script/linux/localstack.sh start            # 启动 PG（embedded-postgres）
bash script/linux/localstack.sh psql "SELECT version()"
LOCALSTACK_PYTHON=<python-with-deps> bash script/linux/localstack.sh backend-start
bash script/linux/localstack.sh backend-health
bash script/linux/smoke_local.sh                 # 全栈冒烟（自动拉起/健康/截图/清理）
bash script/linux/localstack.sh backend-stop && bash script/linux/localstack.sh stop
```

工作流 trace（可复跑生成器）与卫生校验：

```bash
bash script/linux/localstack.sh start && bash script/linux/localstack.sh backend-start
.localstack/venv/bin/python tests/workflows/local_stack_auth.py   # 追加真实 HTTP trace（token 仅记长度）
# Windows: .localstack\venv\Scripts\python.exe tests\workflows\local_stack_auth.py
bash script/linux/check_hygiene.sh                                # 模式杀机械校验（CI sidecar.yml 同款步骤）
```

环境变量别名：`LOCALSTACK_HOME` 与 `WEAVE_LOCALSTACK_HOME` 等价（脚本/Electron 两处入口）。
旧路径（`logs/*.pid`）的遗留进程会被自动采用（adopted）并可正常停机；`backend healthy` 行含
`owner_verified=<pid>` 归属校验（监听者 PID 必须等于记录 PID，拒绝他人健康冒充）。

注意：trace 生成器使用 `httpx.Client(trust_env=False)` —— **本地流量绕过系统代理**。
macOS 的系统级 HTTP 代理会被 httpx 默认读取，导致后端停机时收到代理 502（而非 ConnectError），
干扰失败语义；curl 不读系统代理，故二者行为不同（2026-09-23 实测根因）。

本机实测证据：`tests/localstack/pg_vector_proof.log`（PG 18.6 + vector 0.8.6 + HNSW）、
`tests/localstack/backend_full_loop.log`（register→login→me + users 落库）、`logs/smoke_local.log`（Electron 本地模式 10/10）。

## 3.1 安全边界（个人设备假设，明示）

| 项 | 现状 | 风险 | 多用户/共享机建议 |
|---|---|---|---|
| PG 认证 | `initdb -A trust` + 仅 127.0.0.1 监听 | 同机任意账号可连（TCP 无密码；socket 目录 0700 受保护） | 改 md5/scram + 随机口令（写回 gen_config） |
| JWT secret | `gen_config` 随机生成，文件 0600，gitignore | 无（本地文件） | 保持 |
| `super_admin_bypass` | 模板默认 true（server/config.toml.example:14） | 本地首个账号即管理员 | 共享部署改 false 并配管理员 |
| CORS | 模板默认 `["*"]`（server/app/core/config.py:194-202） | 本地回环后端被恶意网页跨源调用 | 收紧为客户端 origin 白名单 |
| 桌面桥 | main.cjs：senderFrame 校验 + app bundle 写保护（评审修复波） | 无 | 保持 |

## 4. 生产打包（W-L2，本机侧车已实证）

**侧车形态**：`weave-server`（PyInstaller onedir，单二进制三分支）
- `weave-server pg start|stop|exec …` — PG 管理（embedded_postgres 随包收集）
- `weave-server gen-config` — 用户数据目录生成配置（JWT 随机，0600）
- `weave-server backend` — FastAPI 服务（配置经 `LOCALSTACK_CONFIG_PATH` 注入）

**已验证（macOS arm64，2026-09-23）**：`script/linux/build_sidecar.sh` → `SIDECAR OK`（392MB）；
`WEAVE_SIDECAR_BIN=… smoke_local.sh` → `sidecar mode · health OK · pass=true · localHealth=true`
+ 退出零残留（/tmp/smoke_side5.log）。CI 矩阵：`.github/workflows/sidecar.yml`（macos-14 / ubuntu-22.04 / windows-2022）。

**数据目录**：`LOCALSTACK_HOME`（打包后 = `userData/localstack`；`WEAVE_LOCALSTACK_HOME` 可覆盖）；
配置落 `LOCALSTACK_HOME/app/config.toml`；PG 数据在 `LOCALSTACK_HOME/pgdata`。已用 /tmp 自定义目录实证迁移。

**完整安装（无侧车时的降级路径，亦已实证）**：`localstack.sh backend-bootstrap` 一键装齐 venv 依赖；
此后零外部解释器即可运行全栈（/tmp/smoke_venv.log pass）。

**macOS 安装包 ✅（本机，2026-09-23；三方资产随包版）**：`script/linux/package_macos.sh` → `dist_installer/WeaveThinker-0.1.0-arm64.dmg`（**698MB**，SHA256SUMS 裸文件名可校验）；内置 FFmpeg9（GPL-2.0+，无 nonfree）/Chromium Headless Shell/LibreOffice 26.2（证据 tests/installer/bundle_proof.log，含 LO HTTP 端到端 http=200 %PDF）；DMG 挂载→拷贝→安装副本 `--smoke` 全绿、退出零残留；图标经 tracked `assets/icon.png` 复现。首启自动建于 `~/Library/Application Support/weave-thinker-client/localstack/`。

**未签名现实与绕过**：当前 `identity: null`（本地/内测可用）；下载态 DMG 可能被 Gatekeeper 拦——右键→打开，或 `xattr -dr com.apple.quarantine /Applications/WeaveThinker.app`。**分发需 Developer ID 签名 + 公证（W6）**。

**W6 剩余**：签名/公证流水线 · Windows（NSIS）与 Linux（AppImage/deb）安装器 · CI 侧车产物接 extraResources（矩阵产物回填）· 远端部署联调。Docker Compose 保留为服务器/备选形态（§1）。
