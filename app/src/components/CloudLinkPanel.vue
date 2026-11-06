<template>
  <div class="cloud-link-panel">
    <section class="sync-section" data-testid="cloud-link-addr-section">
      <h3 class="sync-heading">云端地址</h3>
      <template v-if="account?.bound">
        <div class="sync-row"><span>服务器</span><b>{{ account.server_url }}</b></div>
        <div class="sync-hint">同步绑定与设备配对共用此地址；如需更换请先解绑云端账号</div>
        <div v-if="accountError" class="sync-error">
          云端账号状态未知（{{ accountError }}）——以下绑定信息可能是陈旧值，请点「刷新」重试
        </div>
      </template>
      <template v-else>
        <div class="sync-bind-form">
          <input
            v-model="cloudUrl"
            type="text"
            placeholder="云端地址（如 https://host:8158）——同步绑定与设备配对共用"
            data-testid="cloud-link-addr"
          />
        </div>
        <div v-if="accountError" class="sync-error">
          云端账号状态未知（{{ accountError }}）——绑定状态可能与显示不符，请点「刷新」重试
        </div>
        <div class="sync-hint">同步绑定与设备配对共用此地址</div>
      </template>
    </section>

    <section class="sync-section" data-testid="sync-account">
      <h3 class="sync-heading">云端账号</h3>
      <template v-if="account?.bound">
        <div class="sync-row"><span>服务器</span><b>{{ account.server_url }}</b></div>
        <div class="sync-row"><span>账号</span><b>{{ account.cloud_username }}</b></div>
        <div class="sync-row"><span>最近同步</span><b>{{ fmt(account.last_sync_at ?? null) }}</b></div>
        <div v-if="account.last_error" class="sync-error">{{ account.last_error }}</div>
        <button class="sync-revoke-btn" :disabled="busy" @click="handleUnbind">
          {{ unbinding ? '解绑中…' : '解绑' }}
        </button>
        <button class="sync-refresh-btn" :disabled="resyncing" data-testid="sync-resync" @click="handleResync">
          {{ resyncing ? '已排入下周期…' : '重新全量同步' }}
        </button>
      </template>
      <template v-else-if="accountError">
        <div class="sync-error" data-testid="sync-bind-error">{{ accountError }}——无法确认绑定状态，暂不提供绑定表单（请刷新重试）</div>
      </template>
      <template v-else>
        <div class="sync-bind-form">
          <input v-model="bindUser" type="text" placeholder="云端账号" data-testid="sync-bind-user" />
          <input v-model="bindPass" type="password" placeholder="云端密码" data-testid="sync-bind-pass" />
          <label class="sync-tls-opt">
            <input v-model="bindTlsSkip" type="checkbox" data-testid="sync-bind-tls-skip" />
            自签证书（跳过 TLS 校验，仅限自己掌控的内网服务器）
          </label>
          <button class="sync-refresh-btn" :disabled="busy || !effectiveUrl" data-testid="sync-bind-submit" @click="handleBind">
            {{ binding ? '绑定中…' : '绑定并启用同步' }}
          </button>
          <div v-if="!effectiveUrl" class="sync-hint">请先填写上方云端地址</div>
        </div>
        <div v-if="bindError" class="sync-error" data-testid="sync-bind-error">{{ bindError }}</div>
      </template>
    </section>

    <section class="sync-section" data-testid="device-exec-status">
      <h3 class="sync-heading">设备执行</h3>
      <div v-if="devError" class="sync-error" data-testid="device-exec-error">{{ devError }}</div>
      <template v-else-if="devStatus">
        <div class="sync-row">
          <span>在线</span>
          <b>
            <span class="device-exec-dot" :class="devStatus.connected ? 'on' : 'off'" data-testid="device-exec-online-dot"></span>
            {{ devStatus.connected ? '已连接' : devStatus.auth_failed ? '凭据失效（请重置后重新配对）' : '离线' }}
          </b>
        </div>
        <div class="sync-row"><span>配对</span><b>{{ devStatus.paired ? (devStatus.name || devStatus.device_id || '已配对') : '未配对' }}</b></div>
        <div class="sync-row"><span>最后心跳</span><b>{{ fmt(devStatus.last_heartbeat_at) }}</b></div>
        <div v-if="devStatus.last_error" class="sync-error" data-testid="device-exec-last-error">最近错误：{{ devStatus.last_error }}</div>
        <div v-if="devStatus.paired && devStatus.name_gate_ready === false" class="sync-error" data-testid="device-exec-identity-unbound">
          设备执行身份未就绪——请重新配对，或在本页重新提交云端绑定（回填身份）
        </div>
        <div class="sync-row"><span>云端身份</span><b data-testid="device-exec-cloud-identity">{{ devStatus.cloud_username || '（未绑定）' }}<template v-if="!devStatus.paired">（墓碑留存）</template><template v-if="devStatus.cloud_user_id"> · {{ devStatus.cloud_user_id.slice(0, 8) }}</template></b></div>
        <div class="sync-row"><span>名门期望值</span><b data-testid="device-exec-expected-ctx">{{ devStatus.workspace_ctx_expected || '（不可算）' }}</b></div>
        <div class="sync-row"><span>构建</span><b data-testid="device-exec-build">{{ devStatus.impl_marker }} · {{ devStatus.build_id }}</b></div>

        <template v-if="!devStatus.paired">
          <div class="sync-bind-form device-pair-form" data-testid="device-exec-pair">
            <input v-model="pairCode" type="text" placeholder="一次性配对码（云端「添加设备」生成）" data-testid="device-exec-code" />
            <input v-model="deviceName" type="text" placeholder="设备名称（可选）" data-testid="device-exec-name" />
            <label class="sync-tls-opt">
              <input v-model="tlsSkip" type="checkbox" data-testid="device-exec-tls-skip" />
              自签证书（跳过 TLS 校验，仅限自己掌控的内网服务器）
            </label>
            <button class="sync-refresh-btn" :disabled="busy || !pairCode.trim() || !effectiveUrl" data-testid="device-exec-pair-submit" @click="handlePair">
              {{ pairing ? '配对中…' : '配对并启用' }}
            </button>
            <div class="sync-hint">配对使用上方「云端地址」（统一地址）{{ effectiveUrl ? '' : '——未设置，请先在上方填写' }}</div>
          </div>
        </template>

        <template v-else>
          <div class="device-binding-info" data-testid="device-exec-binding-info">
            <div class="sync-row"><span>云端地址</span><b>{{ devStatus.cloud_url || '（复用同步绑定）' }}</b></div>
            <div v-if="addrMismatch" class="sync-error" data-testid="device-exec-addr-mismatch">
              设备绑定的云端地址与上方不一致——重新配对可归一
            </div>
          </div>

          <div class="device-rename-row">
            <span class="device-rename-label">设备名称</span>
            <input v-model="deviceName" type="text" data-testid="device-exec-name-edit" placeholder="设备名称" />
            <button class="sync-refresh-btn" :disabled="busy || !deviceName.trim()" data-testid="device-exec-name-save" @click="handleRename">
              {{ renaming ? '保存中…' : '保存名称' }}
            </button>
          </div>
          <div class="sync-hint">修改名称无需重新配对；「保存名称」只改显示名，不会动绑定凭据</div>

          <button class="sync-revoke-btn" :disabled="busy" data-testid="device-exec-repair" @click="repairOpen = !repairOpen">
            {{ repairOpen ? '收起重新配对' : '重新配对' }}
          </button>
          <div v-if="repairOpen" class="device-repair-box" data-testid="device-exec-repair-box">
            <div class="sync-hint">
              重新配对将清除并替换当前绑定：使用全新一次性配对码重新注册（尽力保持 device_id 连续，
              历史会话执行目标不中断）。仅修改设备名称请用上方「保存名称」，无需重新配对。
            </div>
            <div class="sync-hint">设备名称将保持：<b data-testid="device-exec-repair-name">{{ devStatus.name || '（空）' }}</b></div>
            <div class="sync-bind-form">
              <input v-model="repairCode" type="text" placeholder="一次性配对码（云端「添加设备」生成）" data-testid="device-exec-repair-code" />
              <label class="sync-tls-opt">
                <input v-model="repairTlsSkip" type="checkbox" />
                自签证书（跳过 TLS 校验，仅限自己掌控的内网服务器）
              </label>
              <div class="device-repair-actions">
                <button class="sync-revoke-btn" :disabled="busy || !repairCode.trim() || !effectiveUrl" data-testid="device-exec-repair-confirm" @click="handleRepair">
                  {{ pairing ? '配对中…' : '确认重新配对' }}
                </button>
                <button class="sync-refresh-btn" :disabled="busy" data-testid="device-exec-repair-cancel" @click="closeRepair">
                  取消
                </button>
              </div>
            </div>
          </div>

          <div class="device-reset-block">
            <button class="sync-revoke-btn" :disabled="busy" data-testid="device-exec-reset" @click="handleReset">
              {{ resetting ? '重置中…' : '重置本地凭据' }}
            </button>
            <div class="sync-hint">重置本地凭据＝清除本地凭据并断开执行连接（云端设备记录保留）；需重新配对才能恢复执行。</div>
          </div>
        </template>

        <div v-if="pairNotice" class="sync-ok" data-testid="device-exec-pair-notice">{{ pairNotice }}</div>
        <div v-if="pairError" class="sync-error" data-testid="device-exec-pair-error">{{ pairError }}</div>
      </template>
      <div v-else class="sync-loading">加载中…</div>
    </section>

    <section class="sync-section" data-testid="sync-status">
      <h3 class="sync-heading">同步状态</h3>
      <div v-if="error" class="sync-error" data-testid="sync-panel-error">{{ error }}</div>
      <template v-else-if="status">
        <div class="sync-row"><span>服务端同步</span><b>{{ status.server_enabled ? '启用' : '停用' }}</b></div>
        <div class="sync-row"><span>云端绑定</span><b>{{ status.client_enabled ? '已启用' : '未绑定' }}</b></div>
        <div class="sync-row"><span>待推送事件</span><b>{{ status.pending_events }}</b></div>
        <div class="sync-row"><span>冲突记录</span><b>{{ status.conflicts_count }}</b></div>
        <div class="sync-row"><span>最近同步</span><b>{{ fmt(status.last_sync_at) }}</b></div>
        <div v-if="status.last_error" class="sync-error" data-testid="sync-last-error">
          最近错误：{{ status.last_error }}
        </div>
      </template>
      <div v-else class="sync-loading">加载中…</div>
    </section>

    <section class="sync-section" data-testid="sync-devices">
      <h3 class="sync-heading">设备管理</h3>
      <table v-if="devices.length" class="sync-table">
        <thead>
          <tr><th>名称</th><th>平台</th><th>最近在线</th><th>状态</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="d in devices" :key="d.id" :class="{ revoked: d.revoked }">
            <td>{{ d.name || d.id.slice(0, 8) }}</td>
            <td>{{ d.platform || '—' }}</td>
            <td>{{ fmt(d.last_seen_at) }}</td>
            <td>{{ d.revoked ? '已撤销' : '活跃' }}</td>
            <td>
              <button
                v-if="!d.revoked"
                class="sync-revoke-btn"
                :disabled="revoking === d.id"
                @click="handleRevoke(d.id)"
              >
                {{ revoking === d.id ? '撤销中…' : '撤销' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="sync-empty">暂无已注册设备</div>
    </section>

    <section class="sync-section" data-testid="sync-conflicts">
      <h3 class="sync-heading">冲突记录</h3>
      <table v-if="conflicts.length" class="sync-table">
        <thead>
          <tr><th>实体</th><th>方向</th><th>原因</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in conflicts" :key="c.id">
            <td :title="c.entity_id">{{ c.entity_type }}/{{ c.entity_id.slice(0, 8) }}</td>
            <td>{{ c.direction === 'push' ? '推送' : '拉取' }}</td>
            <td>{{ reasonText(c.reason) }}</td>
            <td>{{ fmt(c.created_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="sync-empty">暂无冲突</div>
      <div v-if="conflictTotal > 0" class="sync-pager" data-testid="conflict-pager">
        <button class="sync-pager-btn" data-testid="conflict-prev" :disabled="conflictPage === 0 || pagerBusy" @click="changeConflictPage(-1)">上一页</button>
        <span class="sync-pager-info">共 {{ conflictTotal }} 条 · 第 {{ conflictPage + 1 }}/{{ conflictPages }} 页</span>
        <button class="sync-pager-btn" data-testid="conflict-next" :disabled="conflictPage + 1 >= conflictPages || pagerBusy" @click="changeConflictPage(1)">下一页</button>
      </div>
    </section>

    <div class="sync-actions">
      <button class="sync-refresh-btn" :disabled="loading" @click="loadAll">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getSyncConflicts,
  getSyncDevices,
  getSyncStatus,
  revokeSyncDevice,
  type SyncConflict,
  type SyncDevice,
  type SyncStatus,
  bindSyncAccount,
  getSyncAccount,
  unbindSyncAccount,
  resyncSyncAccount,
  type SyncAccount,
} from '../api/sync'
import { getDeviceExecStatus, pairDeviceExec, renameDeviceExec, resetDeviceExec, type DeviceExecStatus } from '../api/clientExec'
import { refreshAllData, waitSyncComplete } from '../composables/useDataRefresh'

const status = ref<SyncStatus | null>(null)
const devices = ref<SyncDevice[]>([])
const conflicts = ref<SyncConflict[]>([])
// W24 冲突记录分页：每页固定 10 条，offset 分页走既有 /sync/conflicts 契约（红线零变更）
const CONFLICT_PAGE_SIZE = 10
const conflictPage = ref(0)
const conflictTotal = computed(() => status.value?.conflicts_count ?? 0)
const conflictPages = computed(() => Math.max(1, Math.ceil(conflictTotal.value / CONFLICT_PAGE_SIZE)))
const account = ref<SyncAccount | null>(null)
const accountError = ref('')
const devStatus = ref<DeviceExecStatus | null>(null)
const lastDeviceId = ref('')
const nameServer = ref('')
const loading = ref(false)
const revoking = ref<string | null>(null)
const error = ref('')
const devError = ref('')
const bindUser = ref('')
const bindPass = ref('')
const bindTlsSkip = ref(false)
const binding = ref(false)
const unbinding = ref(false)
const resyncing = ref(false)
const bindError = ref('')
const pairNotice = ref('')
const pairing = ref(false)
const renaming = ref(false)
const resetting = ref(false)
const pairError = ref('')
const pairCode = ref('')
const deviceName = ref('')
const tlsSkip = ref(false)
const repairOpen = ref(false)
const repairCode = ref('')
const repairTlsSkip = ref(false)
const cloudUrl = ref('')

// 动作互斥（评审 I5 + re-review NEW-1）：任何在途动作期间禁用全部写操作
// （含绑定/解绑自身——busy 须自含 binding/unbinding，否则双提交回归）
const busy = computed(() =>
  pairing.value || renaming.value || resetting.value || binding.value || unbinding.value)

// 统一云端地址（#21）：绑定态=账号绑定 server_url（只读）；未绑定=面板唯一地址输入。
const effectiveUrl = computed(() =>
  account.value?.bound ? (account.value.server_url || '') : cloudUrl.value.trim(),
)

const addrMismatch = computed(() => {
  const dev = devStatus.value
  if (!dev?.paired || !dev.cloud_url) return false
  const top = effectiveUrl.value
  return !!top && dev.cloud_url !== top
})

function fmt(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function reasonText(reason: string): string {
  const map: Record<string, string> = {
    lww_local_newer: '本地版本更新（远端被弃）',
    lww_remote_newer: '云端版本更新（本地被覆盖）',
    skipped_remote: '被云端仲裁拒绝',
    apply_error: '应用失败',
  }
  return map[reason] || reason
}

const pagerBusy = ref(false)
let conflictFetchSeq = 0

async function changeConflictPage(delta: number) {
  const next = conflictPage.value + delta
  if (next < 0 || next >= conflictPages.value) return
  // W24 评审 I1：in-flight 锁 + 令牌比对 + 失败回滚 + 成功清错——
  // 快速连点/翻页中刷新不得让旧响应覆盖新页（last-resolved-wins 竞态）。
  if (pagerBusy.value) return
  pagerBusy.value = true
  const prevPage = conflictPage.value
  conflictPage.value = next
  const seq = ++conflictFetchSeq
  try {
    const rows = await getSyncConflicts(CONFLICT_PAGE_SIZE, conflictPage.value * CONFLICT_PAGE_SIZE)
    if (seq !== conflictFetchSeq) return
    conflicts.value = rows
    error.value = ''
  } catch (e: any) {
    if (seq !== conflictFetchSeq) return
    conflictPage.value = prevPage
    error.value = e?.response?.status === 404 ? '同步服务未启用' : '加载冲突记录失败'
  } finally {
    if (seq === conflictFetchSeq) pagerBusy.value = false
  }
}

async function loadSync() {
  // account 独立取（评审 Critical C1）：任一子调用失败不得丢绑定态——绑定门
  // （account?.bound）失守会让绑定用户看到可编辑地址+绑定表单，提交地址与真实绑定漂移。
  try {
    account.value = await getSyncAccount()
    accountError.value = ''
  } catch (e: any) {
    accountError.value = e?.response?.status === 404 ? '同步服务未启用' : '加载云端账号失败'
  }
  try {
    // 页钳制先行（W24）：总数收缩后当前页可能越界——先按旧 status 钳制不了，改为串行
    const st = await getSyncStatus()
    status.value = st
    const totalPages = Math.max(1, Math.ceil((st.conflicts_count || 0) / CONFLICT_PAGE_SIZE))
    if (conflictPage.value >= totalPages) conflictPage.value = totalPages - 1
    const [d, c] = await Promise.all([
      getSyncDevices(),
      getSyncConflicts(CONFLICT_PAGE_SIZE, conflictPage.value * CONFLICT_PAGE_SIZE),
    ])
    devices.value = d
    conflicts.value = c
    error.value = ''
  } catch (e: any) {
    error.value = e?.response?.status === 404 ? '同步服务未启用' : '加载同步状态失败'
  }
}

async function loadDevice() {
  try {
    devStatus.value = await getDeviceExecStatus()
    if (devStatus.value?.device_id) lastDeviceId.value = devStatus.value.device_id
    const srvName = devStatus.value?.name || ''
    // 名称输入防覆盖（评审 I3）：仅当未在编辑（值为空或=上次服务端名）才回灌
    if (!deviceName.value || deviceName.value === nameServer.value) deviceName.value = srvName
    nameServer.value = srvName
    devError.value = ''
  } catch (e: any) {
    devError.value = e?.response?.data?.detail || e?.message || '状态读取失败'
  }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([loadSync(), loadDevice()])
    if (!cloudUrl.value) {
      cloudUrl.value = account.value?.server_url || devStatus.value?.cloud_url || ''
    }
  } finally {
    loading.value = false
  }
}

async function handleBind() {
  binding.value = true
  bindError.value = ''
  try {
    await bindSyncAccount({
      server_url: effectiveUrl.value,
      cloud_username: bindUser.value.trim(),
      cloud_password: bindPass.value, // vw-approved: Vue ref 取值填入请求体，非字面凭据
      tls_verify: !bindTlsSkip.value,
    })
    bindPass.value = ''
    await loadAll()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    bindError.value = detail
      || (e?.response
        ? '绑定失败（请检查地址与凭据）'
        : '本地服务不可达（后端未启动或端口被占用）——请重启应用后重试')
  } finally {
    binding.value = false
  }
}

async function handleResync() {
  resyncing.value = true
  try {
    // 判据#4（2026-09-30 用户追加）：全量同步「完成后」不切页自动刷新三模式可见数据。
    // resync 端点=调度语义（resync:scheduled，服务端 wake 后周期跑），完成信号=
    // last_sync_at 前进 且 resync_pending=false（A4.9 评审 B#2：锚定到真正跑完
    // 全量 resync 的周期；陈旧 last_error 不算本次失败——A#1）。
    let baseline: string | null = null
    let baselineError: string | null = null
    try {
      const st = await getSyncStatus()
      baseline = st.last_sync_at
      baselineError = st.last_error
    } catch { /* 状态面不可达则按 null 等首盖章 */ }
    await resyncSyncAccount()
    error.value = ''
    const waitResult = await waitSyncComplete(baseline, 60000, {
      expectResync: true, baselineError,
    })
    if (waitResult === 'done') {
      error.value = ''
      await refreshAllData()
      await loadAll()  // W24 评审 I2：refreshAllData 不含本面板——冲突是 resync 主产出口，须同刷
      // N1：清错先行——loadAll 失败时其 error 不得被事后抹掉
      if (typeof (window as any).$toast === 'function') {
        ;(window as any).$toast('全量同步完成，数据已刷新')
      }
    } else if (waitResult === 'failed') {
      error.value = '全量同步失败：服务端回报错误，详情见「最近错误」'
    } else {
      // 大账号全量可能 >60s（A4.9 评审 A#12/B#8）：超时不是错误——转后台继续等，
      // 完成时补刷；前台提示「稍后自动生效」。
      error.value = '全量同步仍在进行（超过 60s），完成后数据将自动刷新'
      void waitSyncComplete(baseline, 600000, {
        expectResync: true, baselineError,
      }).then(async (r) => {
        if (r === 'done') { await refreshAllData(); await loadAll() }
      })
    }
    setTimeout(() => { resyncing.value = false }, 2000)
  } catch (e: any) {
    resyncing.value = false
    // 真实归因（2026-09-28 用户报告修复：原硬编码「需先绑定」误导——任意失败
    // 都指向绑定，实际成因可能是路由缺失/服务未就绪/网络/服务端 detail）
    const httpStatus = e?.response?.status
    const rawDetail = e?.response?.data?.detail
    const detailText = typeof rawDetail === 'string'
      ? rawDetail
      : (rawDetail != null ? JSON.stringify(rawDetail) : '')
    error.value = httpStatus
      ? `重新全量同步失败：服务返回 ${httpStatus}${detailText ? `（${detailText}）` : ''}`
      : '重新全量同步失败：本地服务未就绪或网络异常，请稍后重试'
  }
}

async function handleUnbind() {
  unbinding.value = true
  try {
    await unbindSyncAccount()
    await loadAll()
  } finally {
    unbinding.value = false
  }
}

async function handleRevoke(id: string) {
  revoking.value = id
  try {
    await revokeSyncDevice(id)
    await loadAll()
  } catch {
    error.value = '撤销设备失败'
  } finally {
    revoking.value = null
  }
}

function closeRepair() {
  repairOpen.value = false
  repairCode.value = ''
}

async function submitPair(code: string, fromRepair: boolean) {
  pairing.value = true
  pairError.value = ''
  pairNotice.value = ''
  try {
    // device_id 仅同云携带（评审 B-M4 / W21 r1-F5）：换地址重配对不把旧云 device_id 带去新云
    const sameCloud = !devStatus.value?.cloud_url || devStatus.value.cloud_url === effectiveUrl.value
    const res = await pairDeviceExec({
      pairing_code: code.trim(),
      cloud_url: effectiveUrl.value,
      name: fromRepair ? (devStatus.value?.name || '') : deviceName.value.trim(),
      // P0#3：换绑场景传旧 device_id；reset 后 status.device_id 已空（凭据面），
      // 由后端墓碑回落兜住（评审 r2-F2 注释勘误）。lastDeviceId 为面板侧双保险。
      device_id: sameCloud ? (devStatus.value?.device_id || lastDeviceId.value || '') : '',
      tls_skip: fromRepair ? repairTlsSkip.value : tlsSkip.value,
    })
    pairCode.value = ''
    repairCode.value = ''
    if (res?.device_id_changed) {
      pairNotice.value = '配对成功——⚠ device_id 已换代（历史会话执行目标断代；上游未支持同 id 重注册或发生冲突）'
    } else if (res?.device_id) {
      pairNotice.value = '配对成功——执行身份已刷新（device_id 保持连续）'
    } else {
      pairNotice.value = '配对成功——执行身份已刷新'
    }
    if (fromRepair) repairOpen.value = false
    await loadDevice()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    pairError.value = typeof detail === 'string' ? detail : (e?.message || '配对失败')
  } finally {
    pairing.value = false
  }
}

async function handlePair() {
  await submitPair(pairCode.value, false)
}

async function handleRepair() {
  await submitPair(repairCode.value, true)
}

async function handleRename() {
  renaming.value = true
  pairError.value = ''
  pairNotice.value = ''
  try {
    const res = await renameDeviceExec({ name: deviceName.value.trim() })
    // W25：云端同步结果如实回执（成功/降级原因 + 重配对带名兜底）
    const base = '名称已保存（仅显示名，绑定凭据未动）'
    pairNotice.value = res?.renamed_cloud
      ? `${base}· 云端已同步`
      : `${base}· 云端未同步——${res?.cloud_note || '未知原因'}`
    await loadDevice()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    pairError.value = typeof detail === 'string' ? detail : (e?.message || '名称保存失败')
  } finally {
    renaming.value = false
  }
}

async function handleReset() {
  resetting.value = true
  pairError.value = ''
  pairNotice.value = ''
  try {
    await resetDeviceExec()
    closeRepair()
    pairNotice.value = '本地凭据已清除（重新配对可恢复执行）'
    await loadDevice()
  } catch (e: any) {
    pairError.value = e?.response?.data?.detail || e?.message || '重置失败'
  } finally {
    resetting.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
/* 样式随组件（scoped 不跨组件泄漏——同名类须自带，2026-10-10 布局坑） */
.cloud-link-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  font-size: 13px;
  width: 100%;
}
.sync-heading {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #1f2328);
}
.sync-row {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  color: var(--text-secondary, #57606a);
}
.sync-row b {
  font-weight: 500;
  color: var(--text-primary, #1f2328);
}
.sync-table {
  width: 100%;
  border-collapse: collapse;
}
.sync-table th,
.sync-table td {
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-color, #e5e7eb);
  font-weight: 400;
}
.sync-table th {
  color: var(--text-secondary, #57606a);
  font-size: 12px;
}
.sync-table tr.revoked td {
  opacity: 0.5;
}
.sync-revoke-btn,
.sync-refresh-btn {
  padding: 3px 10px;
  border: 1px solid var(--border-color, #d0d7de);
  border-radius: 6px;
  background: var(--bg-secondary, #f6f8fa);
  cursor: pointer;
  font-size: 12px;
}
.sync-pager {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-secondary, #57606a);
}
.sync-pager-info {
  flex: 1;
  text-align: center;
}
.sync-pager-btn {
  padding: 4px 12px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 6px;
  background: var(--surface-panel-subtle, #fff);
  color: var(--text-primary, #1f2328);
  font-size: 12px;
  cursor: pointer;
}
.sync-pager-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.sync-pager-btn:not(:disabled):hover {
  border-color: var(--color-primary, #55703f);
}

.sync-revoke-btn:hover,
.sync-refresh-btn:hover {
  background: var(--bg-tertiary, #eaeef2);
}
.sync-revoke-btn:disabled,
.sync-refresh-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.sync-error {
  color: var(--danger-color, #cf222e);
  font-size: 12px;
  padding: 4px 0;
}
.sync-ok {
  margin: 6px 0;
  padding: 6px 8px;
  border: 1px solid #2f7d4f;
  background: #eaf7ee;
  color: #205c39;
  font-size: 12px;
  border-radius: 4px;
}
.sync-empty,
.sync-loading {
  color: var(--text-tertiary, #8c959f);
  font-size: 12px;
  padding: 8px 0;
}
.sync-actions {
  display: flex;
  justify-content: flex-end;
}
.sync-bind-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sync-bind-form input[type="text"],
.sync-bind-form input[type="password"],
.device-rename-row input[type="text"] {
  padding: 6px 10px;
  border: 1px solid var(--border-color, #d0d7de);
  border-radius: 6px;
  background: var(--color-bg, #fff);
  color: var(--color-text, #1f2328);
  font-size: 13px;
}
.sync-tls-opt {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary, #57606a);
  font-size: 12px;
}
.sync-hint {
  color: var(--text-tertiary, #8c959f);
  font-size: 12px;
  line-height: 1.6;
  padding: 2px 0;
}
.device-exec-dot {
  display: inline-block;
  width: 9px; height: 9px; border-radius: 50%;
  margin-right: 6px; vertical-align: middle;
}
.device-exec-dot.on { background: var(--success-color, #34c759); }
.device-exec-dot.off { background: #b0b0b0; }
.device-pair-form { margin-top: 8px; }
.device-binding-info { padding: 2px 0; }
.device-rename-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
}
.device-rename-label {
  color: var(--text-secondary, #57606a);
  flex-shrink: 0;
}
.device-rename-row input[type="text"] {
  flex: 1;
}
.device-repair-box {
  margin: 8px 0;
  padding: 10px 12px;
  border: 1px dashed var(--border-color, #d0d7de);
  border-radius: 6px;
}
.device-repair-actions {
  display: flex;
  gap: 8px;
}
.device-reset-block {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
</style>
