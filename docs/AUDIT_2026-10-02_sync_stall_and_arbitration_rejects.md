# 审计报告 — 客户端同步两事故（2026-10-02）

**任务**：① 大量 `conversations/* 推送 被云端仲裁拒绝` 根因；② 「单击全量同步后一直显示正在同步中」根因；③ 60 服务器部署信息核查。
**性质**：C4/C6 只读审计（零源码改动）。证据 = 本地侧车日志 + 本地 PG（55432）实测 + 60 API 只读探针 + git diff + SQLAlchemy 最小复现。

---

## 结论速览

| # | 现象 | 根因 | 严重度 | 修复方向 |
|---|------|------|--------|----------|
| 1 | 24 条 conversations + 1 条 note 推送"被云端仲裁拒绝"（10/1 15:08:54 / 14:40:37） | LWW 严格大于判定（`sync_apply.py:263`）下**时间戳相等即判 STALE**；触发链 = 会话重排对同值 `(group_id, sort_order)` 的 ORM 同值赋值 → SQLAlchemy 执行 UPDATE 并触发捕获、但**跳过 onupdate** → 事件 payload 带旧 ts（8/4）→ 与云端行 ts 逐毫秒相等 → STALE | Medium（排序/分组变更对这 24 条静默不上行；内容无损失） | reorder 端点显式 `updated_at = utcnow()`；或 sync_apply 对相等 ts 走幂等应用 |
| 2 | 全量同步点击后永挂"正在同步中" | **commit `a29cce0`（10/1 13:46 上游同步波）误删 `server/main.py` 里的 `await sync_client.start()/stop()` 与 `sync_workspace_api` 路由**（上游 chatbot=纯服务端、无客户端 worker，其 main.py 覆盖了本仓 main.py）。重启后的侧车进程（今日 PID 1428）worker 从未启动 → 点击 resync 只在死单例上 `_resync_pending.add()+wake()`，无消费者 → `/api/sync/status.resync_pending` 恒 true → 前端 `waitSyncComplete` 永不 done | **Critical**（同步功能整机瘫痪 + UI 永挂） | 恢复 main.py 的 start/stop 与 sync_workspace 路由（见下 diff）；上游合并波需带「本仓专属启动项清单」防再犯 |
| 3 | 60 服务器部署信息 | 现行=09-30 ASR 波（chatbot `a5c8d5551`，deploy_60.sh rsync backend/）；此前 09-29 模型池波 `47ba4486b`；经 API 指纹核实：`/api/sync/workspace/*`=404、`/api/sync/status` 无 `resync_pending` 键（= 早于本仓 09-30 因果锚波，属预期双端代差） | Info | SSH 凭据已失效（详见 §4），下次部署前需用户重置 |

---

## 1. 同步机制现状（问题二前半）

Joplin 式 delta 同步（事实源 `server/API.md` 二.13）：

```
本地写 → sync_capture（mapper 事件同事务写 sync_events，origin 防回显）
   ↓ worker 周期（interval=30s, batch=200, 单周期≤10 批）
push: 本地 sync_events(seq>push_cursor, 非 rx:*) → POST /api/sync/push
      服务端逐事件 SAVEPOINT + apply_change = LWW 仲裁（updated_at 严格大于才应用；
      否则 STALE→skipped）→ skipped 回程记 sync_conflicts(direction=push,
      reason=skipped_remote) → UI「被云端仲裁拒绝」
pull: GET /api/sync/delta?cursor → apply_change 应用（rx:<origin> 防回显）→ 冲突留痕
blob: 内容寻址 + 上行 diff + 懒穿透续传；resync: 全量快照基线（新域/新设备）
```

- 双端共用同一 LWW 核心 `sync_apply.apply_change`（"两端仲裁规则逐字节一致"）。
- 仲裁判据（`server/app/services/sync_apply.py:263`）：`current_ts is not None and new_ts <= current_ts → STALE`（**相等也算败方**）。
- 完成信号契约（前端）：`last_sync_at` 前进 **且** `resync_pending=false`（`app/src/composables/useDataRefresh.ts`）。

## 2. 事故一：「被云端仲裁拒绝」×24（证据链）

**实测（本地 PG sync_conflicts）**：24 条 conversations 的 `local_updated_at` 全部 = `2026-08-04 16:58:18.xxx`（同一秒），conflict created_at = `2026-10-01 07:08:54Z`。

**实测（60 云端 API，alice）**：同 id 行 `updated_at` 与推送 payload **逐毫秒相等**——
`a2eeff41: 2026-08-04T16:58:18.706498Z`（本地 payload == 云端行）。

**判定**：相等 ts → `new_ts <= current_ts` → STALE → skipped_remote。**为什么相等**：

1. 这 24 行由 09-28 18:12 的 resync 以 `rx:resync` 落地，`apply_change` 显式赋 `row.updated_at = 云端旧值(8/4)`；
2. 10/1 15:08:41 前端 `PUT /api/conversations/reorder`（264 项）对本仓 `conversation.py:835-837` 逐行 `conv.sort_order=...; conv.group_id=...`——其中 240 项真变（onupdate 生效，payload ts=07:08:41），**24 项与现值完全相同**（reorder 事件 payload (group_id,sort_order) 与 09-28 create 事件完全一致，psql 4380/4384/4393 vs 6202/6210/6213 实测）；
3. SQLAlchemy 2.0.50 最小复现（本仓 venv）：**同值赋值 → UPDATE 照常执行、after_update 照常触发（同步事件被发射）、但 onupdate 被跳过** → 事件 payload 带旧 ts；
4. 推送 → 相等 → STALE ×24。

