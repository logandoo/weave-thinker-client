# 全系统超时盘点与审核（2026-09-10，as of commit 23d05a207）

盘点范围：`backend/app/**` + `backend/config.toml`。分类原则：
- **L-BG**：LLM 判断·后台/离线（判定失败代价=错误回退或漏判；等待代价≈0）→ 阈值 **≥120s**
- **L-PERSIST**：LLM 判断·持久化路径（答案已流完，用户不等待首字）→ 阈值 **≥60s**
- **L-HOT**：LLM 判断·交互关键路径（影响 TTFT）→ 慷慨但有界（现 600s 档；个别判断调用虽有用户感知最坏情况，但超时=判定降级的代价更高——用户裁决过短不可接受）
- **L-RT**：LLM 判断·实时语音（设计即"慢判断=跳过"，实时性优先）→ 保持短，例外项
- **IO**：非 LLM（协议/子进程/浏览器/FFmpeg）→ 保持
- **WD**：看门狗/心跳间隔（非一次调用的上限，是存活探测节奏）→ 保持

## 一、LLM 判断类（本 wave 调整对象）

| # | 位置 | 旧值 | 新值 | 用途 | 类 |
|---|------|------|------|------|-----|
| 1 | agentic_judge.py judge_json 默认 | 30s | **config `[agent.auxiliary] judge_default_timeout_seconds`（默认 120s）** | 全部 LLM 判断的兜底 | L-BG |
| 2 | deathmatch_service.py DEFAULT_JUDGE_TIMEOUT | 30s | 120s | 死磕主 judge（done/blocked/ask 裁决，证据包大） | L-BG |
| 3 | deathmatch_service.py creative_goal 调用点 | 15s | 120s | 创作目标分类 | L-BG |
| 4 | deathmatch_service.py harness_repair 调用点 | 15s | 120s | harness 修复菜单选择 | L-BG |
| 5 | deathmatch_service.py completion_reconcile 调用点 | 25s | 120s | judge/verifier 冲突仲裁 | L-BG |
| 6 | citation_ledger（config `[agent.citation]`） | 30s | 60s | 引用去歧义 judge（conv a040c24e 20s 超时事故源） | L-PERSIST |
| 7 | memory_retrieval_service.py:1818 | 25s | 120s | 记忆召回判定 | L-BG |
| 8 | memory_subconscious_service.py:72 | 25s | 120s | 潜意识抽取 | L-BG |
| 9 | skill_evolution_service.py:57 | 25s | 120s | 技能评估 | L-BG |
| 10 | schedule_parser.py:73 | 20s | 120s | 定时任务语义解析（POST/PUT /api/scheduled-tasks 同步路径，用户感知最坏 +120s；超时=解析失败更差） | L-HOT |
| 11 | agent_service.py:89 identity_extract | 20s | 120s | 身份条目抽取（system-prompt 构建路径，TTFT 最坏 +120s；超时=身份失忆更差） | L-HOT |
| 12 | error_classifier.py:253 | 20s | 120s | 工具错误分类（agent loop 内错误恢复路径） | L-HOT |
| 13 | title_generator.py:303 | 15s | 120s | 会话标题生成 | L-BG |
| 14 | memory_multimodal_service.py:168 | 60s | 120s | 多模态记忆处理 | L-BG |
| 15 | visual_critic.py:25 | 60s | 120s | 死磕 VLM 视觉批评 | L-BG |
| 16 | config `[memory] interest_extract_timeout_seconds`（含 config.py 代码 fallback 40→120） | 40s | 120s | 兴趣抽取（proactive_learning 引用） | L-BG |
| 17 | config `[memory] migration_llm_timeout_seconds`（含 memory_migration_service 代码 fallback 60→120） | 60s | 120s | 记忆迁移 LLM 调用 | L-BG |

保持不动（已达标或设计如此）：

| 位置 | 值 | 用途 | 类 | 理由 |
|------|----|------|-----|------|
| config `[agent.audit] call_timeout_seconds` | 600s | 发送前审计 + 协调器 `_complete_json` | L-HOT | 已慷慨；交互路径上限 |
| config `[agent.audit] selection_timeout_seconds` | 480s | 选优兜底生成 | L-HOT | 已慷慨 |
| config `[agent.audit] salvage_timeout_seconds` | 240s | salvage 重整生成 | L-HOT | 已慷慨 |
| config `[agent.tool_loop] judge_timeout_seconds` | 300s | tool_loop 内 judge（外层预算：judge 120+VLM 135+reconcile 120 最坏可触顶 → 有界 fail-open，设计如此） | L-HOT | ≥120s 达标 |
| config `[memory.scheduler]` LLM 相关（120/300s） | 120/300s | 记忆调度器 LLM 调用 | L-BG | 达标 |
| config `[sub_agent] llm_call_timeout_seconds` | 90s | **死键**——config.py 有属性、app/ 无任何引用 | — | 无行为面，保持原值不动（A4.9 I2：误改已回退） |
| voice_service.py intent 判定 | 10s | 语音意图分类 | L-RT | 实时设计：慢=跳过，保持 |
| voice_service.py eot_semantic（config 1.5s）/filler 15s/interject 10s | 1.5-15s | 语音端点/插话判定 | L-RT | 实时设计，保持 |

## 二、非 LLM（IO/协议/子进程）——全部保持

| 位置 | 值 | 用途 |
|------|----|------|
| config `[web_search] timeout_seconds` | 8s/家 | 搜索供应商单腿超时（链式回退） |
| mcp_client.py 15/30/60s | 15-60s | MCP 协议请求 |
| browser_service / interactive_browser 3-30s | 3-30s | Playwright 页面操作 |
| notes.py 10-30s | 10-30s | 导出渲染等待 |
| terminal/code_execution 5s | 5s | 进程输出 drain |
| grep.py 30s / file_upload 30s | 30s | 子进程/下载 |
| config tool_call_timeout_seconds 1800s；overrides vision=3600；stall vision=600/code=300 | 300-3600s | 工具墙钟/停滞 |
| config inactivity 3600s / subtask_iteration 1800s / grace 120s | 120-3600s | 迭代墙钟 |
| export_worker 600s / ffmpeg 60s / http 300s / ws 10/20/5s | 60-600s | 导出/媒体/传输 |
| deathmatch 代码执行 subprocess 60/90s | 60/90s | 沙箱子进程 |
| memory_scheduler 30/120/300s | 30-300s | 调度器内部等待 |
| model_gateway rerank 10s | 10s | rerank HTTP（向量服务，非 LLM 判断） |

## 三、看门狗/心跳（WD）——全部保持

| 位置 | 值 | 用途 |
|------|----|------|
| chat.py `wait_for(queue, 10s)` ×3 | 10s | SSE keepalive 节奏（非调用上限） |
| agent_loop.py `asyncio.wait(8s)` ×3 | 8s | 工具并行存活探测节奏 |
| llm_service `_heartbeat_wrapped` interval | 30s | 流停滞唤醒哨兵（conv efaf8f9c 教训：绝不能取消在途读取） |
| voice_service.py 管线各 watchdog 2-120s | 2-120s | 语音全双工管线存活探测 |

## 审核结论

- 唯一事故源类 = L-BG/L-PERSIST 短超时：citation 20s 事故已实证（conv a040c24e）；deathmatch 15-30s 是「解析连败→paused」的疑似同谋（judge 超时返回 default → 连败计数 +1）。
- 语音实时判断是唯一「短即正确」的例外，单列豁免。
- 非 LLM 的 IO/WD 全部有各自协议依据，不动。
