<script setup lang="ts">
import { computed, h, nextTick, onMounted, onUnmounted, ref, watch, type VNode } from 'vue'
import { ElMessage, ElTag, ElText } from 'element-plus'
import { Download, Refresh, RefreshRight, SortDown, SortUp, Switch, Upload } from '@element-plus/icons-vue'
import { api, pairLabel, type LogRecord, type ReconcileStats, type Status, type TraktDirection, type TraktStatus } from '../api'
import ResumeSyncDialog from './ResumeSyncDialog.vue'
import TraktSyncDialog from './TraktSyncDialog.vue'

const props = defineProps<{ status: Status | null }>()
const servers = computed(() => props.status?.servers ?? ['plex', 'emby'])
const dual = computed(() => servers.value.length === 2)
const emit = defineEmits<{ refresh: [] }>()

const logs = ref<LogRecord[]>([])
const autoScroll = ref(true)
// 倒序时最新日志在最上方；记住选择，刷新页面后保持
const newestFirst = ref(localStorage.getItem('logOrder') === 'desc')
const shownLogs = computed(() => (newestFirst.value ? [...logs.value].reverse() : logs.value))
const logBox = ref<HTMLElement>()
const busy = ref(false)
const resumeDialog = ref(false)
const traktDialog = ref(false)
const traktDirection = ref<TraktDirection>('to_trakt')

function openTrakt(direction: TraktDirection) {
  traktDirection.value = direction
  traktDialog.value = true
}
let lastSeq = 0
let timer: number | undefined

async function pollLogs() {
  try {
    const data = await api.logs(lastSeq)
    if (!data.logs.length) return
    lastSeq = data.seq
    logs.value = [...logs.value, ...data.logs].slice(-500)
    if (autoScroll.value) await scrollToNewest()
  } catch {
    /* 后端不可用时静默 */
  }
}

async function scrollToNewest() {
  await nextTick()
  logBox.value?.scrollTo({ top: newestFirst.value ? 0 : logBox.value.scrollHeight })
}

watch(newestFirst, (desc) => {
  localStorage.setItem('logOrder', desc ? 'desc' : 'asc')
  scrollToNewest()
})

