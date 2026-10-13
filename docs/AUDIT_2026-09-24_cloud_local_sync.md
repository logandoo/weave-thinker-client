# 云端-本地协同可行性评估 — weave-thinker（印象笔记式本地优先 + 可选多端同步）

- 日期：2026-09-24 · 类型：C4 只读评估 + S1 spike（零源码修改）
- 问题：当前架构能否做到"本地作业为主 + 云端多端同步（对话记录 + 笔记完全一致）+ 断网全功能"？大模型部署不在考虑内（断网用户默认有内网 LLM）。
- 结论速览：**可行，且现有架构异常适合。** 推荐 = 自研应用级同步（Joplin/Evernote 模式的 delta 同步器），**不采用第三方同步引擎**（PowerSync/ElectricSQL 的客户端目标是 SQLite，与本仓客户端的 PostgreSQL 后端栈不匹配）。断网全功能**已经成立**（本地栈实证全链路）。同步是增量能力，预计单人 2–4 周。

---

## 1. 现状盘点（证据）

### 1.1 部署形态（已就绪）
- 客户端 = Electron 壳 + SPA + **本地后端（Python/FastAPI）+ 内嵌 PostgreSQL 18 + pgvector**（侧车 `weave-server`，`desktop/local-stack.cjs`；本机已实证 register/login/记忆库全链路）。断网可用性：✅ 已成立（LLM 端点用户内网自理，按题设）。
- 服务端 = 同一份后端代码的远端部署（`server/` vendor 副本 ↔ chatbot 源仓）。
- **两侧同构**（同语言/同框架/同 schema）——应用级同步的天然温床（对比：Joplin 两端同 TypeScript）。

### 1.2 状态数据分布（server/app/db/database.py）
- **PG 35 张表**（`__tablename__` 清单：users / conversations / messages / notes / notebooks / assistants / memory_* 家族（episodes/concepts/clusters/recall_log/clarifications/dreams/llm_calls/subconscious）/ agent_tasks / scheduled_tasks / durable_jobs(3 表) / side_effect_ledger / export_tasks / web_search_results / user_sessions / user_skills / skill_files / user_asr_hotwords / user_model_providers / user_agent_states / chat_sessions / conversation_groups）。
- **同步就绪度**：全部 UUID 字符串主键（`database.py:23` 等；`id = Column(String(36), default=uuid4)`）→ 跨设备 ID 无碰撞 ✅；`updated_at` 覆盖 21 处 · `created_at` 34 处 · **`deleted_at` 0 处——当前全硬删除，同步必须补墓碑**。
- 关键形态（file:line）：
  - `Conversation`（:162）title/sort_order/group_id/deathmatch 状态，created_at+updated_at
  - `Message`（:237）role/content/reasoning/tool_results/tool_calls/context_info，**只有 created_at（无 updated_at）** → 追加型，天然近无冲突 ✅
  - `Note`（:605）title/content/raw_transcription + `Notebook`（:591）
  - `UserWorkspace`（:531）root_path 设备本地路径（**不同步，仅元数据映射**）· 工作区文件在文件系统（`main.py:132-135` audio/output 目录）
  - `Assistant`（:91）用户自定义智能体
- 邻接能力：导出/导入管线已存在（`api/export_tasks.py:18`），zip/单文件下载在 `api/files.py`——首批全量同步与备份可复用。

### 1.3 已有但不满足同步的原语
- durable jobs / SSE / 后台任务框架完备 → 同步 worker 可直接挂在现有后台任务框架上（`agent_scheduler/agent_worker` 已在本地栈实证启动）。
- 死磕状态/LLM 调用台账（side_effect_ledger、durable_job_*）属于运行态，**不应同步**。

---

## 2. 候选方案评估（≥2，含否决）