旁证：`6e10bca1` 的 update（ts 07:07:28，agent loop 收尾写入）在同一批被拒——云端已含该 ts（早一拍 push 已应用），随后的 delete（delete 不走 LWW）成功，故该会话云端已删。`notes/fa646d93`（14:40:37）同类：等值 LWW 判负。

**影响**：内容无损失；但这 24 条的分组/排序改动**静默未上行**（本地与云端排序漂移），且冲突面板被噪音淹没。

## 3. 事故二：全量同步永挂「正在同步中」（Critical）

**代码证据（git show a29cce0 -- server/main.py）**，被删除：
```diff
-    from app.services.sync_client import sync_client
-    await sync_client.start()
...
-    from app.services.sync_client import sync_client
-    await sync_client.stop()
```
同 commit 还删了 `sync_workspace_api` 的 import 与 `app.include_router(...)`。

**运行证据**：
- 侧车日志最后一条 `SyncClient started` = 10-01 09:09（进程 90500）；其后两次重启（进程 10147、当前 1428@今日 09:38）**均无该行**；
- 今日 sync_client 日志 0 条、对 <cloud-host> 请求 0 条；
- 今日 11:19-11:30 点击 `POST /api/sync/account/resync` → 200（已排入下周期）→ 此后无任何 worker 动作，仅 `/api/sync/status` 被持续轮询；
- `sync_client.py:206-213`：`_resync_pending` 仅在 `_resync` 成功后 discard；`request_resync()+wake()` 打在**无 _run 循环的单例**上 → 标记永真；
- 前端 `SyncPanel.vue:217` `waitSyncComplete(baseline, 60000, {expectResync:true})` → `resync_pending` 永不 false → 60s 提示"仍在进行"→ 后台 600s 亦永不 done。

**附带伤害**：`/api/sync/workspace/*` 本地 404（实测）→ workspace 按需同步/预览拉取链路同死。

**为什么"需要同步的数据没有这么多"仍卡死**：卡死与数据量无关——worker 根本不存在，resync 请求从未发出（对照历史：09-30 13:33 最后一次成功 resync 仅 3-5s）。

## 4. 60 服务器部署信息（问题三）

| 项 | 实测 |
|----|------|
| 部署通道/时间 | deploy_60.sh（chatbot 仓），两次部署：09-29 11:39 `47ba4486b`（备份 bak_20260929_113907）→ 09-30 01:30 ASR 波 **`a5c8d5551`**（现行，PID→1214260，deploy60_record_20260930_asr_qwen31） |
| 地址 | `https://<cloud-host>:8158`（自签 TLS；客户端 tls_verify 按绑定关闭） |
| API 指纹 | 登录 OK（alice/cafef00d）；329 conversations；`/api/sync/workspace/status`=404；`/api/sync/status` 无 `resync_pending` 键 → 与 09-29 部署代差相符（云侧不缺事故相关能力；事故 2 全部在本仓客户端） |
| SSH | **不可达**：`~/.weave-connector/secrets.json` 的 wt60-alice 凭据对 `alice@60` 与 `root@60` 均 Permission denied（expect 实测）；weave-connector 守护进程 profile 列表过期（config.toml 有 wt60-alice，服务只见 2 profiles；重启守护会杀并行任务会话，未动）→ ADR D-45。**下次部署前需用户重置凭据** |

## 5. 修复建议（未实施——本审计只读）

1. **[Critical] 恢复 `server/main.py`**：startup 里恢复 `sync_client.start()`（shutdown 恢复 stop）；恢复 `sync_workspace_api` import + include_router。上游合并波补「本仓专属启动项」核对清单，防再犯。
2. **[Medium] reorder 等同值写入**：`PUT /conversations/reorder`、`PUT /groups/reorder` 显式 `updated_at = datetime.utcnow()`（对齐 chat.py 消息落盘先例）；或 capture 层对 no-net-change UPDATE 不发事件；或 `sync_apply` 对相等 ts 做幂等应用/不计冲突。三选一即可消除该类噪音+漂移。
3. **[Info] 冲突面板**：对 `skipped_remote` 且 payload 与云端内容等价的场景降噪（或聚合展示）。
4. **[Info] 60 凭据**：用户重置 SSH 密码后重新 enroll wt60-alice；weave-connector 守护进程需重载 profile（择机重启）。

## 证据索引

- 本地 PG：`sync_conflicts` 24 行、`sync_events` seq 4380/4384/4393（create, rx:resync）vs 6202/6210/6213（update, 同值）、`sync_state`（beef0002: push_cursor=6268, pull_cursor=0, last_sync_at=NULL）
- 侧车日志（~/Library/Application Support/weave-thinker-client/localstack/backend_sidecar.log）：行 366358（SyncClient started @10-01 09:09）、413343/413344（push POST @15:08:53/54）、454227（resync 200 后无 worker 活动）
- git：`git show a29cce0 -- server/main.py`（start/stop/workspace 路由删除 diff）
- 复现脚本：`/var/folders/.../opencode/repro2.py`（同值赋值 → 事件触发 + onupdate 跳过）
- 60 探针：`/tmp/opencode/probe60.py`、`probe60b.py` 输出（登录/329 convs/workspace 404/resync_pending 缺席）
