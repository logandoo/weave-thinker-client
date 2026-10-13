<template>
  <div class="sync-panel">
    <section class="sync-section" data-testid="device-exec-status">
      <h3 class="sync-heading">设备执行状态</h3>
      <div v-if="error" class="sync-error" data-testid="device-exec-error">{{ error }}</div>
      <template v-else-if="status">
        <div class="sync-row">
          <span>在线</span>
          <b>
            <span class="device-exec-dot" :class="status.connected ? 'on' : 'off'" data-testid="device-exec-online-dot"></span>
            {{ status.connected ? '已连接' : status.auth_failed ? '凭据失效（请重置后重新配对）' : '离线' }}
          </b>
        </div>
        <div class="sync-row"><span>配对</span><b>{{ status.paired ? (status.name || status.device_id || '已配对') : '未配对' }}</b></div>
        <div class="sync-row"><span>云端地址</span><b>{{ status.cloud_url || '（复用同步绑定）' }}</b></div>
        <div class="sync-row"><span>最后心跳</span><b>{{ fmt(status.last_heartbeat_at) }}</b></div>
        <div v-if="status.last_error" class="sync-error" data-testid="device-exec-last-error">最近错误：{{ status.last_error }}</div>
        <div v-if="status.paired && status.name_gate_ready === false" class="sync-error" data-testid="device-exec-identity-unbound">
          设备执行身份未就绪——请重新配对，或在「同步」页重新提交云端绑定（回填身份）
        </div>
        <div class="sync-row"><span>云端身份</span><b data-testid="device-exec-cloud-identity">{{ status.cloud_username || '（未绑定）' }}<template v-if="!status.paired">（墓碑留存）</template><template v-if="status.cloud_user_id"> · {{ status.cloud_user_id.slice(0, 8) }}</template></b></div>
        <div class="sync-row"><span>名门期望值</span><b data-testid="device-exec-expected-ctx">{{ status.workspace_ctx_expected || '（不可算）' }}</b></div>
        <div class="sync-row"><span>构建</span><b data-testid="device-exec-build">{{ status.impl_marker }} · {{ status.build_id }}</b></div>
      </template>
      <div v-else class="sync-loading">加载中…</div>
    </section>

    <section class="sync-section" data-testid="device-exec-pair">
      <h3 class="sync-heading">配对</h3>
      <div class="sync-bind-form">
        <input v-model="pairCode" type="text" placeholder="一次性配对码（云端「添加设备」生成）" data-testid="device-exec-code" />
        <input v-model="cloudUrl" type="text" placeholder="云端地址（如 https://host:8158；缺省=同步绑定）" data-testid="device-exec-cloud-url" />
        <input v-model="deviceName" type="text" placeholder="设备名称（可选）" data-testid="device-exec-name" />
        <label class="sync-tls-opt">
          <input v-model="tlsSkip" type="checkbox" data-testid="device-exec-tls-skip" />
          自签证书（跳过 TLS 校验，仅限自己掌控的内网服务器）
        </label>
        <button class="sync-refresh-btn" :disabled="pairing || !pairCode.trim()" data-testid="device-exec-pair-submit" @click="handlePair">
          {{ pairing ? '配对中…' : '配对并启用' }}
        </button>
        <div v-if="pairNotice" class="sync-ok" data-testid="device-exec-pair-notice">{{ pairNotice }}</div>
        <button class="sync-revoke-btn" :disabled="resetting || !status?.paired" data-testid="device-exec-reset" @click="handleReset">
          {{ resetting ? '重置中…' : '重置本地凭据' }}
        </button>
      </div>
      <div v-if="pairError" class="sync-error" data-testid="device-exec-pair-error">{{ pairError }}</div>
    </section>

    <div class="sync-actions">
      <button class="sync-refresh-btn" :disabled="loading" @click="load">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getDeviceExecStatus, pairDeviceExec, resetDeviceExec, type DeviceExecStatus } from '../api/clientExec'

const status = ref<DeviceExecStatus | null>(null)
const pairNotice = ref('')
const lastDeviceId = ref('')
const loading = ref(false)
const pairing = ref(false)
const resetting = ref(false)
const error = ref('')
const pairError = ref('')
const pairCode = ref('')
const cloudUrl = ref('')
const deviceName = ref('')
const tlsSkip = ref(false)

function fmt(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    status.value = await getDeviceExecStatus()
    if (status.value?.device_id) lastDeviceId.value = status.value.device_id
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '状态读取失败'
  } finally {
    loading.value = false
  }
}

async function handlePair() {
  pairing.value = true
  pairError.value = ''
  pairNotice.value = ''
  try {
    const res = await pairDeviceExec({
      pairing_code: pairCode.value.trim(),
      cloud_url: cloudUrl.value.trim(),
      name: deviceName.value.trim(),
      // P0#3：换绑场景传旧 device_id；reset 后 status.device_id 已空（凭据面），
      // 由后端墓碑回落兜住（评审 r2-F2 注释勘误）。lastDeviceId 为面板侧双保险。
      device_id: status.value?.device_id || lastDeviceId.value || '',
      tls_skip: tlsSkip.value,
    })
    pairCode.value = ''
    if (res?.device_id_changed) {
      pairNotice.value = '配对成功——⚠ device_id 已换代（历史会话执行目标断代；上游未支持同 id 重注册或发生冲突）'
    } else if (res?.device_id) {
      pairNotice.value = '配对成功——执行身份已刷新（device_id 保持连续）'
    } else {
      pairNotice.value = '配对成功——执行身份已刷新'
    }
    await load()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    pairError.value = typeof detail === 'string' ? detail : (e?.message || '配对失败')
  } finally {
    pairing.value = false
  }
}

async function handleReset() {
  resetting.value = true
  pairError.value = ''
  try {
    await resetDeviceExec()
    await load()
  } catch (e: any) {
    pairError.value = e?.response?.data?.detail || e?.message || '重置失败'
  } finally {
    resetting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* 样式随组件（scoped 不跨组件泄漏——SyncPanel 同名类须自带，2026-10-10 布局坑） */
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
.sync-ok {
  margin: 6px 0;
  padding: 6px 8px;
  border: 1px solid #2f7d4f;
  background: #eaf7ee;
  color: #205c39;
  font-size: 12px;
  border-radius: 4px;
}

.sync-error {
  color: var(--danger-color, #cf222e);
  font-size: 12px;
  padding: 4px 0;
}
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
.sync-tls-opt {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary, #57606a);
  font-size: 12px;
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
.device-exec-dot {
  display: inline-block;
  width: 9px; height: 9px; border-radius: 50%;
  margin-right: 6px; vertical-align: middle;
}
.device-exec-dot.on { background: var(--success-color, #34c759); }
.device-exec-dot.off { background: #b0b0b0; }
</style>
