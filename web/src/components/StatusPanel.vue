<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, RefreshRight, Switch } from '@element-plus/icons-vue'
import { api, type LogRecord, type Status } from '../api'
import ResumeSyncDialog from './ResumeSyncDialog.vue'

defineProps<{ status: Status | null }>()
const emit = defineEmits<{ refresh: [] }>()

const logs = ref<LogRecord[]>([])
const autoScroll = ref(true)
const logBox = ref<HTMLElement>()
const busy = ref(false)
const resumeDialog = ref(false)
let lastSeq = 0
let timer: number | undefined

async function pollLogs() {
  try {
    const data = await api.logs(lastSeq)
    if (!data.logs.length) return
    lastSeq = data.seq
    logs.value = [...logs.value, ...data.logs].slice(-500)
    if (autoScroll.value) {
      await nextTick()
      logBox.value?.scrollTo({ top: logBox.value.scrollHeight })
    }
  } catch {
    /* 后端不可用时静默 */
  }
}

async function run(action: 'reconcile' | 'restart') {
  busy.value = true
  try {
    await (action === 'reconcile' ? api.reconcile() : api.restart())
    ElMessage.success(action === 'reconcile' ? '已加入对账队列' : '同步引擎已重启')
    emit('refresh')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    busy.value = false
  }
}

const fmtTime = (ts: number) => new Date(ts * 1000).toLocaleString()
const fmtClock = (ts: number) => new Date(ts * 1000).toLocaleTimeString()
const levelType = (l: string) =>
  l === 'ERROR' || l === 'CRITICAL' ? 'danger' : l === 'WARNING' ? 'warning' : 'info'

onMounted(() => {
  pollLogs()
  timer = window.setInterval(pollLogs, 2000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div v-if="status" class="panel">
    <el-alert v-if="status.error" :title="status.error" type="error" show-icon :closable="false" />
    <el-alert
      v-if="!status.configured"
      title="尚未配置，请前往“配置”页填写 Plex / Emby 连接信息和用户映射"
      type="info"
      show-icon
      :closable="false"
    />

    <el-row :gutter="12">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat">
          <div class="label">同步引擎</div>
          <el-tag :type="status.running ? 'success' : 'info'">{{ status.running ? '运行中' : '已停止' }}</el-tag>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat">
          <div class="label">Plex WebSocket</div>
          <el-tag :type="status.plex_ws ? 'success' : 'danger'">{{ status.plex_ws ? '已连接' : '未连接' }}</el-tag>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat">
          <div class="label">增量轮询</div>
          <el-tooltip v-if="status.poll_error" :content="status.poll_error">
            <el-tag type="danger">失败</el-tag>
          </el-tooltip>
          <el-tag v-else-if="status.last_poll" type="success">{{ fmtClock(status.last_poll) }}</el-tag>
          <el-tag v-else type="info">{{ status.running ? '等待首次' : '未运行' }}</el-tag>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat">
          <div class="label">队列</div>
          <el-tag v-if="status.reconciling" type="warning">对账中</el-tag>
          <el-tag v-else type="info">{{ status.queue }} 个任务</el-tag>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>用户映射</span>
          <div>
            <el-button :icon="Refresh" :loading="busy" :disabled="!status.running" @click="run('reconcile')">
              立即对账
            </el-button>
            <el-button :icon="Switch" :disabled="!status.running" @click="resumeDialog = true">
              同步继续观看
            </el-button>
            <el-button :icon="RefreshRight" :loading="busy" :disabled="!status.configured" @click="run('restart')">
              重启引擎
            </el-button>
          </div>
        </div>
      </template>
      <el-table :data="status.pairs" empty-text="引擎未运行">
        <el-table-column label="Plex 用户" min-width="120">
          <template #default="{ row }">{{ row.plex_user || '服务器所有者' }}</template>
        </el-table-column>
        <el-table-column prop="emby_user" label="Emby 用户" min-width="120" />
        <el-table-column label="最近对账" min-width="170">
          <template #default="{ row }">{{ row.reconcile ? fmtTime(row.reconcile.at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="结果" min-width="260">
          <template #default="{ row }">
            <span v-if="!row.reconcile">等待首次对账</span>
            <el-text v-else-if="row.reconcile.error" type="danger">{{ row.reconcile.error }}</el-text>
            <span v-else>
              Plex {{ row.reconcile.plex }} · Emby {{ row.reconcile.emby }} · 匹配 {{ row.reconcile.matched }} ·
              同步 {{ row.reconcile.changed }} · {{ row.reconcile.seconds }}s
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>日志</span>
          <el-checkbox v-model="autoScroll">自动滚动</el-checkbox>
        </div>
      </template>
      <div ref="logBox" class="logs">
        <div v-for="l in logs" :key="l.seq" class="log-line">
          <span class="log-time">{{ fmtTime(l.time) }}</span>
          <el-tag :type="levelType(l.level)" size="small" disable-transitions>{{ l.level }}</el-tag>
          <span class="log-name">{{ l.name }}</span>
          <span class="log-msg">{{ l.message }}</span>
        </div>
        <el-empty v-if="!logs.length" description="暂无日志" :image-size="60" />
      </div>
    </el-card>
  </div>
  <el-empty v-else description="无法连接后端" />
  <ResumeSyncDialog v-model="resumeDialog" />
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.stat {
  margin-bottom: 12px;
}
.stat .label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin-bottom: 8px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.logs {
  height: 360px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.log-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  white-space: nowrap;
}
.log-time {
  color: var(--el-text-color-secondary);
}
.log-name {
  color: var(--el-color-primary);
  min-width: 48px;
}
.log-msg {
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