| 方案 | 机制 | 与本仓匹配度 | 结论 |
|---|---|---|---|
| **A. 自研应用级同步（Joplin 模式）** | 服务端 delta 端点（cursor 分页）+ push 批量 + 墓碑 + 内容寻址 blob；客户端同步 worker | 两端同构 Python/PG，无新基础设施，冲突策略自控 | **✅ 采纳（主方案）** |
| B. PowerSync | PG→客户端 SQLite + 上传队列 | **客户端目标是 SQLite**，与本仓客户端 PG 后端栈冲突；且后端写入路径仍要自己实现 | ❌ 否决（栈不匹配；且商业组件） |
| C. ElectricSQL（Shapes 读路径） | PG logical replication → HTTP → 客户端 | 只解决读路径，写路径仍自研；客户端目标 PGlite（WASM/JS），非我们的 Python 后端 | ❌ 否决 |
| D. Zero / Replicache（mutator/rebase） | 自定义 push/pull/poke 端点 + 客户端 rebase | 写路径仍自研，收益≈自研；面向 Web/React 客户端而非 Python 后端 | ❌ 否决 |
| E. CRDT（Yjs/Automerge）全文协同 | 文档级自动合并 | 对"并发改同一条笔记"最强，但引入 WS 协同基础设施 + 文档迁移 | ⏳ 二期可选（仅笔记正文） |

否决 B/C/D 的统一理由：本仓客户端**数据库是 PostgreSQL 而非 SQLite/浏览器存储**，第三方同步引擎的客户端落盘假设与我们的"客户端即完整后端"架构不匹配；它们的共同短板（写路径/冲突仍要自研）恰是我们自研方案的主要工作面，引入它们不省反而多一层。

---

## 3. 推荐架构（自研 delta 同步器）

```
设备 A（本机完整栈）                同步服务器（同一后端 + sync 端点）              设备 B（本机完整栈）
┌──────────────┐   push(Δ)      ┌──────────────────┐   delta?cursor=…   ┌──────────────┐
│ PG(本地全量)  │ ────────────→ │  sync_events 表   │ ←───────────────  │ PG(本地全量)  │
│ sync_worker  │ ←──────────── │  + blobs(sha256)  │ ───────────────→ │ sync_worker  │
└──────────────┘   delta(Δ)     └──────────────────┘    push(Δ)        └──────────────┘
```

### 3.1 服务端新增（部署形态：同一代码库的云端/内网实例）
| 端点 | 语义 |
|---|---|
| `POST /api/sync/auth` + `POST /api/sync/devices` | 云端账号认证 + 设备注册/撤销（设备 ID → sync bucket） |
| `GET /api/sync/delta?cursor=C` | 增量事件流（分页 + Joplin 同款压缩规则：update-update→update；create-delete→NOOP；create-update→create；update-delete→delete） |
| `POST /api/sync/push` | 批量提交本地变更（幂等；服务端仲裁） |
| `GET/PUT /api/sync/blobs/{sha256}` | 内容寻址文件传输（去重 + 断点续传 + 懒下载） |
| `POST /api/sync/resync` | 游标失效时的全量重建（Joplin `resyncRequired` 同款） |

服务端新增表：`sync_events`（append-only 变更日志，含压缩/保留策略）· `sync_tombstones`（删除墓碑）· `sync_devices`（设备注册）· `sync_blobs`（sha256 元数据）。**不改动现有业务表语义**（仅触发器/事件写入新增）。

### 3.2 客户端新增（vendored `server/` 副本内，不动 chatbot）
- `sync_worker`（挂现有 agent_worker 后台框架）：在线探测 → 每 N 分钟 delta 拉取 → 本地变更队列 push → 墓碑应用；断网自动暂停、复网恢复。
- `sync_state` 表：cursor / last_sync_at / device_id / per-entity sync_time。
- 幂等 upsert：以 UUID 主键 + updated_at 做 LWW；messages 追加型直接 insert-ignore。

### 3.3 同步域划分 + 冲突策略（核心裁决）

