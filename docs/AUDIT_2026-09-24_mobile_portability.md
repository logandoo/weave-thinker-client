# 移动端（iOS/Android）可移植性评估 — weave-thinker

- 日期：2026-09-24 · 类型：C4 只读评估 + S1 spike（零源码修改）
- 问题：移植到 iOS/Android 能否完成类似任务？"毕竟手机端没有 PostgreSQL"。
- 结论速览：**能完成"类似任务"（一致体验：浏览/编辑/同步/离线读写缓存），但定位是"同步客户端"而非"同款本地栈"。** 误区澄清：手机端无**嵌入式原生 PG 二进制**（embedded-postgres 轮子仅 macOS/Linux/Windows），但存在 **PGlite（WASM 完整 PG，含 pgvector，<3MB gz）** 可在移动 WebView 运行。全离线同款栈（本地 LLM+全代理链）在手机端**不建议**（成本/内存/功耗），且题设用户断网时走内网 LLM，手机此时连内网后端即可全功能。

---

## 1. 误区澄清：手机端的"PostgreSQL"

| 说法 | 事实（2026-09 检索） |
|---|---|
| "手机没有 PG" | **嵌入式原生 PG 二进制无移动版**——`embedded-postgres`（我们桌面用的轮子）仅 `manylinux/macosx/win_amd64`（PyPI 18.6.3 页）。Termux 有 PG 包但不可嵌入应用 |
| 手机上能跑 PG 吗 | **能——PGlite**：WASM 版完整 PostgreSQL（单用户模式编译，<3MB gzipped，IndexedDB/OPFS/文件持久化），**官方支持 pgvector**（pglite.dev/docs/about；github.com/electric-sql/pglite） |
| 有实证吗 | Majid Aljishi 实录：**Expo(RN) + PGlite + pgvector** 端侧语义搜索，10k 向量 HNSW 检索 <15ms；Routier 插件页给出 iOS 注意点（Safari/Apple WebKit 用 `idb://` 而非 OPFS——Capacitor WKWebView 同源约束适用） |

## 2. 分层可行性（四类"类似"分别裁决）

| 层级 | 内容 | 可行性 | 依据/代价 |
|---|---|---|---|
| L1 **同款本地栈**（Python FastAPI 后端 + 嵌入式 PG 二进制） | ❌ **不可行** | 无 iOS/Android PG 二进制；Python 嵌入移动=Chaquopy/BeeWare 级重工程且 PG 仍无移动构建 |
| L2 **PG 兼容本地数据层**（PGlite + pgvector 于 WebView/JS） | ✅ 可行，但**需 JS 后端层**（现有 FastAPI 为 Python，不能进 WebView；要么重写裁剪版 JS 后端，要么用移动专用后端） | 独立工程：同 schema 不同实现；向量检索可本地（实证 <15ms@10k） |
| L3 **同步客户端**（衔接上波云-本地协同） | ✅ **推荐形态** | Capacitor SPA（复用已有脚手架 `app/capacitor.config.ts`）+ 本地缓存（PGlite 或 Capacitor SQLite）+ delta 同步到云端/内网后端（端点设计见 docs/AUDIT_2026-09-24_cloud_local_sync.md §3.1） |
| L4 **全离线同款**（PGlite + llama.cpp mobile + 本地 embedding） | ⏳ 理论可行，不建议 | 后端重写为 JS + 移动 LLM 推理内存/速度受限；题设断网=内网 LLM，手机直连内网后端即全功能（无需 L4） |

## 3. 推荐形态（L3，与云-本地协同衔接）

```
桌面端（全功能本地主机）                同步服务器（云端/内网）
  PG 全量 + 全代理链  ←→ delta 同步 ←→  PG 权威副本
                                          ↑ delta 同步
手机端（Capacitor SPA + PGlite/SQLite 缓存 + 同步 worker）
  离线：读+写缓存（笔记/对话可浏览可编辑可新建）
  在线/内网：连内网后端 → 全功能（LLM/代理/工具）
```

- **数据一致性**：桌面与手机经同一 sync 协议收敛（conversations/messages/notes LWW + 追加消息无冲突 + blob 懒下载）；嵌入向量**本地各自重算**（维度/模型可能不同）。
- **离线语义**：手机"彻底无法使用"仅发生在"无网且要 LLM 推理"——题设已声明此场景用户有内网 LLM，则手机连内网后端即可；纯浏览/编辑离线全可用。

## 4. 合规衔接（上一波 4.2/2.5.2）
- PGlite 为打包 JS 资产（2.5.2 合规）；4.2 需原生增值件（APNs 推送/生物识别/原生登录壳——清单与证据见 docs/AUDIT_2026-09-23_multiclient_stack.md §2.1.1）。
- iOS 注意点：PGlite 持久化用 **IndexedDB（`idb://`）**（Apple WebKit 的 OPFS sync-handle 受限，Routier 插件页明示 Safari 用 idb）；WKWebView（Capacitor）同 WebKit 约束。

## 5. 工作量量级（增量于云-本地协同波）
| 项 | 量级 |
|---|---|
| Capacitor 平台生成 + 移动 SPA 适配（既有） | W2/W3（工具链就绪后） |
| 移动端 PGlite/SQLite 缓存层 + 同步 worker（复用桌面协议） | 2–3 天 |
| 离线读写队列 + 冲突提示（LWW 复用） | 1–2 天 |
| 真机回归（iOS/Android） | 1–2 天 |
| **合计**（增量于云-本地协同 MVP 之后） | **约 1 周** |

## 6. 风险与开放问题
1. 移动端 PGlite 持久化在大数据量下的性能/内存（WebKit IndexedDB 上限/压缩策略未实测——建议 W2 时实测一个 50MB 对话集）。
2. WKWebView 的 WASM/AudioWorklet 语音链（F4）仍需逐版本验证（评估报告 F4）。
3. 同步服务器在内网部署时的发现/认证细节（与桌面同协议，二期详设）。
4. 若未来要"手机全离线+本地 LLM"：重估 llama.cpp 移动端（内存/速度）+ JS 后端裁剪——单独立项评估。

## 7. 结论
**能完成类似任务，但分层**：桌面=全功能本地主机；手机=同步客户端（离线读写缓存 + 在线全功能）。"手机没有 PostgreSQL"的硬约束**不成立**（PGlite 提供 PG+pgvector 于 WebView），但它解决的是"本地数据层"，不改变"Python 后端不进手机"的事实。建议按云-本地协同波次落地后接入移动端同步客户端（增量约 1 周）。