async function releaseHeld(user: string, action: 'confirm' | 'discard') {
  try {
    const { count } = await api.traktHeld(user, action)
    ElMessage.success(action === 'confirm' ? `已确认，将删除 ${count} 个条目的观看记录` : `已丢弃 ${count} 个条目`)
    emit('refresh')
  } catch (e) {
    ElMessage.error((e as Error).message)
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

// 表格（宽屏）和卡片列表（窄屏）共用的单元格内容
const NOWRAP = { whiteSpace: 'nowrap' }
const ReconcileResult = (props: { stats?: ReconcileStats | null }) => {
  const r = props.stats
  if (!r) return h('span', '等待首次对账')
  if (r.error) return h(ElText, { type: 'danger' }, () => r.error)
  // 每一项整体换行，分隔点留在行尾
  // 只有一个服务器时没有匹配和同步
  const items = [
    r.plex != null && `Plex ${r.plex}`,
    r.emby != null && `Emby ${r.emby}`,
    r.matched != null && `匹配 ${r.matched}`,
    r.changed != null && `同步 ${r.changed}`,
    `${r.seconds}s`,
  ].filter((t): t is string => !!t)
  const parts: (VNode | string)[] = items.flatMap((t, i) =>
    i < items.length - 1 || r.failed ? [h('span', { style: NOWRAP }, `${t} ·`), ' '] : [h('span', { style: NOWRAP }, t)],
  )
  if (r.failed) parts.push(h(ElText, { type: 'danger', style: NOWRAP }, () => `失败 ${r.failed}（详见日志）`))
  return h('span', parts)
}
ReconcileResult.props = ['stats']

const TraktState = (props: { account: TraktStatus }) => {
  const a = props.account
  if (a.error) return h(ElText, { type: 'danger' }, () => a.error)
  return h('span', [
    a.busy ? h(ElTag, { type: 'warning', size: 'small' }, () => '全量同步中') : null,
    ` 本次运行实时推送 ${a.progress_pushed} 次 · 待推送 ${a.pending}`,
  ])
}
TraktState.props = ['account']

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
          <template v-if="servers.includes('plex')">
            <div class="label">Plex WebSocket</div>
            <el-tag :type="status.plex_ws ? 'success' : 'danger'">{{ status.plex_ws ? '已连接' : '未连接' }}</el-tag>
          </template>
          <template v-else>
            <div class="label">服务器</div>
            <el-tag type="info">仅 Emby</el-tag>
          </template>
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
          <div class="actions">
            <el-button :icon="Refresh" :loading="busy" :disabled="!status.running" @click="run('reconcile')">
              立即对账
            </el-button>
            <el-button v-if="dual" :icon="Switch" :disabled="!status.running" @click="resumeDialog = true">
              同步继续观看
            </el-button>
            <el-button :icon="RefreshRight" :loading="busy" :disabled="!status.configured" @click="run('restart')">
              重启引擎
            </el-button>
          </div>
        </div>
      </template>
      <el-table :data="status.pairs" empty-text="引擎未运行" class="wide-only">
        <el-table-column v-if="servers.includes('plex')" label="Plex 用户" min-width="120">
          <template #default="{ row }">{{ row.plex_user || '服务器所有者' }}</template>
        </el-table-column>
        <el-table-column v-if="servers.includes('emby')" prop="emby_user" label="Emby 用户" min-width="120" />
        <el-table-column v-if="status.trakt.length" label="Trakt" min-width="100">
          <template #default="{ row }">{{ row.trakt_user || '—' }}</template>
        </el-table-column>
        <el-table-column label="最近对账" min-width="170">
          <template #default="{ row }">{{ row.reconcile ? fmtTime(row.reconcile.at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="结果" min-width="260">
          <template #default="{ row }">
            <ReconcileResult :stats="row.reconcile" />
          </template>
        </el-table-column>
      </el-table>
      <div class="narrow-only">
        <el-empty v-if="!status.pairs.length" description="引擎未运行" :image-size="60" />
        <div v-for="row in status.pairs" :key="row.id" class="item">
          <div class="item-title">
            {{ pairLabel(row) }}
            <template v-if="status.trakt.length">→ Trakt {{ row.trakt_user || '—' }}</template>
          </div>
          <div class="item-row">
            <span class="item-label">最近对账</span>
            <span>{{ row.reconcile ? fmtTime(row.reconcile.at) : '—' }}</span>
          </div>
          <div class="item-row">
            <span class="item-label">结果</span>
            <ReconcileResult :stats="row.reconcile" />
          </div>
        </div>
      </div>
    </el-card>

    <el-card v-if="status.trakt.length" shadow="never">
      <template #header>
        <div class="card-header">
          <span>Trakt</span>
          <div class="actions">
            <el-button :icon="Upload" :disabled="!status.running" @click="openTrakt('to_trakt')">
              全量同步到 Trakt
            </el-button>
            <el-button :icon="Download" :disabled="!status.running" @click="openTrakt('from_trakt')">
              从 Trakt 全量同步
            </el-button>
          </div>
        </div>
      </template>
      <div v-if="status.trakt_next.to_trakt || status.trakt_next.from_trakt" class="trakt-next">
        <el-text v-if="status.trakt_next.to_trakt" type="info" size="small">
          下次定时同步到 Trakt：{{ fmtTime(status.trakt_next.to_trakt) }}
        </el-text>
        <el-text v-if="status.trakt_next.from_trakt" type="info" size="small">
          下次定时从 Trakt 同步：{{ fmtTime(status.trakt_next.from_trakt) }}
        </el-text>
      </div>
      <el-alert
        v-for="acc in status.trakt.filter((a) => a.held.length)"
        :key="acc.user"
        type="warning"
        show-icon
        :closable="false"
        class="trakt-held"
        :title="`Trakt[${acc.user}]：短时间内被标记未看的条目过多，疑似媒体库异常，已暂停删除 ${acc.held.length} 个条目的观看记录`"
      >
        <div class="held-titles">{{ acc.held.slice(0, 20).join('、') }}{{ acc.held.length > 20 ? ` 等 ${acc.held.length} 项` : '' }}</div>
        <el-button size="small" type="danger" @click="releaseHeld(acc.user, 'confirm')">确认删除</el-button>
        <el-button size="small" @click="releaseHeld(acc.user, 'discard')">丢弃，不删除</el-button>
      </el-alert>
      <el-table :data="status.trakt" class="wide-only">
        <el-table-column prop="user" label="账户" min-width="120" />
        <el-table-column label="最近推送" min-width="170">
          <template #default="{ row }">{{ row.last_push ? fmtTime(row.last_push) : '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" min-width="280">
          <template #default="{ row }">
            <TraktState :account="row" />
          </template>
        </el-table-column>
      </el-table>
      <div class="narrow-only">
        <div v-for="row in status.trakt" :key="row.user" class="item">
          <div class="item-title">{{ row.user }}</div>
          <div class="item-row">
            <span class="item-label">最近推送</span>
            <span>{{ row.last_push ? fmtTime(row.last_push) : '—' }}</span>
          </div>
          <div class="item-row">
            <span class="item-label">状态</span>
            <TraktState :account="row" />
          </div>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>日志</span>
          <div class="log-tools">
            <el-button
              size="small"
              :icon="newestFirst ? SortDown : SortUp"
              @click="newestFirst = !newestFirst"
            >
              {{ newestFirst ? '倒序（最新在上）' : '正序（最新在下）' }}
            </el-button>
            <el-checkbox v-model="autoScroll">自动滚动</el-checkbox>
          </div>
        </div>
      </template>
      <div ref="logBox" class="logs">
        <div v-for="l in shownLogs" :key="l.seq" class="log-line">
          <span class="log-time">{{ fmtTime(l.time) }}</span>
          <el-tag :type="levelType(l.level)" size="small" class="log-level" disable-transitions>{{ l.level }}</el-tag>
          <span class="log-name">{{ l.name }}</span>
          <span class="log-msg">{{ l.message }}</span>
        </div>
        <el-empty v-if="!logs.length" description="暂无日志" :image-size="60" />
      </div>
    </el-card>
  </div>
  <el-empty v-else description="无法连接后端" />
  <ResumeSyncDialog v-model="resumeDialog" />
  <TraktSyncDialog v-model="traktDialog" :direction="traktDirection" />
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
  line-height: 20px;
  margin-bottom: 8px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.actions .el-button {
  margin-left: 0;
}
.log-tools {
  display: flex;
  align-items: center;
  gap: 12px;
}
.logs {
  height: 360px;
  overflow: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}
.log-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 0;
  white-space: nowrap;
}
.log-time {
  color: var(--el-text-color-secondary);
}
.log-level {
  flex: none;
  width: 64px;
}
.log-name {
  color: var(--el-color-primary);
  min-width: 48px;
}
.log-msg {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
.narrow-only {
  display: none;
}
.item {
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 14px;
}
.item:first-child {
  padding-top: 0;
}
.item:last-child {
  border-bottom: none;
  padding-bottom: 0;
}
.item-title {
  font-weight: 600;
  margin-bottom: 6px;
  word-break: break-all;
}
.item-row {
  display: flex;
  gap: 12px;
  line-height: 1.6;
}
.item-label {
  flex: none;
  width: 4em;
  color: var(--el-text-color-secondary);
}
.item-row > :last-child {
  min-width: 0;
  overflow-wrap: anywhere;
}
@media (max-width: 767px) {
  .actions {
    flex-basis: 100%;
  }
  .actions .el-button {
    flex: 1 1 auto;
    padding-left: 10px;
    padding-right: 10px;
  }
  .logs {
    height: 60vh;
  }
  .wide-only {
    display: none;
  }
  .narrow-only {
    display: block;
  }
  .log-line {
    flex-wrap: wrap;
    gap: 2px 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }
  .log-msg {
    flex-basis: 100%;
  }
}
.trakt-held {
  margin-bottom: 8px;
}
.held-titles {
  margin: 4px 0 8px;
  word-break: break-all;
}
.trakt-next {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 24px;
  margin-bottom: 8px;
}
</style>
