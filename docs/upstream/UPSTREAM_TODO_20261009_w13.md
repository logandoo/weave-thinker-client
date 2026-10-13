# 上游（chatbot）修改清单 — W13 客户端波（tool_result 误判流致命修复）| 2026-10-09

> **用途**：本文件可直接交给上游 chatbot 会话执行。内容 = 客户端 W13 波（`weave-thinker-client`，head `aa13570`）在**共享镜像**中落地的修复，需上游同化（chatbot 仓 + 部署 60）。
> **路径约定**：本文件用**上游路径**（`frontend/...`）；本仓 vendor 对应 `app/...`。
> **基线关系**：①`frontend/src/api/streamDispatch.ts` 上游 HEAD（`e9021bf18`）与本仓修复前**逐字节一致**（已 diff 核验）→ 净补丁可 1:1 套用；②`frontend/tests/workflows/test_stream_true_guard.cjs` 与本仓修复前逐字节一致（已 diff 核验）；③上游 `frontend/package.json` 的 `test:unit` 链比本仓短（止于 `test_math_tokenizer`）→ 链尾追加即可，勿整行替换。
> **红线**：本波**未触**红线 B 文件。
> **上下文材料（本仓）**：`tests/verification_log.md`（W13 节）· `tests/review_package_w13.md` + `review_package_w13_fix1.md`（A4.9 两轮 ready）· `tests/w13_guard_{red,green,revert_mustfail}.log` · `docs/DECISION_MATRIX_local_vs_upstream.md` §3 登记表。

## 概览

| # | 项 | 优先级 | 性质 | 云端（60）是否受影响 |
|---|---|---|---|---|
| A1 | `streamDispatch.ts` 致命分支误吞 tool_result(error:true) | **P0** | 真 bug（用户可见事故） | ✅ Web 端任何失败工具调用（gate-blocked/工具异常）都复现「未知错误」横幅+已流出内容整片消失，稍后自愈 |
| A2 | guard 测试 4 新腿（随 A1） | P0（随 A1） | 回归断言面 | — |
| A3 | `test:unit` 链尾接入 guard | P1 | 回归保护自动化 | — |

---

## A1 — tool_result(error:true) 误判流致命（吞内容 + 「未知错误」）

- **上游文件**：`frontend/src/api/streamDispatch.ts`（与本仓修复前逐字节一致）
- **现状**：后端把每个工具结果**平铺**为 SSE 帧 `{call_id, name, result, error: bool}`（`backend/app/api/chat.py` 两处生成器同形）。`dispatchStreamPayload` 的致命分支 `if (data.error)` 先行——成功结果 `error:false` 漏过，**失败结果 `error:true`（布尔）被误判为流致命**：`onError('未知错误')`（布尔走兜底文案，真实错误文本丢失）+ `return true` 杀 SSE 读循环 → store `onError` 横幅 + `endStreaming` 整 wipe（流式气泡卸载，已流出内容全消失）。后端 agent 继续跑，立即+1500ms `refreshConversation` 从 DB 拉回 → 用户看到「吞内容→等一会恢复→失败卡红→终态红色褪去」全序列。
- **触发面**：任何 `error:true` 的工具结果——gate-blocked workspace write、工具异常、权限拒绝等。
- **变更**（本仓 `2a4e8b1`，净补丁 `git -C /home/user/Documents/DEV/weave-thinker-client diff f45625e..2a4e8b1 -- app/src/api/streamDispatch.ts`，路径换成 `frontend/src/...` 即可 1:1 套用）：
  ```ts
  // 致命分支前新增：
  const isToolResult = data.call_id !== undefined && data.name !== undefined && 'result' in data
  if (data.error && !isToolResult) { … }        // 原 if (data.error)
  // 下方路由复用同一谓词：
  if (isToolResult && h.onToolResult) { h.onToolResult(data as ToolResultEvent) }
  ```
- **为何不改服务端**：平铺协议已在多消费方固化，客户端汇聚点（dispatch）是唯一正确 choke point；上游服务端同理无需动。
- **验收**：guard 测试 case 10–13 全绿；回退修复 case10/12 必红（判别力实证日志在本仓 `tests/w13_guard_revert_mustfail.log`）。

## A2 — guard 测试 4 新腿（随 A1）
- **上游文件**：`frontend/tests/workflows/test_stream_true_guard.cjs`（与本仓修复前逐字节一致）
- **变更**：本仓 `2a4e8b1` 同 commit 内——`makeState` 增 `onToolResult` 记录 + case 10（失败路由 onToolResult/不触发 onError/返回 false/帧原样携带 error:true+result）· case 11（error:false 不变）· case 12（失败后流续跑）· case 13（流级错误帧仍致命）。1:1 套用。

## A3 — `test:unit` 链接入 guard
- **上游文件**：`frontend/package.json`
- **变更**：`test:unit` 链**尾**追加 `&& node --experimental-strip-types tests/workflows/test_stream_true_guard.cjs`（本仓 `a6673c4`；注意上游链尾与本仓不同，逐字追加勿整行替换）。
- **验收**：`npm run test:unit` rc=0 且末腿输出 guard 13/13。

## 部署与验收
- `deploy_60`：A1 为 Web 端用户可见事故修复，建议随下次部署带入；验收=60 上构造一次失败工具调用（如工作区 gate-blocked 写），确认无「未知错误」横幅、已流出内容不消失、失败卡原位标红。
