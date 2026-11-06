# 上游 handoff ③：设备显示名云端改名端点（chatbot 仓待办，W25 契约形状）

> 背景：客户端「保存名称」改的是本地绑定记录（W22），云端设备注册表显示名不更新
> （用户报障 2026-10-15）。客户端侧已落地**改名推送链**：本地保存后自动调
> `POST /api/devices/{device_id}/rename`（凭 device token）；云端无此端点时降级
> 「本地保存+如实回执」。**上游只需补此端点**（下述两处），部署 60 后改名即全端生效，
> 客户端零改动（404 降级自动消失）。
>
> 前两项 handoff 已由上游 faf9afcab 落地（无需再做）：① 同 device_id 重注册=同行换
> token 保身份（register_device upsert）；② pair 响应 +user_id/username。

## 1. `backend/app/services/client_device_service.py` — 新增 `rename_device`

```python
async def rename_device(db: AsyncSession, row: ClientExecutorDevice, name: str) -> ClientExecutorDevice:
    """设备自改显示名（handoff ③ 形状；token/device_id 不动）。"""
    row.name = name  # 端点层已拒 >255（不静默截断——截断=云端与本地显示名静默分歧）
    await db.commit()
    await db.refresh(row)
    return row
```

## 2. `backend/app/api/client_devices.py` — schema + 端点

```python
class DeviceRenameRequest(BaseModel):
    # 家族口径（PairRequest 同款）：缺字段/null 均不走 422——处理器内 400
    name: str | None = None


@router.post("/{device_id}/rename")
async def rename_device_endpoint(
    device_id: str,
    payload: DeviceRenameRequest,
    db: AsyncSession = Depends(get_db),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """设备自改显示名（handoff ③ 形状：凭 device token；token/device_id 不动）。

    body `{name}` → `{device_id, name, renamed:true}`。`renamed:true` 是成功回执锚——
    客户端推送链按此防伪（SPA fallback/改写网关的 200 空体/HTML 伪应答不算成功）。"""
    if not get_config().client_exec_enabled:
        raise HTTPException(status_code=404, detail="device execution disabled")
    presented = credentials.credentials if credentials else None
    device = await cds.verify_device_token(db, presented) if presented else None
    if device is None:
        raise HTTPException(status_code=401, detail="device token required")
    if device.device_id != device_id:
        raise HTTPException(status_code=403, detail="token 与 device_id 不匹配")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="name 超长（>255）")
    row = await cds.rename_device(db, device, name)
    return {"device_id": row.device_id, "name": row.name, "renamed": True}
```

注：`_bearer = HTTPBearer(auto_error=False)`、`verify_device_token`（hash 查找 +
`revoked.is_(False)`）均为既有件；`cds`/`get_config` 为既有 import。路由挂既有
`/api/devices` router 下即可（`/pair` 字面段优先于 `/{device_id}/…`，无遮蔽）。

## 3. API.md 契约（随端点补录）

```
POST /api/devices/{device_id}/rename —— 设备自改显示名（云端 exec 注册表）

认证：device token（Authorization: Bearer <device token>；非用户 JWT）
请求体：{ "name": string }   —— trim 后非空，≤255；缺字段/null → 400（不走 422）
200：{ "device_id", "name", "renamed": true }   ← renamed:true 为成功回执锚
错误：401 device token required · 403 token 与 device_id 不匹配 ·
     400 name 不能为空 / name 超长（>255） · 404 device execution disabled
长度口径：云端硬限 255（拒绝不截断）；客户端 UI 自限 128
```

## 4. 语义红线（照此实现即与客户端兼容）

1. **token/device_id 不动**：改名是纯显示名修改；换 token/换绑=重配对（pair），删除=revoked。
2. **不静默截断**：>255 拒绝（400），截断会造成云端/本地显示名静默分歧。
3. **成功回执锚**：200 响应体必须带 `"renamed": true`——客户端只认它（防伪）。
4. **凭 device token**：设备自改通道；owner 侧用户 JWT 纠名通道可作后续扩展（不必本轮）。
5. 可选超集（上游自愿）：`client_exec_audit` 留痕、限速、web UI 改名入口（用户 JWT 面）。

## 5. 部署与生效

- 上游采纳后 `deploy_60.sh` 部署 60 → 客户端「保存名称」即同步云端设备列表
  （回执从「云端未同步——云端暂不支持改名」自动转「云端已同步」，客户端零改动）。
- vendor 参考实现（已测 8/8）：`server/app/api/client_devices.py` +
  `server/app/services/client_device_service.py`（weave-thinker-client W25 波）。

---

> **收口（2026-10-24 W27 同步波实证）**：本 handoff ③ 端点形状已被上游落地且为超集（rename vs revoke 竞态→404 + StaleDataError→LookupError），vendor 已随同步取上游单件（§3 W25 行销账）。云端设备列表显示名更新缺口闭合。
