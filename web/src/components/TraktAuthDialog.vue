<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { api, type TraktDeviceCode } from '../api'

const props = defineProps<{ clientId: string; clientSecret: string }>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{ authorized: [username: string] }>()

const code = ref<TraktDeviceCode | null>(null)
const error = ref('')
const loading = ref(false)
let timer: number | undefined
let deadline = 0
// 每次打开对话框递增，关闭后旧的轮询回调直接丢弃
let session = 0

function stop() {
  window.clearTimeout(timer)
  timer = undefined
}

async function start() {
  stop()
  const current = ++session
  code.value = null
  error.value = ''
  loading.value = true
  try {
    const res = await api.traktDeviceCode(props.clientId, props.clientSecret)
    if (current !== session) return
    if (!res.ok) {
      error.value = res.error || '获取授权码失败'
      return
    }
    code.value = res
    deadline = Date.now() + (res.expires_in ?? 600) * 1000
    schedule(current, res.interval ?? 5)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function schedule(current: number, seconds: number) {
  timer = window.setTimeout(() => poll(current, seconds), seconds * 1000)
}

async function poll(current: number, interval: number) {
  if (current !== session || !code.value?.device_code) return
  if (Date.now() > deadline) {
    error.value = '授权码已过期，请重新获取'
    return
  }
  try {
    const res = await api.traktDeviceToken(props.clientId, props.clientSecret, code.value.device_code)
    if (current !== session) return
    if (res.status === 'ok' && res.username) {
      ElMessage.success(`Trakt 账户 ${res.username} 授权成功`)
      emit('authorized', res.username)
      visible.value = false
      return
    }
    if (res.status === 'pending') return schedule(current, interval)
    if (res.status === 'slow_down') return schedule(current, interval + 5)
    error.value =
      {
        denied: '已在 Trakt 上拒绝授权',
        expired: '授权码已过期，请重新获取',
        used: '授权码已被使用，请重新获取',
        invalid: '授权码无效，请重新获取',
      }[res.status as string] ?? `授权失败：${res.error ?? res.status}`
  } catch (e) {
    // 网络抖动时继续等待
    if (current === session) schedule(current, interval)
  }
}

async function copyCode() {
  try {
    await navigator.clipboard.writeText(code.value?.user_code ?? '')
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动输入')
  }
}

watch(visible, (v) => {
  if (v) start()
  else {
    session++
    stop()
  }
})
</script>

<template>
  <el-dialog v-model="visible" title="授权 Trakt 账户" width="420px" align-center>
    <div v-loading="loading" class="body">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <template v-if="code?.user_code">
        <p>
          1. 打开
          <el-link type="primary" :href="code.verification_url" target="_blank">{{ code.verification_url }}</el-link>
          并登录要同步的 Trakt 账户
        </p>
        <p>2. 输入以下授权码：</p>
        <div class="code" title="点击复制" @click="copyCode">{{ code.user_code }}</div>
        <p v-if="!error" class="waiting"><el-icon class="is-loading"><Loading /></el-icon> 等待在 Trakt 上确认…</p>
      </template>
    </div>
    <template #footer>
      <el-button v-if="error" type="primary" @click="start">重新获取授权码</el-button>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.body {
  min-height: 120px;
}
.code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 32px;
  letter-spacing: 6px;
  text-align: center;
  padding: 12px;
  margin: 8px 0 16px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  cursor: pointer;
  user-select: all;
}
.waiting {
  color: var(--el-text-color-secondary);
  display: flex;
  align-items: center;
  gap: 6px;
}
</style>
