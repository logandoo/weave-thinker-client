# Weave Thinker（罗航）— Backend API 文档（详版）

> **文档结构**：
> - **一、接口明细**：全部 HTTP 接口，由 `tools/gen_api_md.py` 从**运行中后端的 `/openapi.json`** 自动生成
>   （每接口含 接口名 / 输入表 / 输出表 / 请求示例 / 返回示例；字段类型与必填性以 FastAPI Pydantic 模型为准）。
> - **二、专项语义**：OpenAPI 表达不了的行为级说明——SSE 事件协议、死磕模式全状态机、记忆检索管线、
>   语音事件与 WebSocket 帧、provider 语义、皮肤信任模型等（手工策展，与代码仓库同步维护）。
> - 两部分冲突时：字段级以「一」为准（生成自代码），行为级以「二」为准（生成器不理解业务）。
>
> 生成器与快照：`tools/gen_api_md.py` · 事实源快照 `tests/license_audit/openapi.json`（更新流程：重启后端后重跑生成器）。

## 通用约定

- **Base URL**：`http(s)://<host>:8158`（默认端口，见 `backend/config.toml [server]`）
- **交互式文档**：运行时 `/docs`（Swagger UI）
- **接口规模**：142 个 HTTP operations（21 个业务模块 + main.py 根路由）+ 2 条 WebSocket
- **认证**：JWT Bearer Token（请求头 `Authorization: Bearer <token>`）。`POST /api/auth/login` 获取，
  有效期 `[security] token_expire_days`（默认 7 天）；`POST /api/auth/refresh` 用仍有效的 Token 滑动续期
  （单条原子 CAS 轮换，登出后其 token 永久不可续期——详见二.1）。WebSocket 以 `?token=<jwt>` 或
  `Authorization` 头认证。标注「公开」的接口无需 Token。
- **管理员路由**：`/api/admin/*` 目前仅校验登录、**无角色校验**——任何已登录用户均可调用（已知限制，
  商业版规划收口）。
- **错误约定**：4xx 响应体均为 `{"detail": "<原因>"}`。
  `401`=无/坏 Token；`403`=权限不足或敏感操作拒绝；`404`=资源不存在或非本人资源
  （越权访问他人资源一律 404 而非 403，防探测）；`409`=冲突（重名等）；`400`=参数/业务校验失败；
  `422`=Pydantic 请求模型校验失败（FastAPI 默认 `{"detail": [校验条目]}`）。
- **分页/排序**：列表接口默认不带分页 token，由 limit 参数（如 `limit=200`）或全量返回，排序见各接口说明。
- **时间字段**：全部 ISO-8601（UTC 存储，序列化含时区）；客户端本地按 UTC+8 展示（约定见 README）。

---

# 一、接口明细（OpenAPI 生成）

## 同步 Sync（S1 波 2026-09-24）

前缀 `/api/sync`（`app/api/sync.py`）。行为级语义（压缩四规则 / LWW / 回显抑制 / 捕获域）见「二.13」。

### POST `/api/sync/devices`
**Register Device**

认证：JWT　|　源码模块：`app/api/sync.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 否 | 缺省服务端生成（hex32） |
| `name` | string | 否 | 设备显示名 |
| `platform` | string | 否 | macos/linux/windows/ios/android |

**输出**

`200` 字段（DeviceResponse）：`id` · `name` · `platform` · `last_seen_at` · `revoked` · `created_at`。
错误：`403`（设备已撤销）· `404`（设备属他人）。

### GET `/api/sync/delta`
**Get Delta**

认证：JWT　|　参数（query）：`cursor`（int≥0，默认 0）· `limit`（int 1-1000，默认 200）· `device_id`（string，可选，回显抑制）

**输出**

`200` 字段（DeltaResponse）：`events[]`（`seq` · `entity_type` · `entity_id` · `op` · `payload` · `origin_device` · `created_at`）· `cursor`（本窗口最大 seq）· `has_more`。

### POST `/api/sync/push`
**Push Events**

认证：JWT　|　**输入**：`device_id`（string）· `events[]`（≤500：`entity_type` · `entity_id` · `op` · `payload?`）

**输出**

`200` 字段（PushResponse）：`applied[]` · `skipped[]`。错误：`403`（设备撤销）· `404`（设备不存在/属他人）。

### PUT `/api/sync/blobs/{sha256}`
**Put Blob**

认证：JWT　|　请求体：原始字节（`application/octet-stream`）

**输出**

`200` 字段（BlobResponse）：`sha256` · `size` · `dedup`。错误：`400`（sha 格式/内容不符）· `413`（超 `[sync] blob_max_mb`）。

### GET `/api/sync/blobs/{sha256}`
**Get Blob**

认证：JWT　|　**输出**：`200` `application/octet-stream` 原始字节；`404` 不存在/非本人。

### POST `/api/sync/resync`
**Resync**

认证：JWT　|　请求体：无

**输出**

`200` 字段（ResyncResponse）：`entities`（`{entity_type: [全行快照...]}`）· `cursor`（当前全局最大 seq）。

---

## 认证 Auth

前缀 `/api/auth`（`app/api/auth.py`）

### POST `/api/auth/login`
**Login**

认证：公开　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | 是 |  |
| `password` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `access_token` | string | 是 |  |
| `token_type` | string | 是 |  |
| `user` | UserResponse | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/login" -H "Content-Type: application/json" -d {"username": "string", "password": "string"}
```

**返回示例**

```json
{
  "access_token": "string",
  "token_type": "string",
  "user": {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": null,
    "nickname": null,
    "avatar_data": null
  }
}
```

---

### POST `/api/auth/logout`
**Logout**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/logout" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/auth/me`
**Get Current User Info**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/auth/me" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### PUT `/api/auth/me/permissions`
**Update Agent Permissions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/auth/me/permissions" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### POST `/api/auth/refresh`
**Refresh Token**

> Sliding-session refresh (2026-08-25 wave-3): a still-valid token mints
> a fresh token whose window starts now — users who open the app within
> ``[security] token_expire_days`` are never re-asked for a password.
> ``get_current_user`` already rejects expired/invalid tokens (401/403), so
> nothing stale can roll forward.
> 
> Security (A4.9 I1): the rotation is a single atomic UPDATE ... WHERE
> session_token = <old> (CAS). A missing row — user already logged out, or
> a concurrent tab already rotated this token — yields 401 WITHOUT issuing
> a token: a logout stays terminal for that login and…

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/auth.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `access_token` | string | 是 |  |
| `token_type` | string | 是 |  |
| `user` | UserResponse | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/refresh" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "access_token": "string",
  "token_type": "string",
  "user": {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": null,
    "nickname": null,
    "avatar_data": null
  }
}
```

---

### POST `/api/auth/register`
**Register**

认证：公开　|　源码模块：`app/api/auth.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | string | 是 |  |
| `password` | string | 是 |  |

**输出**

`201` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/auth/register" -H "Content-Type: application/json" -d {"username": "string", "password": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---


## 聊天 Chat（SSE 流式）

前缀 `/api/chat`（`app/api/chat.py`）

### POST `/api/chat/deathmatch/subgoal`
**Deathmatch Add Subgoal**

> Append an acceptance criterion to an ACTIVE deathmatch conversation
> (D3): the judge then checks every subgoal alongside the original goal,
> and the continuation prompt surfaces them so the agent works toward
> them. 400 when the conversation is not in deathmatch goal loop.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/deathmatch/subgoal" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/permission/respond`
**Permission Respond**

> Respond to a pending permission request from the agent loop.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/permission/respond" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream`
**Chat Stream**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `conversation_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `messages` | Message[] | 是 |  |
| `enable_web_search` | bool | 否 | （默认 False） |
| `regenerate_from_message_id` | ∪string|null | 否（可空） |  |
| `edit_message_id` | ∪string|null | 否（可空） |  |
| `force_search_results` | ∪string|null | 否（可空） |  |
| `temperature` | ∪float|null | 否（可空） |  |
| `top_p` | ∪float|null | 否（可空） |  |
| `top_k` | ∪int|null | 否（可空） |  |
| `presence_penalty` | ∪float|null | 否（可空） |  |
| `frequency_penalty` | ∪float|null | 否（可空） |  |
| `max_tokens` | ∪int|null | 否（可空） |  |
| `enable_reasoning` | bool | 否 | （默认 False） |
| `reasoning_effort` | ∪string|null | 否（可空） |  |
| `thinking_budget` | ∪int|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_action` | ∪string|null | 否（可空） |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"conversation_id": "string", "assistant_id": "string", "messages": [{"role": "user", "content": "string"}], "enable_web_search": false, "regenerate_from_message_id": "string", "edit_message_id": "string", "force_search_results": "string", "temperature": 0.0, "top_p": 0.0, "top_k": 0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "max_tokens": 0, "enable_reasoning": false, "reasoning_effort": "string", "thinking_budget": 0, "deathmatch_mode": false, "deathmatch_action": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream/interject/{conversation_id}`
**Interject Agent Stream**

> Submit a user interjection into a RUNNING agent turn.
> 
> Iteration-boundary steering (codex pending-input / deepseek-harness
> next-step inbox): the text is queued on the conversation's active agent
> state and injected as a real user message at the next tool-loop iteration
> boundary — the in-flight LLM stream is never aborted. The loop emits an
> ``interjection_committed`` SSE event when the message enters the model
> context, and the producer persists it as a normal ``role=user`` message
> (commit-time persistence — a queued-but-never-consumed interjection is
> never written to the DB, so…

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream/interject/{conversation_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream/resume`
**Resume Stream**

> Reconnect to an SSE stream via the persistent StreamBuffer.
> 
> Accepts JSON body: 
> Uses subscribe_with_snapshot() for atomic replay + subscribe —
> no duplication gap between replay and live deltas.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream/resume" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/chat/stream/status/{conversation_id}`
**Stream Status**

> Check if a conversation has an active stream buffer.
> 
> Returns status information that the frontend uses to decide whether
> to resume streaming (incomplete) or read from DB (complete).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/chat/stream/status/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/chat/stream/stop/{conversation_id}`
**Stop Agent Stream**

> Explicitly cancel a running agent task for a conversation.
> 
> This is the correct way for the frontend STOP button to cancel an agent.
> It does NOT rely on closing the SSE connection — closing the SSE would be
> indistinguishable from a passive client disconnect (tab switch, browser
> throttle, network blip), which must leave the detached agent running so it
> can self-save results. The agent task is tracked in
> ``_DETACHED_AGENT_TASKS`` and cancelled directly here.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/chat.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/chat/stream/stop/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 会话与分组 Conversations

前缀 `/api/conversations`（`app/api/conversation.py`）

### GET `/api/conversations`
**List Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[ConversationResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "title": "string",
    "group_id": "string",
    "assistant_id": "string",
    "sort_order": 0,
    "created_at": "string",
    "updated_at": "string",
    "last_user_message_at": "string",
    "deathmatch_mode": false,
    "deathmatch_status": "string",
    "deathmatch_reason": "string",
    "deathmatch_goal": "string",
    "deathmatch_turns": 0,
    "deathmatch_max_turns": 30,
    "deathmatch_grilling_total": 0,
    "deathmatch_grilling_completed": 0,
    "deathmatch_grilling_round": 0,
    "deathmatch_grilling_round_total": 3,
    "deathmatch_context_summary": "string",
    "deathmatch_expected_marker": "string",
    "deathmatch_marker_miss_count": 0,
    "deathmatch_compressed_context": "string",
    "deathmatch_plan": {},
    "deathmatch_plan_version": 0
  }
]
```

---

### POST `/api/conversations`
**Create Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---

### POST `/api/conversations/bulk-delete`
**Bulk Delete Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `conversation_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"conversation_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/conversations/export`
**Export Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assistant_id` | string | 是 |  |
| `conversation_ids` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"assistant_id": "string", "conversation_ids": ["string"]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/conversations/export-pdf`
**Export Messages Pdf**

