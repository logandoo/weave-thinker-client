<template>
  <div class="workspace-panel">
    <section class="ws-section">
      <h3 class="ws-section-title">Agent 工作区目录</h3>
      <p class="ws-desc">
        编码 / 文件类任务的项目根目录。默认位于应用数据目录（系统隐藏）；可指定为常用开发目录
        （如 ~/Documents/DEV）。该设置随账号同步到其他端，下次任务生效。
      </p>
      <div class="ws-row">
        <input
          v-model="rootInput"
          class="ws-input"
          spellcheck="false"
          placeholder="/绝对/路径（留空表示恢复默认）"
          aria-label="工作区目录"
          @keydown.enter="save"
        />
        <button v-if="hasBridge" class="ws-btn" :disabled="saving || loading" @click="pick">
          选择…
        </button>
      </div>
      <div class="ws-actions">
        <button class="ws-btn primary" :disabled="saving || loading" @click="save">保存</button>
        <button class="ws-btn" :disabled="saving || loading" @click="reset">恢复默认</button>
        <span v-if="msg" class="ws-msg">{{ msg }}</span>
        <span v-if="error" class="ws-error">{{ error }}</span>
      </div>
      <p class="ws-current">
        当前生效：{{ info?.workspace_root || '…' }}
        <template v-if="info">（{{ info.is_override ? '自定义' : '默认' }}）</template>
      </p>
      <p v-if="info && info.is_override" class="ws-current">默认位置：{{ info.default_root }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { userSettingsApi, type WorkspaceRootInfo } from '@/api/userSettings'

const info = ref<WorkspaceRootInfo | null>(null)
const rootInput = ref('')
const loading = ref(false)
const saving = ref(false)
const msg = ref('')
const error = ref('')

const hasBridge = computed(() => !!(window as any).NativeBridge?.fs?.pickDirectory)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await userSettingsApi.getWorkspaceRoot()
    info.value = data
    rootInput.value = data.is_override ? data.workspace_root : ''
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '读取失败'
  } finally {
    loading.value = false
  }
}

async function pick() {
  try {
    const picked = await (window as any).NativeBridge?.fs?.pickDirectory?.()
    if (typeof picked === 'string' && picked) rootInput.value = picked
  } catch {
    error.value = '目录选择失败'
  }
}

async function save() {
  saving.value = true
  msg.value = ''
  error.value = ''
  try {
    const value = rootInput.value.trim()
    const { data } = await userSettingsApi.setWorkspaceRoot(value || null)
    info.value = data
    rootInput.value = data.is_override ? data.workspace_root : ''
    msg.value = data.is_override ? '已保存（下次任务生效）' : '已恢复默认'
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '保存失败'
  } finally {
    saving.value = false
  }
}

async function reset() {
  rootInput.value = ''
  await save()
}

onMounted(load)
</script>

<style scoped>
.workspace-panel {
  padding: 20px 24px;
  font-size: 13px;
  color: var(--color-text);
}
.ws-section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 8px;
}
.ws-desc {
  color: var(--color-text-light);
  line-height: 1.6;
  margin: 0 0 12px;
}
.ws-row {
  display: flex;
  gap: 8px;
}
.ws-input {
  flex: 1;
  min-width: 0;
  padding: 7px 10px;
  border: 1px solid var(--input-container-border, #d9d9d9);
  border-radius: 8px;
  background: var(--surface-input, #fff);
  color: inherit;
  font-family: inherit;
}
.ws-input:focus {
  outline: none;
  border-color: var(--accent, #4c7a46);
}
.ws-btn {
  padding: 7px 14px;
  border: 1px solid var(--input-container-border, #d9d9d9);
  background: transparent;
  color: inherit;
  border-radius: 8px;
  cursor: pointer;
}
.ws-btn.primary {
  background: var(--accent, #4c7a46);
  color: #fff;
  border-color: transparent;
}
.ws-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.ws-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 10px;
}
.ws-current {
  color: var(--color-text-light);
  margin: 10px 0 0;
  word-break: break-all;
}
.ws-msg {
  color: var(--accent, #4c7a46);
}
.ws-error {
  color: #d9534f;
}
</style>
