<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, type Status } from './api'
import StatusPanel from './components/StatusPanel.vue'
import ConfigPanel from './components/ConfigPanel.vue'

const tab = ref('status')
const status = ref<Status | null>(null)
let timer: number | undefined

async function refreshStatus() {
  try {
    status.value = await api.status()
  } catch {
    status.value = null
  }
}

function onSaved(s: Status) {
  status.value = s
  tab.value = 'status'
}

onMounted(async () => {
  await refreshStatus()
  if (status.value && !status.value.configured) tab.value = 'config'
  timer = window.setInterval(refreshStatus, 3000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<template>
  <div class="layout">
    <header class="header">
      <div class="brand">
        <span class="title">MediaResume</span>
        <span class="subtitle">Plex ⇄ Emby 播放记录同步</span>
      </div>
      <div v-if="status" class="tags">
        <el-tag v-if="!status.configured" type="info">未配置</el-tag>
        <el-tag v-else-if="status.error" type="danger">异常</el-tag>
        <el-tag v-else-if="status.running" type="success">运行中</el-tag>
        <el-tag v-if="status.dry_run && status.running" type="warning">Dry-run</el-tag>
      </div>
      <el-tag v-else type="danger">无法连接后端</el-tag>
    </header>
    <main class="main">
      <el-tabs v-model="tab">
        <el-tab-pane label="运行状态" name="status">
          <StatusPanel :status="status" @refresh="refreshStatus" />
        </el-tab-pane>
        <el-tab-pane label="配置" name="config" lazy>
          <ConfigPanel @saved="onSaved" />
        </el-tab-pane>
      </el-tabs>
    </main>
  </div>
</template>

<style scoped>
.layout {
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 16px 32px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 20px 0 8px;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.title {
  font-size: 22px;
  font-weight: 600;
}
.subtitle {
  color: var(--el-text-color-secondary);
  font-size: 14px;
}
.tags {
  display: flex;
  gap: 8px;
}
</style>