> Export selected messages as PDF (single combined or per-message zip).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ExportMessagesPDFRequest[] | 是 |  |
| `action` | string | 否 | （默认 'single'） |
| `endnote_enabled` | bool | 否 | 尾注开关（默认 True；挂账清零波 T4 随 UI 开关） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/export-pdf" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"title": "string", "content": "string", "role": "string"}], "action": "string", "endnote_enabled": true}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/groups`
**List Conversation Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[ConversationGroupResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/groups" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "color": "string",
    "assistant_id": "string",
    "sort_order": 0,
    "created_at": "string",
    "updated_at": "string",
    "conversation_count": 0
  }
]
```

---

### POST `/api/conversations/groups`
**Create Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `color` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/groups" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "color": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### POST `/api/conversations/groups/bulk-delete`
**Bulk Delete Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `group_ids` | string[] | 是 |  |
| `delete_conversations` | bool | 否 | （默认 False） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/groups/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"group_ids": ["string"], "delete_conversations": false}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/conversations/groups/reorder`
**Reorder Groups**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ConversationGroupReorderItem[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/reorder" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"id": "string", "sort_order": 0}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/conversations/groups/{group_id}`
**Delete Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |
| `delete_conversations` | query | bool | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/conversations/groups/{group_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/conversations/groups/{group_id}`
**Update Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `color` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/{group_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "color": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### PUT `/api/conversations/groups/{group_id}/move`
**Move Conversation Group**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `group_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `assistant_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `color` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `conversation_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/groups/{group_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "color": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "conversation_count": 0
}
```

---

### POST `/api/conversations/import`
**Import Conversations**

> Batch-import conversations from exported `.csv` / `.zip` files.
> 
> Each CSV becomes one conversation under *assistant_id*; `part=k/n`
> continuation rows are merged back losslessly. Per-file failures are
> reported in `errors` without aborting the rest.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.assistant_id` | string | 是 |  |
| `form.files` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/conversations/import" -H "Authorization: Bearer $TOKEN" -F "assistant_id=<value>" -F "files=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/conversations/reorder`
**Reorder Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | ConversationReorderItem[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/reorder" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"items": [{"id": "string", "sort_order": 0, "group_id": null}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/search`
**Search Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `q` | query | string | 是 |  |

**输出**

`200` 字段：`List[ConversationSearchResult]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `conversation_id` | string | 是 |  |
| `title` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `matched_messages` | MatchedMessage[] | 否 | （默认 []） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/search?q=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "conversation_id": "string",
    "title": "string",
    "updated_at": "string",
    "matched_messages": [
      null
    ]
  }
]
```

---

### DELETE `/api/conversations/{conversation_id}`
**Delete Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/conversations/{conversation_id}`
**Get Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |
| `include` | query | string | 否 | `slim` 时思考/工具肥字段以桩交付（懒加载）；缺省 `full` |
| `message_limit` | query | int|null | 否 | 最新 N 条窗口（含 has_more/oldest 分页元数据） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |
| `messages` | MessageResponse[] | 否 | （默认 []） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0,
  "messages": [
    {
      "id": null,
      "conversation_id": null,
      "role": null,
      "content": null,
      "reasoning_content": null,
      "tool_calls": null,
      "tool_results": null,
      "context_info": null,
      "created_at": null
    }
  ]
}
```

---

### PUT `/api/conversations/{conversation_id}`
**Update Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `group_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/{conversation_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "group_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---

### GET `/api/conversations/{conversation_id}/messages`
**Get Messages**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |
| `before_id` | query | string|null | 否 |  |
| `limit` | query | int|null | 否 |  |
| `include` | query | string | 否 | `slim` 时思考/工具肥字段以桩交付（懒加载，全文经 `/api/messages/{id}/payload/{field}` 取回）；缺省 `full` 解回全文 |

**输出**

`200` 字段：`List[MessageResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `conversation_id` | string | 是 |  |
| `role` | string | 是 |  |
| `content` | string | 是 |  |
| `reasoning_content` | ∪string|null | 否（可空） |  |
| `tool_calls` | ∪string|null | 否（可空） |  |
| `tool_results` | ∪string|null | 否（可空） |  |
| `context_info` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/conversations/{conversation_id}/messages" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "conversation_id": "string",
    "role": "string",
    "content": "string",
    "reasoning_content": "string",
    "tool_calls": "string",
    "tool_results": "string",
    "context_info": "string",
    "created_at": "string"
  }
]
```

---

### GET `/api/messages/{message_id}/payload/{field}`
**Get Message Payload**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/message_payloads.py`

按需取回单条消息单个字段的全文（前端「展开取全文」链路）。`field` ∈ `tool_results | reasoning_content | tool_calls`。

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `message_id` | path | string | 是 | 消息 id（须属当前用户） |
| `field` | path | string | 是 | 字段名（白名单外 404） |

**输出**

`200` 字段：
| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | string | 回显 |
| `field` | string | 回显 |
| `content` | string | 字段全文（外置桩已还源） |
| `size_bytes` | int | UTF-8 字节数（与 stub meta 同语义） |
| `externalized` | bool | 该字段是否外置 |

**错误语义（2026-10-05 起收紧）**：字段白名单外 / 消息不存在或不属当前用户 / 字段不存在 → `404`；**外置桩按 (message_id, field) 归属寻址 payload 行——伪造桩（携带他消息 payload_ref）或孤儿桩（行缺失）一律 `404`**，绝不按全局 sha256 寻址、绝不把桩体当内容回吐。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/messages/{message_id}/payload/tool_results" -H "Authorization: Bearer $TOKEN"
```

---


### PUT `/api/conversations/{conversation_id}/move`
**Move Conversation**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/conversation.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | string | 是 |  |
| `group_id` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `sort_order` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `last_user_message_at` | ∪string|null | 否（可空） |  |
| `deathmatch_mode` | bool | 否 | （默认 False） |
| `deathmatch_status` | string | 否 | （默认 'inactive'） |
| `deathmatch_reason` | ∪string|null | 否（可空） |  |
| `deathmatch_goal` | ∪string|null | 否（可空） |  |
| `deathmatch_turns` | int | 否 | （默认 0） |
| `deathmatch_max_turns` | int | 否 | （默认 30） |
| `deathmatch_grilling_total` | int | 否 | （默认 0） |
| `deathmatch_grilling_completed` | int | 否 | （默认 0） |
| `deathmatch_grilling_round` | int | 否 | （默认 0） |
| `deathmatch_grilling_round_total` | int | 否 | （默认 3） |
| `deathmatch_context_summary` | ∪string|null | 否（可空） |  |
| `deathmatch_expected_marker` | ∪string|null | 否（可空） |  |
| `deathmatch_marker_miss_count` | int | 否 | （默认 0） |
| `deathmatch_compressed_context` | ∪string|null | 否（可空） |  |
| `deathmatch_plan` | ∪object|null | 否（可空） |  |
| `deathmatch_plan_version` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/conversations/{conversation_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"group_id": "string", "assistant_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "group_id": "string",
  "assistant_id": "string",
  "sort_order": 0,
  "created_at": "string",
  "updated_at": "string",
  "last_user_message_at": "string",
  "deathmatch_mode": false,
  "deathmatch_status": "string",
  "deathmatch_reason": "string",
  "deathmatch_goal": "string",
  "deathmatch_turns": 0,
  "deathmatch_max_turns": 30,
  "deathmatch_grilling_total": 0,
  "deathmatch_grilling_completed": 0,
  "deathmatch_grilling_round": 0,
  "deathmatch_grilling_round_total": 3,
  "deathmatch_context_summary": "string",
  "deathmatch_expected_marker": "string",
  "deathmatch_marker_miss_count": 0,
  "deathmatch_compressed_context": "string",
  "deathmatch_plan": {},
  "deathmatch_plan_version": 0
}
```

---


## 助手 Assistants

前缀 `/api/assistants`（`app/api/assistant.py`）

### GET `/api/assistants`
**List Assistants**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

**输出**

`200` 字段：`List[AssistantResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `model_alias` | string | 否 | （默认 ''） |
| `subtask_model_alias` | string | 否 | （默认 ''） |
| `citation_style` | string | 否 | （默认 ''） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "name": "string",
    "system_prompt": "string",
    "model_alias": "string",
    "subtask_model_alias": "string",
    "citation_style": "string",
    "id": "string",
    "user_id": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/assistants`
**Create Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `model_alias` | string | 否 | （默认 'deepseek'） |
| `subtask_model_alias` | string | 否 | （默认 ''） |
| `citation_style` | string | 否 | （默认 ''） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `model_alias` | string | 否 | （默认 ''） |
| `subtask_model_alias` | string | 否 | （默认 ''） |
| `citation_style` | string | 否 | （默认 ''） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/assistants" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "system_prompt": "string", "model_alias": "string", "subtask_model_alias": "string", "citation_style": "string"}
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "model_alias": "string",
  "subtask_model_alias": "string",
  "citation_style": "string",
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### DELETE `/api/assistants/{assistant_id}`
**Delete Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/assistants/{assistant_id}`
**Get Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `model_alias` | string | 否 | （默认 ''） |
| `subtask_model_alias` | string | 否 | （默认 ''） |
| `citation_style` | string | 否 | （默认 ''） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "model_alias": "string",
  "subtask_model_alias": "string",
  "citation_style": "string",
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/assistants/{assistant_id}`
**Update Assistant**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `system_prompt` | ∪string|null | 否（可空） |  |
| `model_alias` | ∪string|null | 否（可空） |  |
| `subtask_model_alias` | ∪string|null | 否（可空） |  |
| `citation_style` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `system_prompt` | string | 否 | （默认 ''） |
| `model_alias` | string | 否 | （默认 ''） |
| `subtask_model_alias` | string | 否 | （默认 ''） |
| `citation_style` | string | 否 | （默认 ''） |
| `id` | string | 是 |  |
| `user_id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/assistants/{assistant_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "system_prompt": "string", "model_alias": "string", "subtask_model_alias": "string", "citation_style": "string"}
```

**返回示例**

```json
{
  "name": "string",
  "system_prompt": "string",
  "model_alias": "string",
  "subtask_model_alias": "string",
  "citation_style": "string",
  "id": "string",
  "user_id": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/assistants/{assistant_id}/conversations`
**Get Assistant Conversations**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/assistant.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `assistant_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/assistants/{assistant_id}/conversations" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 模型列表 Models

前缀 `/api/models`（`app/api/models.py`）

### GET `/api/models`
**List Models**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/models.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/models" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 笔记与笔记本 Notes

前缀 `/api/notes`（`app/api/notes.py`）

### GET `/api/notes/default-notebook`
**Get Default Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/default-notebook" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notebooks`
**List Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

**输出**

`200` 字段：`List[NotebookResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "is_default": true,
    "created_at": "string",
    "updated_at": "string",
    "note_count": 0
  }
]
```

---

### POST `/api/notes/notebooks`
**Create Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### POST `/api/notes/notebooks/bulk-delete`
**Bulk Delete Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `notebook_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"notebook_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/notes/notebooks/bulk-export`
**Bulk Export Notebooks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `notebook_ids` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/bulk-export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"notebook_ids": ["string"]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/notes/notebooks/{notebook_id}`
**Delete Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/notes/notebooks/{notebook_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/notes/notebooks/{notebook_id}`
**Update Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notebooks/{notebook_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### PUT `/api/notes/notebooks/{notebook_id}/default`
**Set Default Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `is_default` | bool | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `note_count` | int | 否 | （默认 0） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notebooks/{notebook_id}/default" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "is_default": true,
  "created_at": "string",
  "updated_at": "string",
  "note_count": 0
}
```

---

### GET `/api/notes/notebooks/{notebook_id}/export`
**Export Notebook**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks/{notebook_id}/export" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notebooks/{notebook_id}/notes`
**List Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

**输出**

`200` 字段：`List[NoteListItem]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content_preview` | string | 是 |  |
| `content_length` | int | 否 | （默认 0） |
| `token_estimate` | int | 否 | （默认 0） |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notebooks/{notebook_id}/notes" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "notebook_id": "string",
    "title": "string",
    "content_preview": "string",
    "content_length": 0,
    "token_estimate": 0,
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/notes/notebooks/{notebook_id}/notes`
**Create Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `notebook_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `content` | string | 否 | （默认 ''） |
| `raw_transcription` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notebooks/{notebook_id}/notes" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "content": "string", "raw_transcription": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/notes/notes/bulk-delete`
**Bulk Delete Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `deleted_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-delete" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"]}
```

**返回示例**

```json
{
  "status": "string",
  "deleted_count": 0
}
```

---

### POST `/api/notes/notes/bulk-export`
**Bulk Export Notes**

> Export multiple notes as PDF or MD. Returns a ZIP archive.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `endnote_enabled` | query | bool | 否 | 尾注开关（默认 True；镜像 `GET /notes/{note_id}/export` 同名查询参数） |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |
| `format` | string | 否 | （默认 'md'） |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-export" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"], "format": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/notes/notes/bulk-move`
**Bulk Move Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `note_ids` | string[] | 是 |  |
| `target_notebook_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `status` | string | 是 |  |
| `moved_count` | int | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/notes/bulk-move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"note_ids": ["string"], "target_notebook_id": "string"}
```

**返回示例**

```json
{
  "status": "string",
  "moved_count": 0
}
```

---

### DELETE `/api/notes/notes/{note_id}`
**Delete Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/notes/notes/{note_id}`
**Get Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/notes/notes/{note_id}`
**Update Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | ∪string|null | 否（可空） |  |
| `content` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notes/{note_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"title": "string", "content": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/notes/notes/{note_id}/export`
**Export Note**

