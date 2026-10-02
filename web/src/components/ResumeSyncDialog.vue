<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type ResumeDirection, type ResumePairBase, type ResumePlan, type ResumeResult } from '../api'

const visible = defineModel<boolean>({ required: true })

const direction = ref<ResumeDirection>('plex_to_emby')
const loading = ref(false)
const executing = ref(false)
const dryRun = ref(false)
const preview = ref<(ResumePairBase & { plan?: ResumePlan })[] | null>(null)
const results = ref<(ResumePairBase & { result?: ResumeResult })[] | null>(null)

const names = { plex: 'Plex', emby: 'Emby' } as const
const srcName = computed(() => (direction.value === 'plex_to_emby' ? 'Plex' : 'Emby'))
const dstName = computed(() => (direction.value === 'plex_to_emby' ? 'Emby' : 'Plex'))
const totals = computed(() => {
  const plans = (preview.value ?? []).map((p) => p.plan).filter((p): p is ResumePlan => !!p)
  return {
    remove: plans.reduce((n, p) => n + p.remove.length, 0),
    add: plans.reduce((n, p) => n + p.add.length, 0),
  }
})

// 切换方向或重新打开时清空旧的预览
watch([direction, visible], () => {
  preview.value = null
  results.value = null
})

const pairLabel = (p: ResumePairBase) => `Plex[${p.plex_user || '服务器所有者'}] ⇄ Emby[${p.emby_user}]`
const fmtPos = (ms?: number) => {
  if (!ms) return ''
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}
const actionText = (a?: string, ms?: number) => (a === 'progress' ? `写入进度 ${fmtPos(ms)}` : '取消隐藏')

async function loadPreview() {
  loading.value = true
  results.value = null
  try {
    const res = await api.resumePreview(direction.value)
    preview.value = res.pairs
    dryRun.value = res.dry_run
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function execute() {
  const { remove, add } = totals.value
  try {
    await ElMessageBox.confirm(
      `将以 ${srcName.value} 为准同步 ${dstName.value} 的继续观看列表：从 ${dstName.value} 移除 ${remove} 部，补充 ${add} 部。` +
        (dryRun.value ? '\n当前为 Dry-run 模式，只会记录日志，不会实际修改。' : ''),
      '确认执行',
      { type: remove > 0 ? 'warning' : 'info', confirmButtonText: '执行', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  executing.value = true
  try {
    results.value = (await api.resumeExecute(direction.value)).pairs
    preview.value = null
    const errors = results.value.reduce((n, p) => n + (p.result?.errors.length ?? 0) + (p.error ? 1 : 0), 0)
    if (errors) ElMessage.warning(`执行完成，有 ${errors} 项失败`)
    else ElMessage.success('执行完成')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    executing.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="同步继续观看列表" width="min(860px, 94vw)" top="6vh">
    <el-alert type="info" :closable="false" show-icon class="block">
      一次性任务：以源服务器为准，按整部剧/电影对齐目标服务器的“继续观看”。目标有而源没有的会被移除；
      源有而目标没有的，看到一半的会写入进度，“下一集”推荐在 Emby 上会取消隐藏（Plex 无法手动加入）。
      之后的“从继续观看移除”仍会自动双向同步。
    </el-alert>

    <div class="toolbar">
      <el-radio-group v-model="direction" :disabled="loading || executing">
        <el-radio-button value="plex_to_emby">Plex → Emby</el-radio-button>
        <el-radio-button value="emby_to_plex">Emby → Plex</el-radio-button>
      </el-radio-group>
      <el-button type="primary" plain :loading="loading" :disabled="executing" @click="loadPreview">预览</el-button>
    </div>

    <template v-if="preview">
      <el-alert v-if="dryRun" type="warning" :closable="false" show-icon class="block" title="当前为 Dry-run 模式，执行时只记录日志" />
      <div v-for="p in preview" :key="p.id" class="pair">
        <div class="pair-title">{{ pairLabel(p) }}</div>
        <el-alert v-if="p.error" type="error" :closable="false" :title="p.error" />
        <template v-else-if="p.plan">
          <div class="summary">
            <el-tag type="info">已一致 {{ p.plan.same }}</el-tag>
            <el-tag type="danger">从 {{ names[p.plan.dst] }} 移除 {{ p.plan.remove.length }}</el-tag>
            <el-tag type="success">补充到 {{ names[p.plan.dst] }} {{ p.plan.add.length }}</el-tag>
            <el-tag v-if="p.plan.unsupported.length" type="warning">无法补充 {{ p.plan.unsupported.length }}</el-tag>
          </div>
          <el-collapse>
            <el-collapse-item v-if="p.plan.remove.length" :title="`从 ${names[p.plan.dst]} 移除（${p.plan.remove.length}）`">
              <el-table :data="p.plan.remove" size="small" max-height="260">
                <el-table-column prop="title" label="剧集 / 电影" />
              </el-table>
            </el-collapse-item>
            <el-collapse-item v-if="p.plan.add.length" :title="`补充到 ${names[p.plan.dst]}（${p.plan.add.length}）`">
              <el-table :data="p.plan.add" size="small" max-height="260">
                <el-table-column prop="title" label="剧集 / 电影" min-width="160" />
                <el-table-column prop="episode" label="条目" min-width="160" />
                <el-table-column label="操作" width="160">
                  <template #default="{ row }">{{ actionText(row.action, row.position_ms) }}</template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
            <el-collapse-item v-if="p.plan.unsupported.length" :title="`无法补充（${p.plan.unsupported.length}）`">
              <el-table :data="p.plan.unsupported" size="small" max-height="260">
                <el-table-column prop="title" label="剧集 / 电影" min-width="160" />
                <el-table-column prop="reason" label="原因" min-width="200" />
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </template>
      </div>
    </template>

    <template v-if="results">
      <div v-for="p in results" :key="p.id" class="pair">
        <div class="pair-title">{{ pairLabel(p) }}</div>
        <el-alert v-if="p.error" type="error" :closable="false" :title="p.error" />
        <template v-else-if="p.result">
          <el-alert v-if="p.result.dry_run" type="warning" :closable="false" title="Dry-run：未实际修改，详见日志" />
          <el-descriptions v-else :column="2" border size="small">
            <el-descriptions-item :label="`已从 ${names[p.result.dst]} 移除`">{{ p.result.removed }} 部</el-descriptions-item>
            <el-descriptions-item :label="`已补充到 ${names[p.result.dst]}`">{{ p.result.added }} 部</el-descriptions-item>
            <el-descriptions-item :label="`${names[p.result.dst]} 继续观看现有`">{{ p.result.dst_count }} 项</el-descriptions-item>
            <el-descriptions-item label="失败">{{ p.result.errors.length }} 项</el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-for="(err, i) in p.result.errors"
            :key="i"
            type="error"
            :closable="false"
            :title="err"
            class="block"
          />
        </template>
      </div>
    </template>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button
        type="primary"
        :loading="executing"
        :disabled="!preview || (totals.remove === 0 && totals.add === 0)"
        @click="execute"
      >
        执行同步
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.block {
  margin-bottom: 12px;
}
.toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 16px;
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
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
</style>
