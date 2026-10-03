<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type ResumePairBase, type TraktDirection, type TraktPlan, type TraktPlanEntry } from '../api'

const props = defineProps<{ direction: TraktDirection }>()
const visible = defineModel<boolean>({ required: true })

const loading = ref(false)
const executing = ref(false)
const dryRun = ref(false)
const preview = ref<(ResumePairBase & { plan?: TraktPlan })[] | null>(null)

const toTrakt = computed(() => props.direction === 'to_trakt')
const title = computed(() => (toTrakt.value ? '全量同步到 Trakt' : '从 Trakt 全量同步'))

interface Section {
  key: 'history' | 'watched' | 'progress' | 'clear'
  label: string
  kind: 'watched' | 'progress' | 'clear'
}
const sections = computed<Section[]>(() =>
  toTrakt.value
    ? [
        { key: 'history', label: '在 Trakt 补观看记录', kind: 'watched' },
        { key: 'progress', label: '写入 Trakt 播放进度', kind: 'progress' },
        { key: 'clear', label: '清除 Trakt 上已看完条目的进度', kind: 'clear' },
      ]
    : [
        { key: 'watched', label: '在 Plex / Emby 标记已看', kind: 'watched' },
        { key: 'progress', label: '写入 Plex / Emby 播放进度', kind: 'progress' },
      ],
)

const entries = (plan: TraktPlan, key: Section['key']): TraktPlanEntry[] => plan[key] ?? []
const total = computed(() =>
  (preview.value ?? []).reduce(
    (n, p) => n + (p.plan ? sections.value.reduce((m, s) => m + entries(p.plan!, s.key).length, 0) : 0),
    0,
  ),
)

const pairLabel = (p: ResumePairBase & { plan?: TraktPlan }) =>
  `Plex[${p.plex_user || '服务器所有者'}] ⇄ Emby[${p.emby_user}]` + (p.plan ? ` · Trakt[${p.plan.trakt_user}]` : '')
const fmtDate = (ts?: number) => (ts && ts > 0 ? new Date(ts * 1000).toLocaleDateString() : '未知')
const fmtPct = (v?: number | null) => (v == null ? '—' : `${Math.round(v)}%`)

watch(visible, (v) => {
  preview.value = null
  if (v) loadPreview()
})

async function loadPreview() {
  loading.value = true
  try {
    const res = await api.traktSyncPreview(props.direction)
    preview.value = res.pairs
    dryRun.value = res.dry_run
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function execute() {
  try {
    await ElMessageBox.confirm(
      `${title.value}：共 ${total.value} 项。执行时会按最新数据重新计算，在后台进行，结果请查看日志。` +
        (dryRun.value ? '\n当前为 Dry-run 模式，只会记录日志，不会实际修改。' : ''),
      '确认执行',
      { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  executing.value = true
  try {
    const res = await api.traktSyncExecute(props.direction)
    const failed = res.pairs.filter((p) => p.error)
    if (failed.length) ElMessage.error(failed.map((p) => p.error).join('；'))
    else {
      ElMessage.success('已开始执行，进度和结果请查看日志')
      visible.value = false
    }
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    executing.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="min(860px, 94vw)" top="6vh">
    <el-alert type="info" :closable="false" show-icon class="block">
      <template v-if="toTrakt">
        以 Plex / Emby 合并后的状态为准（最近一次对账）：已看而 Trakt 上没有的补观看记录；看到一半的写入 Trakt 进度（1%～79%）；
        已看完的清除 Trakt 上残留的进度。不会删除 Trakt 上的观看记录。
      </template>
      <template v-else>
        以 Trakt 为准：Trakt 已看而本地未看的标记为已看；Trakt 进度比本地新且相差明显的写入本地进度。
        只处理库中已有的条目，不会把本地条目改为未看。Trakt 只有百分比进度，写入的位置有一定误差。
      </template>
    </el-alert>
    <div v-loading="loading" class="body">
      <el-alert
        v-if="dryRun && preview"
        type="warning"
        :closable="false"
        show-icon
        class="block"
        title="当前为 Dry-run 模式，执行时只记录日志"
      />
      <div v-for="p in preview ?? []" :key="p.id" class="pair">
        <div class="pair-title">{{ pairLabel(p) }}</div>
        <el-alert v-if="p.error" type="error" :closable="false" :title="p.error" />
        <template v-else-if="p.plan">
          <div class="summary">
            <el-tag v-for="s in sections" :key="s.key" :type="entries(p.plan, s.key).length ? 'success' : 'info'">
              {{ s.label }} {{ entries(p.plan, s.key).length }}
            </el-tag>
            <el-tag v-if="p.plan.skipped_newer" type="info">本地进度更新，跳过 {{ p.plan.skipped_newer }}</el-tag>
          </div>
          <el-collapse>
            <template v-for="s in sections" :key="s.key">
              <el-collapse-item
                v-if="entries(p.plan, s.key).length"
                :title="`${s.label}（${entries(p.plan, s.key).length}）`"
              >
                <el-table :data="entries(p.plan, s.key)" size="small" max-height="300">
                  <el-table-column prop="title" label="条目" min-width="180" />
                  <el-table-column prop="key" label="TMDB" min-width="190" />
                  <el-table-column v-if="s.kind === 'watched'" label="观看时间" width="120">
                    <template #default="{ row }">{{ fmtDate(row.watched_at) }}</template>
                  </el-table-column>
                  <template v-if="s.kind === 'progress'">
                    <el-table-column label="本地" width="80">
                      <template #default="{ row }">{{ fmtPct(row.local_pct) }}</template>
                    </el-table-column>
                    <el-table-column label="Trakt" width="80">
                      <template #default="{ row }">{{ fmtPct(row.trakt_pct) }}</template>
                    </el-table-column>
                  </template>
                  <el-table-column v-if="s.kind === 'clear'" label="Trakt 进度" width="100">
                    <template #default="{ row }">{{ fmtPct(row.trakt_pct) }}</template>
                  </el-table-column>
                </el-table>
              </el-collapse-item>
            </template>
          </el-collapse>
        </template>
      </div>
      <el-empty v-if="preview && !preview.length" description="没有绑定 Trakt 账户的用户映射" :image-size="60" />
    </div>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button :loading="loading" @click="loadPreview">刷新预览</el-button>
      <el-button type="primary" :loading="executing" :disabled="!total" @click="execute">执行（{{ total }} 项）</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
}
.body {
  min-height: 80px;
}
.pair {
  margin-bottom: 16px;
}
.pair-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
