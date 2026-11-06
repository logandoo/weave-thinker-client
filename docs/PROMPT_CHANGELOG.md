# Prompt Changelog（P6 / SemVer）

每条记录：`id / version / reason / eval report / rollback anchor`。
版本号遵循 `docs/PROMPT_GOVERNANCE.md`：MAJOR=输出契约/安全行为/工具权限；MINOR=新规则/section；PATCH=措辞。

## 2026-10-18 — P0–P6 首次收口

- release: `b1c0023d` → `6b023df0`（P0–P2 + reviewer + P3/P4 可观测收口）
- eval: `prompt_eval` 73/73；红队 24/24；live matrix 5/5（10 cases/alias）；compact 30/30；owner 人工 96.0%（25/30）；context-rot live 4/4；title 20/20
- rollback: 单 spec 回退见下表；compact 回滚=`[agent] prompt_compact_ratio=0`；config prompt 回滚=恢复 `config.toml` 对应键

### 版本变更

- `agent.system` **1.1.0**：P1.5 PERSONA 末位 + P3.3 compact 变体/5% canary + P1.9 conditional loading + P3.5 cache assembly；行为变更（MINOR）。
- 其余 spec **1.0.0**：首次注册/文本 move-only；无行为语义变更；`config.toml` 3 条 prompt 以 `config_prompt` 元数据入 manifest。

| id | version | owner | hash |
|---|---|---|---|
| `agent.system` | 1.1.0 | agent-core | `615e4634dcea` |
| `agentic.judge` | 1.0.0 | tools | `e3b0c44298fc` |
| `audit.claim_verify` | 1.0.0 | audit | `a134cdb8ace5` |
| `audit.response` | 1.0.0 | audit | `f9daafb07cb7` |
| `audit.scout` | 1.0.0 | audit | `e3b0c44298fc` |
| `background.worker` | 1.0.0 | tools | `e3b0c44298fc` |
| `citation.cross_turn` | 1.0.0 | citation | `1aaca9c88ebb` |
| `citation.disambiguate` | 1.0.0 | citation | `e3b0c44298fc` |
| `codegen.python` | 1.0.0 | tools | `e4e57306e6fa` |
| `context.compressor` | 1.0.0 | tools | `e3b0c44298fc` |
| `coordinator.route` | 1.0.0 | agent-core | `0b9a15d31554` |
| `deathmatch.bible` | 1.0.0 | deathmatch | `c5b001c0300b` |
| `deathmatch.continuation` | 1.0.0 | deathmatch | `82dc656d8157` |
| `deathmatch.grilling` | 1.0.0 | deathmatch | `ad04c9fd249b` |
| `deathmatch.judge` | 1.0.0 | deathmatch | `d674be5e3df9` |
| `deathmatch.plan` | 1.0.0 | deathmatch | `b2bd4e6f7baf` |
| `deathmatch.verifier` | 1.0.0 | deathmatch | `1896d569bdbd` |
| `error.classify` | 1.0.0 | tools | `51d235cdc828` |
| `generic.system` | 1.0.0 | agent-core | `e3b0c44298fc` |
| `memory.active_dreaming` | 1.0.0 | memory | `bb8a1104b491` |
| `memory.backfill` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.boundary` | 1.0.0 | memory | `1accdd1bfc3f` |
| `memory.clarification` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.consolidation` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.dream` | 1.0.0 | memory | `7de106dfb66d` |
| `memory.extraction` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.listwise_verify` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.migration` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.multimodal` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.profile` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.retrieval` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.subconscious` | 1.0.0 | memory | `e3b0c44298fc` |
| `memory.summary` | 1.0.0 | memory | `6d510a26e960` |
| `moa.aggregate` | 1.0.0 | tools | `e3b0c44298fc` |
| `proactive.learning` | 1.0.0 | memory | `e3b0c44298fc` |
| `schedule.execute` | 1.0.0 | tools | `e3b0c44298fc` |
| `schedule.parse` | 1.0.0 | tools | `467234f2fd87` |
| `search.plan` | 1.0.0 | agent-core | `b2f4230d1b26` |
| `subagent.child` | 1.0.0 | agent-core | `e3b0c44298fc` |
| `title.generation` | 1.0.0 | tools | `e049539c1963` |
| `tool.availability` | 1.0.0 | agent-core | `c58c5acb920c` |
| `tool.digest` | 1.0.0 | tools | `e3b0c44298fc` |
| `voice.barge_in` | 1.0.0 | voice | `d3556b4c9a3b` |
| `voice.duplex` | 1.0.0 | voice | `e3b0c44298fc` |
| `voice.endpointing` | 1.0.0 | voice | `b8386e84665e` |
| `voice.intent` | 1.0.0 | voice | `4f4ea0777677` |
| `voice.interjection` | 1.0.0 | voice | `c2675ed813ee` |

## 弃用

- `GRILLING_SYSTEM_PROMPT`：2026-10-18 删除（零调用）。
- `used_by=[]` 连续两个 release → 候选删除；`prompt_audit lint --check` 在 `deprecated.remove_after` 过期后非零退出。

## 发布门

lint 绿 → 快照/契约绿 → golden set 非劣 → 红队绿 → owner/security review → canary 5–10% → 全量。
回滚 = 切换 spec 指针/feature flag，不重新部署。