> Export a single note as PDF or Markdown.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |
| `format` | query | string | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/notes/{note_id}/export" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/notes/notes/{note_id}/move`
**Move Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `note_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target_notebook_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/notes/notes/{note_id}/move" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"target_notebook_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/notes/quick`
**Create Quick Note**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `transcription` | string | 是 |  |
| `notebook_id` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content` | string | 是 |  |
| `raw_transcription` | ∪string|null | 是（可空） |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/notes/quick" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"transcription": "string", "notebook_id": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "notebook_id": "string",
  "title": "string",
  "content": "string",
  "raw_transcription": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/notes/search`
**Search Notes**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/notes.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `q` | query | string | 是 |  |
| `notebook_id` | query | string | 否 |  |

**输出**

`200` 字段：`List[NoteSearchResult]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `note_id` | string | 是 |  |
| `notebook_id` | string | 是 |  |
| `notebook_name` | string | 是 |  |
| `title` | ∪string|null | 是（可空） |  |
| `content_snippet` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/notes/search?q=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "note_id": "string",
    "notebook_id": "string",
    "notebook_name": "string",
    "title": "string",
    "content_snippet": "string"
  }
]
```

---


## 后台任务 Agent Tasks

前缀 `/api/agent-tasks`（`app/api/agent_tasks.py`）

### GET `/api/agent-tasks`
**List Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 | Filter by status: pending, claimed, running, completed, failed, cancelled |
| `include_completed` | query | bool | 否 | Include completed/failed/cancelled tasks |
| `limit` | query | int | 否 |  |

**输出**

`200` 字段：`List[AgentTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "title": "string",
    "goal": "string",
    "status": "string",
    "progress": 0.0,
    "iterations_done": 0,
    "iterations_max": 0,
    "elapsed_seconds": 0.0,
    "task_type": "string",
    "result": "string",
    "error": "string",
    "output_conversation_id": "string",
    "created_at": "2026-08-26T07:00:00Z",
    "started_at": "2026-08-26T07:00:00Z",
    "completed_at": "2026-08-26T07:00:00Z"
  }
]
```

---

### POST `/api/agent-tasks`
**Create Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `goal` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `task_type` | string | 否 | （默认 'general'） |
| `iterations_max` | int | 否 | （默认 30） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"goal": "string", "title": "string", "assistant_id": "string", "conversation_id": "string", "task_type": "string", "iterations_max": 30}
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "goal": "string",
  "status": "string",
  "progress": 0.0,
  "iterations_done": 0,
  "iterations_max": 0,
  "elapsed_seconds": 0.0,
  "task_type": "string",
  "result": "string",
  "error": "string",
  "output_conversation_id": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### GET `/api/agent-tasks/grilling/{conversation_id}`
