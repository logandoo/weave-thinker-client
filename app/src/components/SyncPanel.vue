<template>
  <div class="sync-panel">
    <section class="sync-section" data-testid="sync-account">
      <h3 class="sync-heading">云端账号</h3>
      <template v-if="account?.bound">
        <div class="sync-row"><span>服务器</span><b>{{ account.server_url }}</b></div>
        <div class="sync-row"><span>账号</span><b>{{ account.cloud_username }}</b></div>
        <div class="sync-row"><span>最近同步</span><b>{{ fmt(account.last_sync_at ?? null) }}</b></div>
        <div v-if="account.last_error" class="sync-error">{{ account.last_error }}</div>
        <button class="sync-revoke-btn" :disabled="unbinding" @click="handleUnbind">
          {{ unbinding ? '解绑中…' : '解绑' }}
        </button>
        <button class="sync-refresh-btn" :disabled="resyncing" data-testid="sync-resync" @click="handleResync">
          {{ resyncing ? '已排入下周期…' : '重新全量同步' }}
        </button>
      </template>
      <template v-else>
        <div class="sync-bind-form">
          <input v-model="bindUrl" type="text" placeholder="同步服务器（如 https://host:8158）" data-testid="sync-bind-url" />
          <input v-model="bindUser" type="text" placeholder="云端账号" data-testid="sync-bind-user" />
          <input v-model="bindPass" type="password" placeholder="云端密码" data-testid="sync-bind-pass" />
          <label class="sync-tls-opt">
            <input v-model="bindTlsSkip" type="checkbox" data-testid="sync-bind-tls-skip" />
            自签证书（跳过 TLS 校验，仅限自己掌控的内网服务器）
          </label>
          <button class="sync-refresh-btn" :disabled="binding" data-testid="sync-bind-submit" @click="handleBind">
            {{ binding ? '绑定中…' : '绑定并启用同步' }}
          </button>
        </div>
        <div v-if="bindError" class="sync-error" data-testid="sync-bind-error">{{ bindError }}</div>
      </template>
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
    </section>

    <div class="sync-actions">
      <button class="sync-refresh-btn" :disabled="loading" @click="loadAll">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
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
import { refreshAllData, waitSyncComplete } from '../composables/useDataRefresh'

const status = ref<SyncStatus | null>(null)
const devices = ref<SyncDevice[]>([])
const conflicts = ref<SyncConflict[]>([])
const account = ref<SyncAccount | null>(null)
const loading = ref(false)
const revoking = ref<string | null>(null)
const error = ref('')
const bindUrl = ref('')
const bindUser = ref('')
const bindPass = ref('')
const bindTlsSkip = ref(false)
const binding = ref(false)
const unbinding = ref(false)
const resyncing = ref(false)
const bindError = ref('')

function fmt(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('zh-CN', { hour12: false })
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

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [st, d, c, a] = await Promise.all([
      getSyncStatus(),
      getSyncDevices(),
      getSyncConflicts(),
      getSyncAccount(),
    ])
    status.value = st
    devices.value = d
    conflicts.value = c
    account.value = a
  } catch (e: any) {
    error.value = e?.response?.status === 404 ? '同步服务未启用' : '加载同步状态失败'
  } finally {
    loading.value = false
  }
}

async function handleBind() {
  binding.value = true
  bindError.value = ''
  try {
    await bindSyncAccount({
      server_url: bindUrl.value.trim(),
      cloud_username: bindUser.value.trim(),
      cloud_password: bindPass.value,
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
      await refreshAllData()
      error.value = ''
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
      }).then((r) => {
        if (r === 'done') void refreshAllData()
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

onMounted(loadAll)
</script>

<style scoped>
.sync-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  font-size: 13px;
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
.sync-bind-form input[type="password"] {
  padding: 6px 10px;
  border: 1px solid var(--border-color, #d0d7de);
  border-radius: 6px;
  background: var(--color-bg, #fff);
  color: var(--color-text, #1f2328);
  font-size: 13px;
}
</style>