| 数据域 | 同步？ | 策略 | 备注 |
|---|---|---|---|
| conversations（元数据/分组/死磕状态） | ✅ | LWW（updated_at） | 死磕运行态字段（wall_time/turns 等）不同步 |
| messages | ✅ | **追加型 insert-only**（无 updated_at）→ 无冲突 | 删除随 conversation 级联 |
| notes / notebooks | ✅ | LWW；二期可选 Yjs 协同正文 | 并发双端改同一篇 → LWW 后写胜 |
| workspace 文件 | ✅ | sha256 内容寻址 blob（懒下载 + 断点续传） | 设备路径映射本地维护（UserWorkspace.root_path 不随 sync） |
| assistants / user settings（ui_preferences/昵称/头像） | ✅ | LWW | |
| memory（episodes/concepts/clusters） | ⏳ Phase 3 | **同步文本+元数据；嵌入向量每设备本地重算**（嵌入模型可能不同/维度不一致） | 或同供应商时直传向量 |
| agent_tasks / scheduled_tasks / durable_jobs / side_effect_ledger / chat_sessions / web_search_results / export_tasks | ❌ | 不同步（运行态/派生物/设备会话） | |
| user_sessions | ❌ | 设备本地认证 | |

### 3.4 断网能力（题设重点）
**已完全成立**：本地栈（本机实证）离线全功能——对话/笔记/记忆/工作区全部走本地后端+本地 PG；LLM 端点按题设由用户内网提供。同步是纯增量：离线时全部写入本地 + 队列，复网自动追平。

### 3.5 身份与多设备
- 云端账号（用户名/密码）⇢ sync bucket（按 user_id 隔离，PowerSync bucket 模型同理念）。
- 设备注册/撤销（sync_devices 表 + 配对码或登录式）。
- 隐私选项：自托管同步服务器（内网部署 = 同一安装包的服务端模式）→ 彻底数据自控；E2EE 列为 Phase 3+（Joplin 已有先例可循）。

---

## 4. 实施波次与工作量量级

| 波次 | 内容 | 量级 |
|---|---|---|
| S1 | 服务端 sync 四端点 + sync_events/tombstones/devices/blobs 表 | 3–5 天 |
| S2 | 客户端 sync_worker（离线队列/幂等/LWW/游标）+ 冲突策略 | 5–8 天 |
| S3 | blob 同步（sha256 寻址+懒下载+断点续传） | 2–3 天 |
| S4 | 冲突 UI 指示 + 设备管理 + 测试矩阵（断网/复网/双端互改） | 3–5 天 |
| S5 | 记忆同步策略（重算 or 直传）+ E2EE（可选） | 按需 |
| **合计** | **单人约 2–4 周**（一个完整功能波） | |

## 5. 风险与开放问题
1. **笔记并发双端编辑** LWW 会丢内容 → 二期 Yjs/Automerge 升级为正文级 CRDT（仅 NoteEditor 正文面）。
2. **嵌入维度跨设备不一致**（不同供应商/模型）→ 强制本地重算嵌入，或同步前校验维度。
3. **大文件/语音带宽** → 懒下载 + 断点续传 + 大小阈值策略。
4. **硬删除→墓碑迁移** → schema 新增 sync_tombstones（兼容迁移，行为默认关）。
5. **隐私** → 自托管云为主形态（内网）；官方云若做，需 E2EE 选项。
6. **multi-user 本地栈与云账号的映射**（一台机器多账号）→ 每个本地账号可绑定各自云账号（bucket 隔离）。

## 6. 结论
**可行，且是顺势工程**：现有"客户端=完整后端"架构让同步只剩"增量传输+冲突仲裁"一层；第三方同步引擎因客户端落盘假设（SQLite/浏览器）与我们的 PG 后端栈不匹配而不适用；推荐自研 delta 同步器（Joplin 生产验证模式），单人约 2–4 周可交付 MVP。断网全功能已证。

## 附录：本评估未覆盖
- E2EE 细节设计（密钥派生/主密钥轮换——Phase 3 另立）· 计费/配额（若官方云）· 服务端 sync_events 的保留/压缩策略调优 · 移动端同步的电池/后台限制（属移动端波次 W2/W3 后）· 并发编辑实测基准。