**Get Grilling Questions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks/grilling/{conversation_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/agent-tasks/grilling/{conversation_id}/round-answer`
**Answer Grilling Round**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `conversation_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `answers` | object[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/grilling/{conversation_id}/round-answer" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"answers": [{}]}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/agent-tasks/grilling/{task_id}/answer`
**Answer Grilling Question**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `answer` | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/grilling/{task_id}/answer" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"answer": "string"}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/agent-tasks/{task_id}`
**Delete Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/agent-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/agent-tasks/{task_id}`
**Get Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `title` | ∪string|null | 否（可空） |  |
| `goal` | string | 是 |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `iterations_done` | int | 是 |  |
| `iterations_max` | int | 是 |  |
| `elapsed_seconds` | ∪float|null | 否（可空） |  |
| `task_type` | string | 是 |  |
| `result` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `output_conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/agent-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "title": "string",
  "goal": "string",
  "status": "string",
  "progress": 0.0,
  "iterations_done": 0,
  "iterations_max": 0,
  "elapsed_seconds": 0.0,
  "task_type": "string",
  "result": "string",
  "error": "string",
  "output_conversation_id": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### POST `/api/agent-tasks/{task_id}/cancel`
**Cancel Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/{task_id}/cancel" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/agent-tasks/{task_id}/messages`
**Append Task Message**

> F1/H2（2026-09-14）：向运行中的后台任务追加补充消息（steer）。
> 
> worker 每 5s 轮询 pending_messages 并推入 AgentLoop 的 interjection_queue，
> 在下一迭代边界作为真实 user 消息进入模型。仅 pending/claimed/running 可追加；
> 队列上限 10（与 chat interject 一致）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/agent_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/agent-tasks/{task_id}/messages" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 定时任务 Scheduled Tasks

前缀 `/api/scheduled-tasks`（`app/api/scheduled_tasks.py`）

### GET `/api/scheduled-tasks`
**List Scheduled Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 | Filter by status: active, paused, cancelled, completed, failed |
| `include_completed` | query | bool | 否 | Include completed/failed/cancelled tasks |

**输出**

`200` 字段：`List[ScheduledTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/scheduled-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "name": "string",
    "prompt": "string",
    "schedule_type": "string",
    "schedule_expr": "string",
    "next_run_at": "string",
    "last_run_at": "string",
    "status": "string",
    "repeat_count": 0,
    "run_count": 0,
    "assistant_id": "string",
    "conversation_id": "string",
    "created_at": "string"
  }
]
```

---

### POST `/api/scheduled-tasks`
**Create Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_text` | string | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `repeat_count` | ∪int|null | 否（可空） |  |

**输出**

`201` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/scheduled-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "prompt": "string", "schedule_text": "string", "assistant_id": "string", "repeat_count": 0}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---

### DELETE `/api/scheduled-tasks/{task_id}`
**Delete Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `204`— Successful Response

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/scheduled-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 204
# Successful Response

---

### PUT `/api/scheduled-tasks/{task_id}`
**Update Scheduled Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `prompt` | ∪string|null | 否（可空） |  |
| `schedule_text` | ∪string|null | 否（可空） |  |
| `status` | ∪string|null | 否（可空） |  |
| `repeat_count` | ∪int|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/scheduled-tasks/{task_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "prompt": "string", "schedule_text": "string", "status": "string", "repeat_count": 0}
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---

### POST `/api/scheduled-tasks/{task_id}/trigger`
**Trigger Scheduled Task**

> Manually trigger a scheduled task immediately.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/scheduled_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `prompt` | string | 是 |  |
| `schedule_type` | string | 是 |  |
| `schedule_expr` | string | 是 |  |
| `next_run_at` | ∪string|null | 否（可空） |  |
| `last_run_at` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `repeat_count` | ∪int|null | 否（可空） |  |
| `run_count` | int | 是 |  |
| `assistant_id` | ∪string|null | 否（可空） |  |
| `conversation_id` | ∪string|null | 否（可空） |  |
| `created_at` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/scheduled-tasks/{task_id}/trigger" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "name": "string",
  "prompt": "string",
  "schedule_type": "string",
  "schedule_expr": "string",
  "next_run_at": "string",
  "last_run_at": "string",
  "status": "string",
  "repeat_count": 0,
  "run_count": 0,
  "assistant_id": "string",
  "conversation_id": "string",
  "created_at": "string"
}
```

---


## 导出任务 Export Tasks

前缀 `/api/export-tasks`（`app/api/export_tasks.py`）

### GET `/api/export-tasks`
**List Export Tasks**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `status` | query | string|null | 否 |  |
| `limit` | query | int | 否 |  |

**输出**

`200` 字段：`List[ExportTaskResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "task_type": "string",
    "format": "string",
    "note_id": "string",
    "status": "string",
    "progress": 0.0,
    "filename": "string",
    "error": "string",
    "created_at": "2026-08-26T07:00:00Z",
    "started_at": "2026-08-26T07:00:00Z",
    "completed_at": "2026-08-26T07:00:00Z"
  }
]
```

---

### POST `/api/export-tasks`
**Create Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_type` | string | 否 | （默认 'single'） |
| `format` | string | 否 | （默认 'pdf'） |
| `note_id` | ∪string|null | 否（可空） |  |
| `note_ids` | ∪string[]|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/export-tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"task_type": "string", "format": "string", "note_id": "string", "note_ids": ["string"]}
```

**返回示例**

```json
{
  "id": "string",
  "task_type": "string",
  "format": "string",
  "note_id": "string",
  "status": "string",
  "progress": 0.0,
  "filename": "string",
  "error": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### DELETE `/api/export-tasks/{task_id}`
**Delete Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/export-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/export-tasks/{task_id}`
**Get Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `task_type` | string | 是 |  |
| `format` | string | 是 |  |
| `note_id` | ∪string|null | 否（可空） |  |
| `status` | string | 是 |  |
| `progress` | float | 是 |  |
| `filename` | ∪string|null | 否（可空） |  |
| `error` | ∪string|null | 否（可空） |  |
| `created_at` | ∪str(datetime)|null | 否（可空） |  |
| `started_at` | ∪str(datetime)|null | 否（可空） |  |
| `completed_at` | ∪str(datetime)|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks/{task_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "task_type": "string",
  "format": "string",
  "note_id": "string",
  "status": "string",
  "progress": 0.0,
  "filename": "string",
  "error": "string",
  "created_at": "2026-08-26T07:00:00Z",
  "started_at": "2026-08-26T07:00:00Z",
  "completed_at": "2026-08-26T07:00:00Z"
}
```

---

### POST `/api/export-tasks/{task_id}/cancel`
**Cancel Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/export-tasks/{task_id}/cancel" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/export-tasks/{task_id}/download`
**Download Export Task**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/export_tasks.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `task_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/export-tasks/{task_id}/download" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 记忆 Memory（v2 端点）

前缀 `/api/memory`（`app/api/memory.py`）

### DELETE `/api/memory/all`
**Delete All Memory**

> §10.4 全量擦除（GDPR Art. 17）：新概念层 + 文件层 + 旧 agent_memories；保留 raw 对话与笔记。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/memory/all" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/clarifications`
**List Clarifications**

> §10.4：澄清记录列表（revert 端点的前置——用户需能看到已应用澄清的 ID）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/clarifications" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/memory/clarifications/{clarification_id}/revert`
**Revert Clarification Endpoint**

> §9.6/§10.4：撤销已应用的澄清（negate 恢复有效；refine/add_constraint 回滚旧版本）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `clarification_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/memory/clarifications/{clarification_id}/revert" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/concepts`
**List Concepts**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/concepts" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/memory/concepts/{concept_id}`
**Delete Concept**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `concept_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/memory/concepts/{concept_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/memory/concepts/{concept_id}/forget`
**Forget Concept**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `concept_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/memory/concepts/{concept_id}/forget" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/cost_governance/status`
**Get Cost Governance Status**

> §9.10：用户在设置页查看自己的降级状态与触发原因。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/cost_governance/status" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/dreams`
**List Dreams**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/dreams" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/memory/recall_log`
**List Recall Log**

> C1：逐轮召回台账（元数据；不存记忆内容）。cursor=(created_at,id)。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `limit` | query | int | 否 |  |
| `before_id` | query | string|null | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/memory/recall_log" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/memory/{user_id}/cost_governance/reset`
**Reset Cost Governance**

> §9.10：admin 手动 reset 用户降级状态（本人可 reset 自己）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/memory.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/memory/{user_id}/cost_governance/reset" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 技能 Skills

前缀 `/api/skills`（`app/api/skills.py`）

### GET `/api/skills`
**List Skills**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/skills`
**Create Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "description": "string", "content": "string", "is_active": true}
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### GET `/api/skills/by-name/{skill_name}`
**Get Skill By Name**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_name` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills/by-name/{skill_name}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### POST `/api/skills/scan-folder`
**Scan Folder Executables**

> Scan an uploaded folder for executable files and return warnings before upload.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |
| `form.paths` | string[] | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/scan-folder" -H "Authorization: Bearer $TOKEN" -F "files=<value>" -F "paths=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skills/scan-zip`
**Scan Zip Executables**

> Scan zip file for executable files and return warnings before upload.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/scan-zip" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skills/upload`
**Upload Skills**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `force` | query | bool | 否 | Force upload even with executable warnings |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### POST `/api/skills/upload-folder`
**Upload Skills Folder**

> Upload skills from a folder (e.g. selected via webkitdirectory).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `force` | query | bool | 否 | Force upload even with executable warnings |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |
| `form.paths` | string[] | 是 |  |

**输出**

`200` 字段：`List[SkillResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skills/upload-folder" -H "Authorization: Bearer $TOKEN" -F "files=<value>" -F "paths=<value>"
```

**返回示例**

```json
[
  {
    "name": "string",
    "description": "string",
    "content": "string",
    "is_active": true,
    "id": "string",
    "user_id": "string",
    "source": "string",
    "category": "string",
    "created_at": "string",
    "updated_at": "string"
  }
]
```

---

### DELETE `/api/skills/{skill_id}`
**Delete Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skills/{skill_id}`
**Get Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---

### PUT `/api/skills/{skill_id}`
**Update Skill**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skills.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skill_id` | path | string | 是 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | ∪string|null | 否（可空） |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | ∪string|null | 否（可空） |  |
| `is_active` | ∪bool|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `name` | string | 是 |  |
| `description` | ∪string|null | 否（可空） |  |
| `content` | string | 是 |  |
| `is_active` | bool | 否 | （默认 True） |
| `id` | string | 是 |  |
| `user_id` | string | 否 | （默认 ''） |
| `source` | string | 否 | （默认 'user'） |
| `category` | ∪string|null | 否（可空） |  |
| `created_at` | string | 否 | （默认 ''） |
| `updated_at` | string | 否 | （默认 ''） |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/skills/{skill_id}" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"name": "string", "description": "string", "content": "string", "is_active": true}
```

**返回示例**

```json
{
  "name": "string",
  "description": "string",
  "content": "string",
  "is_active": true,
  "id": "string",
  "user_id": "string",
  "source": "string",
  "category": "string",
  "created_at": "string",
  "updated_at": "string"
}
```

---


## 语音识别 ASR

前缀 `/api/asr`（`app/api/asr.py`）

### GET `/api/asr/hotwords`
**Get Asr Hotwords**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/asr/hotwords" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "hotwords": [
    {
      "text": null,
      "weight": null,
      "lang": null
    }
  ]
}
```

---

### POST `/api/asr/hotwords`
**Save Asr Hotwords**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `hotwords` | HotwordItem[] | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/asr/hotwords" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"hotwords": [{"text": "string", "weight": 0, "lang": null}]}
```

**返回示例**

```json
{
  "hotwords": [
    {
      "text": null,
      "weight": null,
      "lang": null
    }
  ]
}
```

---

### POST `/api/asr/transcribe`
**Transcribe Audio**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/asr.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |
| `form.custom_hotwords` | string|null | 否 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `text` | string | 是 |  |
| `language` | ∪string|null | 否（可空） |  |
| `timestamps` | ∪TimestampInfo[]|null | 否（可空） | （默认 []） |
| `segments` | ∪SegmentInfo[]|null | 否（可空） | （默认 []） |
| `hotwords_used` | ∪string[]|null | 否（可空） | （默认 []） |
| `speaker_mode` | ∪string|null | 否（可空） | （默认 'disabled'） |
| `duration` | ∪float|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/asr/transcribe" -H "Authorization: Bearer $TOKEN" -F "file=<value>" -F "custom_hotwords=<value>"
```

**返回示例**

```json
{
  "text": "string",
  "language": "string",
  "timestamps": [
    null
  ],
  "segments": [
    null
  ],
  "hotwords_used": [
    "string"
  ],
  "speaker_mode": "string",
  "duration": 0.0
}
```

---


## 语音对话 Voice

前缀 `/api/voice`（`app/api/voice.py`）

### GET `/api/voice/sessions`
**List Voice Sessions**

> List voice sessions (conversations belonging to the 酬 assistant).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/voice/sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/voice/sessions`
**Create Voice Session**

> Create a new voice session under the 酬 assistant.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/voice/sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/voice/sessions/{session_id}/messages`
**Get Voice Session Messages**

> Get messages for a voice session (for loading history).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/voice.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/voice/sessions/{session_id}/messages" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 文件 Files

前缀 `/api/files`（`app/api/file_upload.py + app/api/files.py`）

### GET `/api/files/download`
**Download File**

> Download a file from the user's workspace.
> 
> Auth: Bearer header (standard), or ?token= query param (for <img> tags).
> Path resolution is the shared workspace resolver (no filename-walk around
> the containment/symlink rules — A4.9 Critical 2026-09-19); the response is
> pinned to the validated file descriptor.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Absolute path or workspace-relative path to the file |
| `token` | query | string|null | 否 | JWT token for <img> tag auth |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/download?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/files/list`
**List Files**

> List one page of a directory inside the user's workspace (offset/limit; folder manager + 同步客户端翻页).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Workspace-relative or in-workspace directory path |
| `offset` | query | int | 否 | 分页偏移（默认 0，≥0；2026-09-29 D-30） |
| `limit` | query | int | 否 | 单页条数（默认/上限 1000，1..1000） |
| `token` | query | string|null | 否 | JWT token for link-based auth |

输出字段：`{path, entries, truncated, total, offset, limit}`；`truncated`=还有后续页
（旧服务端无后三字段——客户端识别后不翻页，兼容不回归）。

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/list?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/files/office-html`
**Office Html**

> Convert a spreadsheet to a sanitized, self-contained HTML preview.
> 
> 2026-09-21: PDF pagination split wide tables across pages and clipped
> long cell text at page boundaries; the Calc HTML export keeps every sheet
> as one natural-width table (horizontal scroll in the viewer) with wrapped
> text and charts inlined as images. The document is sanitized server-side
> and served to a sandboxed iframe. ``400`` for non-spreadsheets, ``501``
> when LibreOffice/preview is unavailable and ``422`` on conversion failure
> — the frontend then falls back to ``/office-pdf`` and finally to its
> client-s

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Workspace-relative or in-workspace spreadsheet path |
| `token` | query | string|null | 否 | JWT token for link-based auth |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/office-html?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/files/office-pdf`
**Office Pdf**

> Convert an Office file to PDF for inline preview (server-side.
> 
> ``501 office_preview_unavailable`` when LibreOffice is missing — the
> frontend then falls back to its client-side renderers.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Workspace-relative or in-workspace office file path |
| `token` | query | string|null | 否 | JWT token for link-based auth |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/office-pdf?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/files/upload`
**Upload And Parse Files**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `save_to_notebook` | query | bool | 否 |  |
| `notebook_id` | query | string|null | 否 |  |

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.files` | str(binary) — 文件 | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `results` | FileParseResult[] | 是 |  |
| `notebook_id` | ∪string|null | 否（可空） |  |
| `notebook_name` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/files/upload" -H "Authorization: Bearer $TOKEN" -F "files=<value>"
```

**返回示例**

```json
{
  "results": [
    {
      "success": null,
      "markdown": null,
      "error": null,
      "file_type": null,
      "filename": null,
      "file_path": null,
      "rel_path": null,
      "size": null
    }
  ],
  "notebook_id": "string",
  "notebook_name": "string"
}
```

---

### GET `/api/files/zip`
**Download Zip**

> Zip one folder inside the user's workspace and stream it as a download.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/file_upload.py + app/api/files.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 | Workspace-relative or in-workspace directory path |
| `token` | query | string|null | 否 | JWT token for link-based auth |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/files/zip?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 图片 Images

前缀 `/api/images`（`app/api/image_upload.py`）

### GET `/api/images/serve`
**Serve Note Image**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `path` | query | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/images/serve?path=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/images/upload`
**Upload Image**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/images/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/images/upload-media`
**Upload Note Media**

> Upload an audio/video file into the note media store (noteimg/).
> 
> Returns the same shape as /upload so the frontend insert pipeline is
> identical. Audio ≤50MB, video ≤200MB. Size caps are enforced WHILE
> streaming (never buffer the whole upload into memory first — a
> multi-GB file must be rejected without OOMing the backend).

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/image_upload.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/images/upload-media" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 登录/聊天会话 Sessions

前缀 `/api/sessions`（`app/api/sessions.py`）

### GET `/api/sessions/chat-sessions`
**List Chat Sessions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

**输出**

`200` 字段：`List[item]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/chat-sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {}
]
```

---

### DELETE `/api/sessions/chat-sessions/{session_id}`
**Delete Chat Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/sessions/chat-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/sessions/chat-sessions/{session_id}`
**Get Chat Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `object`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/chat-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/sessions/user-sessions`
**List User Sessions**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

**输出**

`200` 字段：`List[item]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/sessions/user-sessions" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {}
]
```

---

### DELETE `/api/sessions/user-sessions/{session_id}`
**Delete User Session**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/sessions.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `session_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/sessions/user-sessions/{session_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 用户管理 Admin

前缀 `/api/admin`（`app/api/admin.py`）

### POST `/api/admin/memory/migration/rollback`
**Admin Migration Rollback**

> §8.5.5：单用户回滚。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/memory/migration/rollback" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/admin/memory/migration/run`
**Admin Migration Run**

> §8.5.1：手动触发迁移。user_id 缺省全量排队；dry_run=true 只读统计。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

请求体为直接量：`object|null`。

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/memory/migration/run" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/memory/migration/status`
**Admin Migration Status**

> §8.5.1：各用户迁移进度（读取 metadata_json.migration）。

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | query | string|null | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/memory/migration/status" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/admin/reload-config`
**Reload Config**

认证：公开　|　源码模块：`app/api/admin.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/reload-config"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/users`
**List Users**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

**输出**

`200` 字段：`List[UserResponse]`（数组，每项字段如下）
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/users" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
[
  {
    "id": "string",
    "username": "string",
    "created_at": "string",
    "agent_permissions": {},
    "nickname": "string",
    "avatar_data": "string"
  }
]
```

---

### POST `/api/admin/users`
**Create User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `username` | query | string | 是 |  |
| `password` | query | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/admin/users?username=<value>&password=<value>" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/admin/users/{user_id}`
**Delete User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/admin/users/{user_id}`
**Get User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### PUT `/api/admin/users/{user_id}`
**Update User**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/admin.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `user_id` | path | string | 是 |  |
| `username` | query | string | 否 |  |
| `password` | query | string | 否 |  |
| `is_active` | query | bool | 否 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/admin/users/{user_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 系统 System

前缀 `/api/system`（`app/api/system.py`）

### GET `/api/system/capabilities`
**Get System Capabilities**

> Return a machine-readable manifest of the harness's capabilities.
> 
> Includes: tool list, subsystem flags, key limits/timeouts, and modes.
> Does NOT include: model names, API keys, file paths, user data.

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/system.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/system/capabilities" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---


## 用户资料与偏好 Users（资料/头像/模型供应商覆盖/Skin 偏好）

前缀 `/api/users`（`app/api/user_settings.py + app/api/skins.py`）

### DELETE `/api/users/me/avatar`
**Delete Avatar**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/users/me/avatar" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### POST `/api/users/me/avatar`
**Upload Avatar**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/users/me/avatar" -H "Authorization: Bearer $TOKEN" -F "file=<value>"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### DELETE `/api/users/me/model-provider`
**Delete Model Provider**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/users/me/model-provider" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/users/me/model-provider`
**Get Model Provider**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/users/me/model-provider" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### PUT `/api/users/me/model-provider`
**Update Model Provider**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/users/me/model-provider" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {}
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/users/me/preferences`
**Get Ui Preferences**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/users/me/preferences" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "skin_id": "string"
}
```

---

### PUT `/api/users/me/preferences`
**Update Ui Preferences**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `skin_id` | string | 是 |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/users/me/preferences" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"skin_id": "string"}
```

**返回示例**

```json
{
  "skin_id": "string"
}
```

---

### GET `/api/users/me/profile`
**Get Profile**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/users/me/profile" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---

### PUT `/api/users/me/profile`
**Update Profile**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/user_settings.py + app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `nickname` | ∪string|null | 否（可空） |  |

**输出**

`200` 字段：
| 字段 | 类型 | 必填* | 说明 |
|---|---|---|---|
| `id` | string | 是 |  |
| `username` | string | 是 |  |
| `created_at` | string | 是 |  |
| `agent_permissions` | ∪object|null | 否（可空） |  |
| `nickname` | ∪string|null | 否（可空） |  |
| `avatar_data` | ∪string|null | 否（可空） |  |

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X PUT "https://<host>:8158/api/users/me/profile" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d {"nickname": "string"}
```

**返回示例**

```json
{
  "id": "string",
  "username": "string",
  "created_at": "string",
  "agent_permissions": {},
  "nickname": "string",
  "avatar_data": "string"
}
```

---


## 皮肤 Skins

前缀 `/api/skins`（`app/api/skins.py`）

### GET `/api/skins`
**List Skins**

认证：公开　|　源码模块：`app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skins/mine`
**List My Skins**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins/mine" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### POST `/api/skins/upload`
**Upload Skin**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `form.file` | str(binary) — 文件 | 是 |  |
| `form.name` | string | 否 |  |
| `form.description` | string | 否 |  |

**输出**

- `201` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X POST "https://<host>:8158/api/skins/upload" -H "Authorization: Bearer $TOKEN" -F "file=<value>" -F "name=<value>" -F "description=<value>"
```

**返回示例**

HTTP 201 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### DELETE `/api/skins/{skin_id}`
**Delete My Skin**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skin_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X DELETE "https://<host>:8158/api/skins/{skin_id}" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/api/skins/{skin_id}/css`
**Get My Skin Css**

认证：需登录（`Authorization: Bearer <token>`）　|　源码模块：`app/api/skins.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `skin_id` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/api/skins/{skin_id}/css" -H "Authorization: Bearer $TOKEN"
```

**返回示例**

HTTP 200

---

## 其他（main.py / 根路由）

### GET `/`
**Root**

认证：公开　|　源码模块：`main.py`

**输入**

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

### GET `/app/frontend/{full_path}`
**Spa Fallback**

认证：公开　|　源码模块：`main.py`

**输入**

| 字段 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `full_path` | path | string | 是 |  |

**输出**

- `200` — 响应体类型 `any`（Successful Response）

> \* 输出「必填」列 = 该字段是否总在响应中出现（Pydantic required）；对象字段深层行以 `.` 前缀展开。

**请求示例**

```bash
curl -k -X GET "https://<host>:8158/app/frontend/{full_path}"
```

**返回示例**

HTTP 200 — `application/json` 流式/二进制响应（按端点说明处理）。

---

---

# 二、专项语义（行为级补充）

## 1. 聊天流式（SSE）——`/api/chat/stream*`

`POST /api/chat/stream` 是主聊天接口，返回 **SSE 流**（`text/event-stream`）：创建或复用会话、持久化用户
消息、解析 `[skill:...]` 标记、处理重新生成/编辑（`regenerate_from_message_id` / `edit_message_id`）、管理
死磕模式生命周期，并在后台 `asyncio.Task` 中运行 AgentLoop。**客户端断开后 Agent 继续后台运行**并自行
保存结果；首次完成后自动生成会话标题。

### SSE 事件类型

| event | data 字段 | 说明 |
|---|---|---|
| `content` | `{content}` | 助手正文增量 |
| `reasoning_content` | `{reasoning_content, phase?}` | 思考增量 |
| `content_segment` | `{segment_content}` | 分段内容（死磕多轮输出） |
| `tool_call` | `{name, arguments, call_id}` | 工具调用 |
| `tool_result` | `{name, call_id, result, error}` | 工具执行结果 |
| `iteration` | `{iteration, max_iterations}` | 工具循环轮次（`[agent.tool_loop] max_iterations`，默认 50） |
| `ping` | `{ping: true}` | 心跳（30s 空闲） |
| `message` | `{agent_step: {...}}` | Agent 步骤（工具/推理/搜索进度） |
| `message` | `{attachments: [...]}` | 生成的文件附件（下载卡） |
| `message` | `{permission_request: {request_id, tool_name, description, details}}` | 敏感工具执行前的权限请求（经 `/api/chat/permission/respond` 应答） |
| `message` | `{deathmatch_verdict: {...}}` | 死磕裁判结论（结构见 2.8） |
| `message` | `{title_update: {conversation_id, title}}` | 标题更新（在 done 之后） |
| `error` | `{error}` | 错误 |
| `done` | `{conversation_id, message_id, title, tool_results, done: true}` | 终止事件；`tool_results` 为 JSON 字符串：`{rounds, results, search_failed, agent_steps, attachments?, display_sequence?, content_segments?}` |

> 前端以 `conversationId` 为键并行管理多路 SSE；引用台账 `[N]` 由 `citation_ledger` 单调编号，正文中的
> `[N]` 均能在 `done.tool_results.results` 中找到来源。

### 断线重连与状态查询

- `POST /api/chat/stream/resume`：重连进行中（或刚完成）的流。请求体 `{conversation_id}`。响应先发送原子
  快照事件 `message`：`{replay: {...}, status: "complete"|"incomplete", is_running}`，随后接续实时事件。
  若已完成则仅发快照 + `done`。优先读 `StreamBuffer`，否则回退 `ActiveAgentRegistry`。死磕模式同样适用。
- `GET /api/chat/stream/status/{conversation_id}`：`{has_buffer, status: "running"|"complete"|"error"|"none", is_running, db_message_id, content_length}`。
- `POST /api/chat/stream/stop/{conversation_id}`：停止后台任务。`{status: "cancelled"|"not_running", conversation_id}`。
  若 run 仍处 setup 阶段（无已创建 Agent 任务），会释放该会话仍处 provisional 状态的 session-lock 槽位，
  保证编辑后重发不被 `conversation_busy` 误拒。
- `POST /api/chat/permission/respond`：`{request_id, approved: bool}` → `{status: "ok", approved}`；
  request 不存在或过期 → 404。

## 2. 会话健壮性说明

- **列表排序契约（2026-09-15）**：`GET /api/conversations` 按 `coalesce(sort_order,0) ASC`、
  `coalesce(last_user_message_at, updated_at) DESC` 排序（NULL `sort_order` 与响应契约 `sort_order or 0`
  同口径）。用户发送消息时服务端把该会话 `sort_order` 置 0 并刷新其最近用户消息时间——**继续旧会话即
  置顶到所在分组/时间分类最上方**（前端同口径乐观置顶，刷新后由服务端持久化顺序保持一致）。
  `GET /api/conversations/{id}` 返回同口径的 `last_user_message_at`（由已加载消息计算，无额外查询）。
- `sort_order` 允许被库级写入置 NULL（ORM 默认值不覆盖显式 NULL INSERT），曾导致整表 500；启动迁移
  `conversations_sort_order_backfill` / `conversation_groups_sort_order_backfill` 幂等回填 0，
  响应构造层再做 `None→0` 防御——单行脏数据不再引发 500（2026-08-25 起）。
- `DELETE /api/conversations/{id}` 会连带取消该会话的活跃定时任务。
- `GET /api/conversations/{id}` 返回前执行死磕僵尸状态修复（见 2.7）。

## 3. provider 语义（助手模型配置）

`provider_type` 可选值（wire 值，前端下拉显示名括号内为中文名）：

| wire 值 | 前端显示名 | 语义 |
|---|---|---|
| `deepseek` | DeepSeek | OpenAI 兼容 + DeepSeek 思考参数格式（`reasoning_effort`: `low`/`high`/`max`） |
| `qwen3.8_vllm` | **Qwen3.8(Local)**（2026-08-26 改名，原 "Qwen3.8 (VLLM)"） | 按 modelscope vLLM 部署指南格式调用本地/自托管服务；**地址/密钥/模型名由助手配置**：`custom_api_url`/`custom_api_key`/`custom_model_name` 优先，留空回退服务器 `[providers."qwen3.8_27b"]` 配置 |
| `mimo` | MiMo (Xiaomi) | 小米 MiMo（含 TTS/ASR 供应商链） |
| `custom` | 自定义 | 任意 OpenAI 兼容端点（可 `extra_body` 直传 JSON） |
| `zhipu` / `qwen` | （已移除） | 后端分支保留兼容，前端不再提供 |

Qwen3.8(Local) 供应商采样参数分两套（思考/非思考），字段为 NULL 时使用模型卡默认值（思考：
temperature 1.0 / top_p 0.95 / top_k 20 / min_p 0.0 / presence_penalty 0.0 / repetition_penalty 1.0；
非思考：0.7 / 0.80 / 20 / 0.0 / 1.5 / 1.0）。`preserve_thinking`（默认 true）经
`chat_template_kwargs.preserve_thinking` 传给 vLLM；`reasoning_effort` 取 `xhigh`/`medium`/`low` 走
`chat_template_kwargs`。`reasoning` 请求参数（`enable_reasoning` / `reasoning_effort` / `thinking_budget`）
全链路继承助手配置（P0：同助手下一切 LLM 行为默认走该助手模型，2026-08-21 用户强制要求）。

## 4. 技能 Skills 行为补充

- 双来源：**用户技能**（数据库，可增删改）+ **系统技能**（`backend/skills/*/SKILL.md`，只读，
  `source="system"`，ID 形如 `system:<name>`，数据库接口不解析系统技能 ID，用 `GET /api/skills/by-name/{name}`）。
- `POST /api/skills` 重名（用户/系统均查）拒绝；`PUT` 改名时再次检查冲突。
- ZIP 上传按根目录分组，需含 `SKILL.md`；`scan-zip` / `scan-folder` 先扫描可执行/危险文件（**不落库**），
  有告警时上传需 `?force=true`。
- 上传的脚本经 `skill_run_script` 工具在用户工作区沙箱执行（非服务端随意执行）。

## 5. 语音（ASR + Voice）

### REST（总览见一部分）

- `POST /api/asr/transcribe` 表单 `file`（音频）+ `custom_hotwords?`（JSON 字符串，如
  `[{"text":"foo","weight":3}]`）。响应含 `text`、可选 `timestamps` / `segments`（说话人分离）/
  `hotwords_used` / `speaker_mode` / `duration`。
- 热词 `GET/POST /api/asr/hotwords`：POST 整体替换（先删后插，权重 1–5，尝试同步 DashScope 词表）。
- 每用户有专属**语音助理**（`ensure_voice_assistant()` 在首次语音会话时创建），独立于默认助手。

### WebSocket

- `WS /api/voice/ws?conversation_id=`：全双工语音对话（`VoiceDuplexSession`，支持 barge-in 打断 + 断点续播）。
  语音模式禁用时以 `1013` 关闭。
- `WS /api/asr/ws/transcribe/stream`：实时流式转写（代理到底层 ASR）。认证失败以 `1008` 关闭；
  错误帧 `{"event":"error","error":"..."}`。
- 服务端 → 客户端节选事件：

| 事件 | 内容 | 说明 |
|---|---|---|
| `filler` | `{"event","text"}` | 回答生成期间先口播的随机填充词前缀（`[voice] filler_*`，可关闭） |
| `agent_backchannel` | `{"event","text"}` | 用户句间停顿内的短应和（`backchannel_*`，可关闭） |
| `interjection` | `{"event","text","emotion","raw_text","kind"?}` | 助手主动插话：`kind` 空=追问式；`memory_append`=记忆补充；`memory_correct`=自我纠正（默认关 `memory_correct_enabled=false`） |
| `tts_*` / `asr_*` | 音频分段与识别状态 | 播放进度/打断偏移量，前端渲染语音条 |

- **记忆插话（L0，2026-08-19）**：每用户轮 fire-and-forget 调 `retrieve_with_meta`，结果以哨兵区块
  原地替换 identity prompt 记忆段（下轮回答自动接地）；另派仲裁 LLM 判是否值得插话（append/correct/none，
  LLM-only 无关键词分类），门控：`memory_interjection_min_score`(0.5)、预算 `max_append`(3)/`max_correct`(2)、
  冷却 20s、同 candidate-id 去重。插话落库为 assistant 消息（Web 可见、重连可重建）；仲裁输出强制过
  注入消毒（不受 `super_admin_bypass` 影响）。
- filler/backchannel 走 aux TTS 队列串行播报（绝不与回答/插话重叠），填充词在回答首段音频就绪时截断；
  记忆插话是普通项（完整播完，不被截断）。

## 6. 文件 / 图片 行为补充

- `POST /api/files/upload`：`save_to_notebook=false` 流式存工作区 `uploads/`（单文件 ≤1GB）；
  `=true` 解析文档（Word/PPT/Excel/CSV/PDF → Markdown，处理内嵌/本地/远程图片）存为笔记（图片文件拒绝）。
- `GET /api/files/download`：认证支持 `Authorization` 头**或** `?token=`（供 `<img>` 标签）；路径不在
  工作区时按文件名递归查找。
- `GET /api/files/list`：工作区目录列表（文件夹管理器，UPSTREAM_TODO_20261007 项5）——`path`=
  工作区相对/工作区内目录路径；目录优先、名称不区分大小写排序；单次最多 1000 条
  （`truncated=true` 表示被截断），符号链接不列出；越界/不存在 404。⚠ 两实现面差异（2026-10-08 W7 评审限定）：
  上游部署端点当前无分页参数、单次上限即全量窗口；**本仓 vendor 服务端实现含
  `offset/limit` 分页与 `total/offset/limit` 响应字段**（D-30，2026-09-29 本地补丁；
  旧客户端忽略新字段行为不变，旧服务端无这些字段时客户端兼容降级）。
- `GET /api/images/serve`：路径逃逸工作区 403；不存在 404。
- 笔记导出 PDF 走 `POST /api/export-tasks` 异步（WeasyPrint，Markdown→HTML→PDF，支持 Mermaid/LaTeX、
  本地图片 base64 内嵌），`/download` 取件。

## 7. 死磕模式（Deathmatch）全状态机

死磕模式是持久化多轮目标完成循环：**盘问（Grilling）** + **目标循环（Goal Loop / PEVR）** 两阶段，
API 分布在聊天流（`/api/chat/stream`）、后台任务（`/api/agent-tasks/grilling/*`）与会话
（`/api/conversations`）三个模块。

- **盘问（Phase 1）**：2–3 轮、每轮 2–3 个澄清问题；每个问题是 `task_type="grilling"` 的 AgentTask
  （`pending`）。全部回答后 LLM 合成目标，转目标循环。
- **目标循环（Phase 2 / PEVR）**：P 计划 → E 执行 → V 裁判验证 → R 重规划。裁判每轮评估目标是否满足，
  满足则 `done`；停滞三级升级：`<3` 正常续接 · `≥3`（`stall_partial_threshold`）→ `partial_complete`
  · `≥6`（`stall_hard_threshold`）/ `max_turns` 耗尽 / `max_wall_time_seconds` 超时 → `human_gate`
  （均需用户经 `deathmatch_action="resume"` 介入）。

### 会话上的死磕字段（`ConversationResponse` 补齐）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `deathmatch_mode` | bool | false | 死磕开关 |
| `deathmatch_status` | str | "inactive" | `inactive`/`grilling`/`active`/`done`/`paused`/`human_gate`/`partial_complete` |
| `deathmatch_goal` | str? | null | 合成后的目标描述 |
| `deathmatch_turns` / `deathmatch_max_turns` | int / int | 0 / 30 | 当前轮 / 预算（`[deathmatch].max_turns` 覆盖） |
| `deathmatch_grilling_total` / `_completed` / `_round` / `_round_total` | int | 0/0/0/3 | 盘问进度 |
| `deathmatch_reason` | str? | null | 状态原因（用户可见） |
| `deathmatch_context_summary` | str? | null | 上下文压缩摘要 |
| `deathmatch_plan` / `deathmatch_plan_version` | dict? / int | null / 0 | PEVR 计划（`steps: [{id, description, status}]`）与修订计数 |
| `deathmatch_expected_marker` / `_marker_miss_count` | str? / int | null / 0 | 上下文标记与丢失计数 |
| `deathmatch_compressed_context` | str? | null | 压缩上下文（JSON） |

### start/stop 控制（经 `POST /api/chat/stream` 字段）

| `deathmatch_mode` | `deathmatch_action` | 行为 |
|---|---|---|
| true | null | 按当前状态自动推进（grilling→分类意图；paused→恢复检测；done→意图分类） |
| true | "start" | 任意状态激活：压缩上下文 + 启动盘问 |
| true | "stop" | 停用并清理状态 |
| true | "pause" / "resume" | 暂停 / 恢复（盘问中→grilling，盘问完成→active，重置墙钟） |
| false | — | 普通聊天（不受死磕逻辑影响） |

### `POST /api/chat/deathmatch/subgoal`（追加验收标准）

请求体 `{"conversation_id": "str", "text": "≤500 字符"}` → `{"status": "ok", "subgoals": [str, ...]}`；
裁判每轮检查全部标准。错误：400（非死磕目标循环/空/超 20 条）、404（会话不存在或非本人）。

### SSE 事件 `deathmatch_verdict`（结构）

```json
{
  "status": "grilling|active|done|paused|human_gate|partial_complete",
  "verdict": "grilling_questions_generated|grilling_recovered|continue|done|wait|…",
  "reason": "自然语言原因说明",
  "turns": 3, "max_turns": 30,
  "grilling_completed": 1, "grilling_total": 3,
  "grilling_round": 1, "grilling_round_total": 3,
  "grilling_questions": [],
  "verify_result": {},
  "human_gate": "...",
  "final_attachments": [],
  "final_summary_table": "...",
  "plan_version": 1,
  "plan_steps": []
}
```

多轮输出经 `content_segment` 事件分段，每轮内容作为独立 `Message` 落库。

### 盘问阶段 API（行为）

- `GET /api/agent-tasks/grilling/{conversation_id}`：列出当前轮问题
  （`task_id` / `question` / `recommendation` / `options`(2–4 个) / `round` / `status` / `answer?`）。
- `POST /api/agent-tasks/grilling/{task_id}/answer`：单题回答 `{"answer"}`；本轮答完 → 推进下一轮或
  合成目标转 `active`；`result.status ∈ {grilling_in_progress, next_round, grilling_complete}`。
- `POST /api/agent-tasks/grilling/{conversation_id}/round-answer`：批量 `{"answers": [{task_id, answer}]}`；
  不全 → `{"status": "incomplete", ...}`。

### 僵尸状态修复

`GET /api/conversations/{id}` 返回前执行 `_reconcile_deathmatch_status()`：
`status="active"` 但无 Agent 运行且 `updated_at` 超 60s → `paused`；`grilling` 但问题全答完 → 合成目标转
`active`；60s 内被写过的会话跳过（防与 `/chat/stream` 竞争）。

## 8. 记忆检索管线（内部行为，无独立端点；成本治理端点见一部分）

记忆上下文在聊天/语音请求内自动注入系统提示。行为与关键配置（2026-08-09/19 修正后）：

- **入口**：`retrieve_and_build_context(db, user_id, messages)`；meta 入口
  `retrieve_with_meta(...) -> (ctx, memory_ids, top_gate_score)`（语音每轮召回用；ctx 与旧入口逐字节一致）。
  门控：`[memory].enabled` + `[memory].retrieval_enabled` + 运行时探测（pgvector / embedding provider）。
- **四阶段**：Stage 0 jieba 分词 +（可选）LLM 相对时间归一 → Stage 1 BM25（概念/事件/原文三索引）→
  Stage 2 描述 BM25 + 1-hop 关系扩展 → Stage 3 embedding 余弦 + RRF 融合
  （同 tier 尺度一致化：BM25-only 候选归一化到 `[0, bm25_weight]`；`embedding_sim_threshold` 默认 0.3 滤弱）。
- **Stage 4 交叉编码器重排（模式 B）**：`rerank_mode=cross_encoder` 调
  `[memory].rerank_api_base`（`POST /rerank`），候选池 tier 均衡（concept 8 / episodic 4 / subconscious 4），
  raw logits min-max 归一化写 `calibrated_score`；失败回退 score_only。**早退**：top1-top3 差距 ≥
  `stage4_llm_trigger_score_gap` 或 top1 `embedding_sim` ≥ `stage4_ce_skip_strong_top_sim`(0.7) 时跳过。
- **画像写路径**：调度器每日从近 14 天 daily-summary 提炼 profile 事实 → upsert 为
  `memory_type='profile'` 概念（幂等，`profile_sync_enabled` 总开关）；读路径优先 profile 概念
  （≤600 字符恒定基底，优先级 0.30）。
- **冷启动回退**：概念 ≤5 时若近 30 天有带 embedding 的 subconscious 内容则继续正常管线；否则 v1 摘要
  / 多模态 fallback。
- **复合终分**：relevance/recency/weight 线性融合（`fusion_*`）；relevance 用**绝对校准分**（跨 tier 可比）。
- **注入组装**：段按绝对相关分降序，预算 `injection_total_token_budget`(默认 2000) 从最低优先段截断；
  恒定基底 `[用户画像 Profile]` + `[用户长期记忆总览]`；命中段 `[相关事件 Episodic]` /
  `[相关概念 Concept]` / `[近期原文片段 Subconscious]`，再后 `[近期 Dream]` / `[相关概念集合 Clusters]` /
  `[澄清提示]`。
- **Dream**：`[近期 Dream]` = `agent_dreams` 表 `dream_type='consolidation'` 最新行（consolidation 管线
  每日生成，不受 `dreaming_enabled` 门控）；v1 nightly dream 分离为 `dream_type='nightly'`（不注入）。
- **会话缓存**：最近 3 轮查询哈希，TTL 5min。
- **成本治理端点**（`/api/memory/cost_governance/status`、`/api/memory/{user_id}/cost_governance/reset`）：
  升级当 `today_calls > max(7 日均值 × warn_multiplier, min_today_calls)`（2026-08-25 起加绝对下限默认 8）；
  恢复当 `today_calls ≤ 7 日均值 × recovery_ratio`（默认 1.0）。`reason` = 当前降级触发式（降级时保留）；
  `last_change` = 最近一次等级迁移动作记录。

## 9. 皮肤 Skins 信任模型（上传皮肤）

- `POST /api/skins/upload`（multipart：`file` .css ≤300KB、`name?`、`description?`）：
  id = 文件名 stem，须匹配 `^[a-z0-9][a-z0-9-]{0,49}$`；同名 upsert。
- 校验失败 400 `{"detail"}`：css file required / skin css too large / invalid skin id / decode /
  **missing anchor**（须含 `[data-skin="<id>"]`）/ **unbalanced braces** / **forbidden in skin css**
  （`<` 字符、`expression(`、`javascript:`、含 http 的 `@import`；`>` 子代选择器合法不禁）。
- 占用内置 id → 409。
- **信任模型**：护栏 = 格式校验 + per-user 权限边界，**非 CSS 沙箱**（`data:` 等变体不在格式护栏内；
  上传者对自己会话的 DOM 负全责——只上传自己审查过的 CSS）。
- 用户仅能查看/删除自己上传的皮肤（`/api/skins/mine`、`/api/skins/{id}/css`、`DELETE /api/skins/{id}` 越权
  一律 404/401）。内置目录 `GET /api/skins`（公开）；用户偏好 `GET/PUT /api/users/me/preferences`
  （`skin_id` 校验为「内置目录 ∪ 本人已上传」，否则 400）。契约与设计见 `docs/SKINS.md`。

## 10. 管理端点补充

- `POST /api/admin/reload-config`：需请求头 `x-admin-secret: <[security].jwt_secret_key>`，否则 403。
- `GET /app/frontend/{path}`：SPA 静态资源 + `index.html` 兜底（路由前缀见 `vite.config.ts` / `router`）。
- `GET /`：有前端构建 → 307 到 `/app/frontend/`；否则 JSON 运行标记。

## 11. 认证生命周期（refresh 语义）

`POST /auth/refresh`（wave-3，2026-08-25）：持**仍有效**的 Bearer Token 调用，以单条原子 UPDATE
（`WHERE session_token = <旧 token>`）轮换 `UserSession` 行（新 `jti`，`exp ≈ now + token_expire_days`），
并刷新 `last_active_at`。该行 logout 后其 token **永久不可续期**（行缺失/并发轮换输家 → 401，不签发）；
取消/篡改 → 401；用户停用 → 403；无 Authorization 头 → 403（HTTPBearer）。前端应用启动时静默调用，
`/auth/refresh` 在 401 跳转白名单内。策略：持续活跃可无限续期（无 idle/绝对上限）——自托管单租户场景的
有意取舍。

## 12. 用户资料与用户级模型供应商覆盖（2026-09-13）

**用户资料**：`GET/PUT /api/users/me/profile`（昵称，≤50 字符，空串/`null` = 清除，展示回落 username）；
`POST /api/users/me/avatar`（multipart，≤2MB，服务端 Pillow 校验/EXIF 校正/居中裁剪 256×256 → JPEG q85
data URL 存 `users.avatar_data`，原始字节不落盘）；`DELETE /api/users/me/avatar`。`UserResponse` 在
login/refresh/me/profile 四处统一携带 `nickname` 与 `avatar_data`。

**模型供应商覆盖**：每用户每 kind（`llm`/`vlm`/`embedding`/`rerank`/`asr`/`tts`）至多一行
（`user_model_providers`）。`GET` 返回各 kind 覆盖（`has_api_key` + `api_key_tail` 掩码尾 4 位，**永不回显明文**）；
`PUT` 按 kind 增量保存（未出现的 kind 保持原状，`null` = 删除该 kind；`api_key` 缺省 = 保持原值，`""` = 显式清除）；
`DELETE` 清空全部。校验：未知 kind / 非 http(s) URL / 未知参数键 / 参数越域 → `400`。参数白名单：
`temperature`(0-2) · `top_p`(0-1) · `top_k`(≥0 整数) · `max_tokens`(≥0 整数) · `presence_penalty`/`frequency_penalty`(-2..2)，
`null` = 不覆盖。

**覆盖语义**：覆盖在该用户的任务上下文（HTTP/WS 鉴权、后台任务、定时任务、记忆 per-user 调度）内生效，
经 `ModelRegistry.get/resolve/endpoint_for_assistant` 统一改写端点；未设置覆盖的用户零行为差异。
- 自定义 `base_url` 时 API Key 一律按用户填写发送；缺省则发空（`no-key` 哨兵）——**绝不回落系统 Key**；
  仅覆盖 model/params 时沿用系统 URL 与 Key。
- 系统级任务（启动迁移、无用户上下文的后台扫描）永远走系统端点。
- 显式 purpose 路由（judge/vlm/记忆抽取等）同 kind 一并覆盖（用户可只覆盖部分 kind 精细控制）。
- **ASR 覆盖特例**：自定义 ASR URL 时按通用 HTTP 引擎（`POST {base_url}/transcribe`，携带用户
  填写的 Key，缺省则不发送）调用；实时语音对话仅支持 DashScope/MiMo 引擎——设置自定义 ASR URL
  后该用户的语音对话不可用（设置面板已提示此限制）。

## 13. 同步 Sync（S1 波 2026-09-24；W7/W8 收口；前缀 `/api/sync`，全部 JWT）

**域与捕获**：同步域实体 = conversations · conversation_groups · messages · notes · notebooks ·
assistants · user_asr_hotwords · user_skills · skill_files（父归属 user_skills）· user_model_providers
（自然键 upsert + 删除自然键兜底）· user_profile（users 自行四列白名单：nickname/avatar_data/
ui_preferences/agent_permissions）· 记忆域（`[sync] memory_enabled` 默认 true；pgvector 缺失主机自动剔除）。
表名即 `entity_type`（user_profile 为虚拟类型）。服务端对同步域模型的 ORM insert/update/delete
同事务写 `sync_events`（append-only，`seq` 自增即全端游标；`payload`=全行快照 JSON，delete 为 null）；
delete 同时写 `sync_tombstones`（唯一键 `(user_id, entity_type, entity_id)`，**不随龄裁剪**）。
密钥列（api_key 等）自 D-16 起随同步（用户原则=同账户所有数据一致；自托管+TLS 威胁模型）。
本仓为纯服务端（Web 端 + 同步中心）；`sync_client`/`sync_workspace` 为客户端实例独有组件
（纯服务端部署不存在；blob 懒穿透 import 失败即跳过）。

**端点语义**：
- `POST/GET /api/sync/devices` · `POST /api/sync/devices/{id}/revoke`（设备注册/列举/幂等撤销；
  撤销后 push 与带 device_id 的 delta 均 403）。
- `GET /api/sync/delta?cursor=C&limit=N&device_id=D`：返回该用户 `seq > C` 的事件窗口（limit 默认 200、
  上限 1000，按 seq 升序），窗口内按 (entity_type, entity_id) Joplin 四规则压缩（create+delete→消除、
  create+update→create 最新态、update+delete→delete 等）；`device_id` 与事件 origin_device 相同时
  抑制（SQL 侧、LIMIT 前）。返回 `{events, cursor, has_more, min_available_seq, resync_required}`：
  `min_available_seq`=本用户现存最小 seq（空=None）；**`resync_required`（W7 项8 + W8 8a）**=
  `cursor>0` 且（越过保留沿 `cursor < min_available_seq-1`，或低于 per-user 裁剪水位
  `cursor < max_deleted_seq`——`sync_prune_watermarks`，日志被全部裁剪时 min=None 仍可判）。
  **零事件用户（无水位、无存活事件）不再误报**（D-w8-1：旧判定对 `min_seq is None` 恒真，
  遵约客户端会每 tick 全量 resync 风暴）。客户端收到信号应放弃增量、走全量 resync
  （「绝不静默返回不完整数据」；对齐 Joplin resyncRequired / smrt floor 范式）。
- `POST /api/sync/push`：body `{device_id, events:[{entity_type, entity_id, op, payload?}]}`；
  与客户端 pull 同一 LWW 仲裁核心（`sync_apply.apply_change`，双端逐字节同源）。create 不存在则插入/
  已存在按 LWW；update 不存在则 upsert，存在要求 `payload.updated_at` 严格大于现有行否则 skipped；
  delete 存在即删。per-event SAVEPOINT（单事件异常只回滚自身，applied/skipped 不谎报）。
  应用成功同样产生服务端事件（origin_device=device_id）。父引用归属校验（notes→notebook、
  messages→conversation、conversations→assistant/group），越权一律 skipped。返回
  `{applied:[entity_id], skipped:[entity_id]}`。
- `POST /api/sync/resync`：全量基线（首连/游标失效/手动）。**游标安全（W8 8c 加固 D-w8-5）**：
  取 **per-user** `cursor=max(该用户 seq, per-user 裁剪水位)`（非全局 max——跨用户提交不污染
  游标；水位设底防 delta 恒提示重同步），读取次序= **游标 → 墓碑 → 快照**；轮末复查游标，
  推进则重试（≤3）——窗口内提交的实体要么 seq>游标（delta 补偿）、要么已提交必落入重试轮
  墓碑/快照。残留边界（同用户并发事务 seq 倒挂：分配先于游标、提交晚于末次快照读取）由后续
  resync 收敛（墓碑永不裁剪+全量回放）。`messages` 快照外置桩以全文序列化（W7 6.1：批量+sha
  校验；孤儿/不符保持桩）。  **墓碑回放（W8 8b；payload=W9 项9）**：响应含
  `tombstones:[{entity_type, entity_id, deleted_at[, payload]}]`（本用户、域内、memory 随开关门控；
  体积∝删除史，单条≈130B）——`payload` 仅自然键类型非空（user_model_providers 写
  `{kind, provider}`，供对端 `apply_change` 删除的异 id 归并行兜底；空载荷存 SQL NULL，
  重复删除以非空载荷补写空载荷=coalesce 治愈）；**客户端消费须转发 payload**
  （W10 client 波配套；W9 客户端传 None → 该类归并行暂不可删）。**客户端契约=先应用墓碑再并快照**
  （墓碑=历史删除事实、快照=当前状态；先删后并可让同 id 重建行以快照为准）；旧客户端忽略该字段
  （维持 merge-only，不劣化）。响应
  `{entities:{<type>:[全行快照…], user_profile, model_config?}, cursor, tombstones}`。
  **模型池配置随同步（D-21）**：`model_config` 虚拟实体（`[endpoints]/[routing]/[defaults]` 同源；
  `model_config_enabled`/`model_config_include_keys` 双闸门）。
- `PUT /api/sync/blobs/{sha256}` · `GET /api/sync/blobs[/{sha256}]` · `GET /api/sync/blobs`（S3 内容
  寻址；PUT 实算 sha 校验、`blob_max_mb` 上限、原子落盘、幂等去重；GET 支持 Range 206 断点续传；
  列表元组游标分页）。
- `GET /api/sync/conflicts`：LWW 败方留痕（direction=push/pull；reason=skipped_remote /
  lww_local_newer / lww_remote_newer / apply_error）。
- `GET /api/sync/status` · `GET/PUT/DELETE /api/sync/account` · `POST /api/sync/account/resync` ·
  `POST /api/sync/account/sync-now`：绑定与状态面（客户端实例语义；密码永不回显；换绑向前看——
  push_cursor=本地 max seq、pull_cursor=0、换发 device_id）。

**保留与裁剪（W7 项8 + W8 8a/8d）**：手动工具 `backend/scripts/prune_sync_events.py`
（dry-run 默认；`--execute` 真删；`--days` 默认 90，≤0=no-op；dry-run 打印
candidates/protected/deletable 供执行前核对保护面）。按龄删除 `sync_events`（created_at NULL 遗留行
按最老计入）；**墓碑不随龄裁剪（D-w4）**；**push 侧保护（D-w8-4/D-w8-6）**：仍可能被推送的未推送
事件只留不删——绑定在场（`sync_state.server_url`+`cloud_username` 非空；解绑可续推故不因停用/清密码
解除）、`seq > push_cursor` 且 `origin` 非 `rx:`（与客户端 `_push` 选择面同源）；**per-user 裁剪水位
（D-w8-1）**：删除前按用户记 `sync_prune_watermarks.max_deleted_seq`（max 合并、只增不减）供 delta
缺口判定。⚠ 旧客户端未消费 `resync_required` 时越窗会静默漏事件——手工执行前确认设备在线或可全量重同步。

**仲裁语义（sync_apply，双端逐字节同源；红线 B）**：LWW 时钟=行 `updated_at`（严格大于才应用；
等值即败——发射侧保证真实变更必 bump）；messages 追加型（create 幂等、update 恒 skipped、
delete 存在即删）；**唯一窄例外（W7 6.2，D-w1）**：messages create 撞已有行且本端某外置字段为
不可解析孤儿桩（W8 8d 起存值信任用后端签发桩形 `stub_markers_trusted`，合法桩形内容不误伤；坏 meta
的桩不可解析也不可治愈——保持原样、绝不引入伪内容）而入站携非桩全文时治愈该字段；user_profile 用
profile 事件流时钟（防登录 churn）。**skill_files 归属（W9 项10）**：`owns` 经
`SkillFile.skill_id → UserSkill.user_id` 双检——此前缺分支致 delete/update 恒 SKIPPED（潜伏），
修复后 owner 可删/可更（LWW 时钟仍为空 → 常规 capture 更新因缺 `updated_at` 恒 SKIPPED；手造
带 `updated_at` 的 push 可应用，租户面由 owns 兜底）。

### 本仓客户端补充（本地语义与端点；同步波保留，勿被上游浓缩版覆盖）

> 上游 API.md §13 为浓缩版（W8 收口）。以下为本仓客户端专属端点与决策注记（D-13/D-14/D-17/D-21/D-22 · 判据#4/#5/#8 · workspace 物化），语义单一真源=本节；resync 响应契约（tombstones/游标水位）以上方 §13 上游版为准。

- `PUT /api/sync/account`（per-user）：自助绑定。body `{server_url, cloud_username, cloud_password}`；
  先真实登录远端 + 探测 sync 面（仅 200 接受），凭据错/远端不可达/无 sync 面均 400 不落库。
  密码仅存本机 DB（同盒威胁模型，D-13）。**重绑语义**：同身份（同 server+同账号）= 纯换密码，
  游标续推；换身份 = **向前看**（`rebind_reset: true`）：push_cursor 取本地当前最大 seq
  （业务表全局主键——全量历史推新账号必撞旧账号同 id 行；旧数据留旧账号，新账号从换绑点
  起同步，搬家走 export/import）+ pull_cursor=0（拉新账号既有数据）+ 换发 device_id。
  换绑后同步范围：仅「换绑点起新建的结构」流入新账号；父在旧账号的旧实体编辑被服务端
  skipped（全局 PK+父归属约束）并在 sync_conflicts 留痕（冲突面板可见）。
- `DELETE /api/sync/account`：解绑（停同步+清密码；游标保留——仅同身份重绑续推安全，D-14）。
- `GET /api/sync/status`（S4+per-user）：`{server_enabled, client_enabled, server_url,
  cloud_username, device_id, push_cursor, pull_cursor, pending_events, last_sync_at, last_error,
  conflicts_count, resync_pending}`；`client_enabled`=本人已绑定且启用；`resync_pending`=手动
  全量请求尚未被 worker 周期消费完（前端据此把「全量完成」锚定到真正跑完 resync 的周期）。
- `POST /api/sync/account/resync`（2026-09-30 判据#4）：手动重跑全量基线（调度语义）。重置
  `pull_cursor=0 / last_sync_at=null / last_error=null`，登记 resync_pending 因果锚并 wake
  worker 立即跑周期（不等 interval tick）。未绑定 → `400`。返回 `{resync: "scheduled"}`。
  完成信号（前端轮询）：`last_sync_at` 前进 且 `resync_pending=false`。
  **限频（判据#8，A4.9 A#11/B#10）**：per-user 5s 冷却（与 sync-now 共享窗口），冷却内 →
  `429` + `Retry-After: 5`。
- `POST /api/sync/account/sync-now`（2026-09-30 判据#5）：启动期轻量 delta 同步触发（不重置游标）。
  绑定判定与 worker 对齐（server_url/cloud_username/cloud_password 四字段俱全）：可同步 →
  清 `last_error` + wake + `{scheduled: true}`；未绑定/半残 → `200 {scheduled: false}`（不报错，
  启动路径零打扰，前端静默走离线模式）。**限频**：冷却内 → `200 {scheduled: false, throttled: true}`
  不 wake（throttled≠未绑定——前端照常等待在途周期）。
- 模型池配置随同步（D-21/D-22，2026-09-29）：resync 响应含虚拟实体 `model_config`（单行；服务端
  合并配置的模型段 `[endpoints]/[routing]/[defaults]`，字段与服务端 registry 同源）。客户端白名单
  落盘 `config_model.toml`（0600，保留本地非模型段）并热重载注册表 → 助手模型列表等数据源与账户侧
  一致。开关（[sync] 段）：`model_config_enabled`（默认 true；多租户/开放注册部署置 false 规避部署级
  api_key 下发给注册用户）· `model_config_include_keys`（默认 true；置 false 时递归剥离密钥形字段，
  仅同步端点结构）。无模型段的服务端不下行该实体；旧客户端遇未知实体安全跳过（apply_change → SKIPPED）。

**workspace 文件物化（2026-09-29 波；前缀 `/api/sync/workspace`，全部 JWT）**：
本地栈预览走本地 workspace 路径，云端上传的文件（另一设备/Web 端）本地缺失会 404。本波做
**云端→本地单向物化**（不做上行/删除传播）：全量同步（首连 resync / 手动 `/api/sync/account/resync`）
后按 `[sync] workspace_auto_max_mb` 自动拉取小文件；大文件走预览按需同步。端点：
- `GET /api/sync/workspace/status?path=<rel>`：`{bound, local_exists, local_size, remote_exists,
  remote_size, auto_max_mb, max_mb}`；本地已有则跳过远端探测（remote_* 为 null）；未绑定/探测失败
  `remote_exists=null`（未知），前端走原语义。path 接受 workspace 相对路径，**也接受旧附件的
  部署侧绝对路径**（`/…/user_workspaces/<账号段>/<相对路径>`）——内部归一化后工作；无法归一的
  绝对路径/非法形 → `400`（`workspace_sync_invalid_path`）。
- `POST /api/sync/workspace/fetch`：body `{path}`，按需单文件同步（大文件预览「同步」按钮入口）。
  大小上限 `[sync] workspace_max_mb`（默认 1024）。path 同 status（相对路径或旧绝对路径归一化）。
  错误映射：`400`（未绑定/非法路径）· `404`（远端无此文件）· `413`（超上限）· `502`（远端不可达/下载失败）
  · `500`（本地写入失败）。成功返回 `{ok, path, size}`（path 为归一化后的相对路径）。
  路径安全：`../`/绝对路径/反斜杠/NUL 拒绝；落点 resolve 后必须位于本地 workspace 内（符号链接逃逸拒绝）。

---

*文档版本：详版（2026-09-14）。OpenAPI 快照 diff 即接口变更清单；`tools/gen_api_md.py` 可重放。*

## 设备执行器 Device Executor（本地侧，PLAN_device_executor P0）

前缀 `/api/client-exec`（`app/api/client_exec.py`，client-only 不回传上游）。executor worker
（`app/services/device_executor_service.py`）与云端协议契约如下；云端面（`/api/devices*`、
`/api/device/ws`）在 chatbot 仓实现（主方案 §6.2）。

### GET `/api/client-exec/status`
**Device Executor Status**

认证：JWT　|　源码模块：`app/api/client_exec.py`

**输出** `200`（DeviceExecStatus）：`enabled` · `paired` · `connected` · `auth_failed` ·
`device_id` · `name` · `platform` · `cloud_url` · `last_heartbeat_at` · `last_error` · `inflight_calls` ·
`identity_bound`（绑定记录携带 local_user_id 或云端身份）· `name_gate_ready`（名门期望值可算：
配对云端身份在档或 sync 绑定已回填 cloud_user_id；false=面板提示重新配对/重新提交同步绑定）·
`build_id`（构建世代：env `WEAVE_BUILD_ID` → 打包 `BUILD_ID` 文件 → `"dev"`）·
`impl_marker`（实现标记 `W20-device-identity`，判代用）· `cloud_username` · `cloud_user_id` ·
`workspace_ctx_expected`（名门期望值 `{slug}-{uid8}`，Hub 公式导出）。
**字段数据面**（评审 r2-F2）：`paired/device_id/name/platform` 取凭据面（reset 后空）；
`cloud_username/cloud_user_id/workspace_ctx_expected` 取墓碑面（reset 后仍展示，UI 标注
「墓碑留存」）。不含 token（凭据永不回传）。

### POST `/api/client-exec/pair`
**Pair Device**

认证：JWT　|　请求体：`pairing_code`（string，必填）· `cloud_url?`（string，缺省=sync 绑定
server_url 或 `[client_exec] cloud_url`）· `name?` · `platform?` · `device_id?`（string，重配对
保代：复用旧绑定/reset 墓碑的 device_id，缺省由后端回落——历史会话 `execution_target`
连续性）· `tls_skip?`（bool，自签内网）

行为：sidecar 凭配对码调云端 `POST /api/devices/pair`（TLS；body `{code,name?,platform?,device_id?}` → `{device_id,token,name?,user_id?,username?}`）换长效 device token，**仅此一次明文**
进入 sidecar，0600 落盘 `$LOCALSTACK_HOME/app/device_exec.json`（不进日志/不回传前端）。
**换绑保 device_id**（W21）：`device_id` 请求链=`req`（`"new"`=强制新代逃生口）→ 旧绑定
（含 reset 墓碑；仅同 `cloud_url` 复用）→ 空（首配）。**过渡态**：云端同 id 重注册语义
（同行换 token 保身份）尚待上游 handoff——当前上游可能 unique 约束 500，客户端**自动去
device_id 降级重试一次**（配对成功但换代），响应 `device_id_changed: true` 标注；响应
`device_id` 与请求不一致亦标注该字段。字段 `device_id?` 超长（>128）走 `422`。

**输出** `200`：`device_id` · `name` · `platform` · `cloud_url`（无 token）。
错误：`400`（缺配对码/云端响应畸形）· `4xx/5xx` 透传云端状态（错码/过期码）· `502`（云端不可达）。

### POST `/api/client-exec/reset`
**Reset Local Credential**

认证：JWT　|　请求体：无

行为：删除本地 token（撤销权在云端 web；本地 reset=断连+拒新调用）并唤醒 worker 重读凭据。
**墓碑语义**（W21）：只清 `token`——`device_id`/身份键/`cloud_url` 留存，后续重配对复用旧
device_id（历史会话 `execution_target` 不孤儿化）；无 token 记录对凭据面（连接/调用）仍
fail-closed。

**输出** `200`：`{"reset": true|false}`（false=无 token——未配对或已墓碑态）。

### WS 协议契约（executor ↔ 云端 `/api/device/ws`，双端共同遵守；W16b=上游已落地代码为准）

- 传输：设备出站长连接（WSS）；握手 `Authorization: Bearer <device token>` 头（token 不进 URL/日志）。
- 信封：全部消息带 `"jsonrpc": "2.0"`；请求 `{jsonrpc, id, method, params}` 且 **id ≡ call_id**
  （params 不含 call_id）；响应 `{jsonrpc, id, result|error}`；通知 `{jsonrpc, method, params}`。
- 云→设备 method：`initialize` · `tools/call {tool, args, timeout_seconds, workspace_ctx, script?, script_ext?}` ·
  `tools/cancel {call_id}`（call_id=被取消请求的 id；云端发后不候应答）· `fs/list {path}` ·
  `fs/read {path, offset, limit}` · `fs/download {path}` · `fs/zip {path}` ·
  `fs/office-html {path}` · `fs/office-pdf {path}`（W19 P1；三方法 params 恰 `{path}`=workspace 相对）。
- 设备→云结果：
  - `tools/call` → `result {"ok": true, "content": "<工具结果串>"}` 或 `result {"ok": false, "error": "<code: msg>"}`；
  - `fs/list` → `result {"ok": true, "path", "entries", "truncated", "total", ...}`（SPA 读 `entries`；
    目录条目 `type="dir"`+`is_dir:true`，文件条目 `type`=`_guess_type` 归一）；
  - `fs/read` → `result {"ok": true, "content", "offset", "limit", "total", "eof", ...}`；
  - `fs/download` → `result {"ok": true, "data_b64", "sha256"}`（≤CHUNK_SIZE 单包）或
    `result {"ok": true, "transfer_id", "sha256", "size", "chunks"}` **先于 chunk 流**
    （W19 D-91：result 先行——上传走云端汇编窗，不挤 RPC 等待窗）+ chunk 流：
    通知 `{method:"chunk", params:{transfer_id, seq, data_b64, sha256(本块), last:true(末块)}}`（逐块 sha256 校验）。
  - `fs/zip`（目录→zip 字节）· `fs/office-html`（表格→HTML 字节）· `fs/office-pdf`（office→PDF 字节）→
    **result 与 `fs/download` 同形**（单包 `{ok, data_b64, sha256}` 或 `{ok, transfer_id, sha256, size, chunks}`+chunk 流）；
    产物在设备侧生成（zip=打包目录；office=设备随包 LibreOffice `dist_lo/` soffice 转换）。
  - 协议级失败 → `error {"code", "message", "call_id"}` 信封（云端取 `message`）。
  - `heartbeat {device_id, ts}` · `progress {call_id, note}`（可选）通知。
  - `initialize` 为设备侧可选应答（云端暂无发起方），`result {ok, content: <caps json>}`。
- 语义：at-most-once（断线取消在途，不重放）；`tools/call` 等待上限 = `max([client_exec]
  call_timeout_seconds, timeout_seconds)`；`tools/cancel` 同时可中止在途 `fs/*`
  （call_id=请求 id；zip 打包/office 转换被取消后排空后台线程再清理临时件）。
  字节通道注意（D-91 收口）：分块传输 **result 先行**——`rpc_call` 等待窗只覆盖
  **生产**（zip 打包/office 转换/预哈希），**上传**随 chunk 流落在云端汇编窗
  （`_assemble_transfer` 自 result 起独立计时）；「汇总带全量摘要」故生产必先于宣告
  （固有代价）——极慢生产（超大目录树）请上调两侧 `[client_exec] call_timeout_seconds`。
  `fs/zip`·`fs/office-*` 重活并发上限 4（D-92），超限 `ok:false` + `too_many_in_flight`；
  轻量 `fs/list|read|download` 不受此限。
  生产侧加固（W19 补波 2）：`fs/zip` 打包限时=生产预算（下述 `call_max_extension_seconds`），
  超限 `ok:false` + `timeout: fs/zip build exceeded Ns`（先回错误再排空后台线程）；
  分块路径以预哈希实测 size 预检 `[client_exec] max_payload_mb`，超限 `ok:false` +
  `payload_too_large`（不发任何字节/汇总，三面同检；**消息含「超过」=云端 oversize 分类词**，
  `remote_dispatch_router` 据此回「文件过大」文案——HTTP 413 状态面待上游把
  `payload_too_large` 映射 DeviceOversizeError，见 D-93 handoff）。
  progress 发报规范（W19 补波 3，DOWNSTREAM_TODO_20261011 §一）：
  `fs/zip`·`fs/office-*` 必发、`fs/download`·`tools/call` 建议发、`fs/list|read` 不发（快）；
  间隔 `min(30s, 窗/3)`（下限 1s）；`call_id`=请求 id 逐字回显；`note` 单行 ≤60 无控制字符
  （设备发报前消毒，云端入库再截断）；终态（result/error/超时/取消）即停发，无结束消息；
  progress=存活信号（at-most-once 不变）。等待窗由云端按原窗续期（每 tick 截止前移），
  硬上限 `[client_exec] call_max_extension_seconds`（**本仓同键镜像**=RPC 生产预算：
  zip 打包/RPC 路径 office 转换超时；缺省 3600，0=不限制，非法回落 3600；本地 HTTP
  `/api/files/office-*` 仍用 `office_preview_timeout_seconds`；**0=不限制仅及 zip 打包线程，
  RPC office 转换在 0 时回退 `office_preview_timeout_seconds` 兜底**（soffice 必须有限
  超时=进程组 kill 边界，D-94；长文档请设正预算或上调 office 钮）。
  `workspace_ctx`=名门期望值（W20 契约）：**云端身份导出**
  `{slug(cloud_username)}-{cloud_user_id[:8]}`（与云端 `remote_dispatch_router._workspace_ctx`
  同式同源；**跨栈不要求 uuid 相等**——期望值两侧都从云端身份算，本地身份只决定在哪个本地
  用户 workspace 执行）。设备无云端身份（配对记录/sync 绑定均缺）时回 `identity_unbound`
  fail-closed；仅 `workspace_ctx` **键缺省**（真旧云端）放行——键在但 None/空串/非字符串一律
  `workspace_mismatch` 拒绝（falsy 不得冒充缺省；绝不接受绝对路径）。
  **过渡态**（上游 pair 响应尚未带身份字段期）：期望值回落 sync 绑定 `cloud_user_id`
  （存量绑定由 sync 周期惰性回填 / 重新提交同步绑定触发）；两侧均缺 → `identity_unbound`
  （面板 `name_gate_ready=false` 提示）。云端改名/换绑后须重新配对或重新提交同步绑定；
  `fs/*` 的 `path` 一律 workspace 相对（`is_within` 包含性校验 fail-closed）；
  `script?` = 脚本纯内容字符串 + `script_ext?` = 扩展名（skill_run_script 内联脚本，
  物化 `{workspace}/skill_scripts/` 后经 `_exec_skill_script`）。
- 错误码：`unauthorized` · `tool_not_allowed` · `identity_unbound`（W20：设备未绑定云端执行身份）· `workspace_mismatch` · `workspace_unavailable` ·
  `workspace_path_invalid` · `workspace_path_outside` · `workspace_path_not_found` ·
  `timeout` · `cancelled` · `call_not_found` · `duplicate_call_id` · `too_many_in_flight` ·
  `payload_too_large`（字节通道 oversize 预检，W19 补波 2）·
  `method_not_found` · `internal_error` · `office_preview_unavailable`（预览禁用/无 soffice，
  W19 三方法；=本地 `/api/files/office-*` 501 语义）· `office_preview_failed`（类型不符/转换失败，
  =本地 400/422 语义）（结果面错误随 `ok:false` 的 error 串携带）。
- 安全（§2.1）：executor 丢弃入站 args 一切 `_` 前缀键（云端 `strip_internal_args` 出境亦剥，双保险）；
  `remote_call` 上下文下权限门与包含性检查无条件执行（无视 `_permission_granted` 与
  `super_admin_bypass`）；`allowed_tools` 白名单镜像 fail-closed（显式空表=deny-all）。
  白名单镜像 16 名（与云端 `DEFAULT_REMOTE_ALLOWED_TOOLS` 同源，W18 G2 起）：
  `workspace_read` `workspace_write` `workspace_edit` `workspace_glob` `grep` `diff` `word_count`
  `terminal` `process` `git` `execute_code` `skill_run_script` `provide_file` `provide_folder`
  `citation_render` `pdf_export`。
