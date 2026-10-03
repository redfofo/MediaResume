<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Delete, Plus } from '@element-plus/icons-vue'
import { api, defaultConfig, type Config, type EmbyTest, type PlexTest, type Status } from '../api'
import TraktAuthDialog from './TraktAuthDialog.vue'

const emit = defineEmits<{ saved: [status: Status] }>()

const loading = ref(true)
const saving = ref(false)
const cfg = reactive<Config>(defaultConfig())
const plexTest = ref<PlexTest | null>(null)
const embyTest = ref<EmbyTest | null>(null)
const testing = reactive({ plex: false, emby: false })

// 非所有者的 Plex 账户（所有者对应 plex_user 为空）
const plexAccounts = computed(() => (plexTest.value?.accounts ?? []).filter((a) => !a.owner))
const plexOwner = computed(() => plexTest.value?.accounts?.find((a) => a.owner)?.name)
const embyUsers = computed(() => embyTest.value?.users ?? [])

const traktAccounts = ref<string[]>([])
const traktDialog = ref(false)
const traktReady = computed(() => !!cfg.trakt.client_id.trim())

async function loadTraktAccounts() {
  try {
    traktAccounts.value = (await api.traktAccounts()).accounts
  } catch {
    /* 忽略，下拉列表为空即可 */
  }
}

function onTraktAuthorized(username: string) {
  if (!traktAccounts.value.includes(username)) traktAccounts.value = [...traktAccounts.value, username].sort()
  // 只有一个映射且尚未绑定时直接选上，省一步操作
  if (cfg.mappings.length === 1 && !cfg.mappings[0].trakt_user) cfg.mappings[0].trakt_user = username
}

async function removeTraktAccount(username: string) {
  try {
    await ElMessageBox.confirm(`移除后需要重新授权才能再次推送到 Trakt 账户 ${username}。`, '移除 Trakt 授权', {
      type: 'warning',
      confirmButtonText: '移除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await api.traktDeleteAccount(username)
    traktAccounts.value = traktAccounts.value.filter((u) => u !== username)
    for (const m of cfg.mappings) if (m.trakt_user === username) m.trakt_user = null
    ElMessage.success(`已移除 Trakt 账户 ${username} 的授权，相关映射已解绑，请保存配置`)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
}

async function testPlex(silent = false) {
  testing.plex = true
  try {
    plexTest.value = await api.testPlex(cfg.plex.url, cfg.plex.token)
    if (!silent) {
      if (plexTest.value.ok) ElMessage.success(`Plex 连接成功：${plexTest.value.name}`)
      else ElMessage.error(`Plex 连接失败：${plexTest.value.error}`)
    }
  } catch (e) {
    plexTest.value = { ok: false, error: (e as Error).message }
  } finally {
    testing.plex = false
  }
}

async function testEmby(silent = false) {
  testing.emby = true
  try {
    embyTest.value = await api.testEmby(cfg.emby.url, cfg.emby.api_key)
    if (!silent) {
      if (embyTest.value.ok) ElMessage.success(`Emby 连接成功：${embyTest.value.name}`)
      else ElMessage.error(`Emby 连接失败：${embyTest.value.error}`)
    }
  } catch (e) {
    embyTest.value = { ok: false, error: (e as Error).message }
  } finally {
    testing.emby = false
  }
}

function addMapping() {
  cfg.mappings.push({ plex_user: null, emby_user: '', plex_token: null, trakt_user: null })
}

async function save() {
  saving.value = true
  try {
    const payload: Config = {
      ...cfg,
      mappings: cfg.mappings.map((m) => ({
        plex_user: m.plex_user || null,
        emby_user: m.emby_user,
        // 所有者使用全局 token
        plex_token: m.plex_user ? m.plex_token || null : null,
        trakt_user: m.trakt_user || null,
      })),
    }
    const res = await api.saveConfig(payload)
    ElMessage.success('配置已保存，同步引擎已重启')
    emit('saved', res.status)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try {
    const [res] = await Promise.all([api.getConfig(), loadTraktAccounts()])
    if (res.config) Object.assign(cfg, { ...res.config, trakt: { ...defaultConfig().trakt, ...res.config.trakt } })
    if (res.error) ElMessage.warning(`现有配置文件无效：${res.error}`)
    if (res.config) await Promise.all([testPlex(true), testEmby(true)])
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <el-form v-loading="loading" :model="cfg" label-position="top" class="panel">
    <el-row :gutter="12">
      <el-col :xs="24" :md="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>Plex</span>
              <el-tag v-if="plexTest?.ok" type="success">{{ plexTest.name }} · {{ plexTest.version }}</el-tag>
              <el-tag v-else-if="plexTest" type="danger">{{ plexTest.error }}</el-tag>
            </div>
          </template>
          <el-form-item label="服务器地址">
            <el-input v-model="cfg.plex.url" placeholder="http://192.168.1.10:32400" />
          </el-form-item>
          <el-form-item label="X-Plex-Token（服务器所有者）">
            <el-input v-model="cfg.plex.token" type="password" show-password placeholder="Plex Token" />
          </el-form-item>
          <el-button :icon="Connection" :loading="testing.plex" @click="testPlex()">测试连接</el-button>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>Emby</span>
              <el-tag v-if="embyTest?.ok" type="success">{{ embyTest.name }} · {{ embyTest.version }}</el-tag>
              <el-tag v-else-if="embyTest" type="danger">{{ embyTest.error }}</el-tag>
            </div>
          </template>
          <el-form-item label="服务器地址">
            <el-input v-model="cfg.emby.url" placeholder="http://192.168.1.11:8096" />
          </el-form-item>
          <el-form-item label="API 密钥">
            <el-input v-model="cfg.emby.api_key" type="password" show-password placeholder="Emby 后台 → 高级 → API 密钥" />
          </el-form-item>
          <el-button :icon="Connection" :loading="testing.emby" @click="testEmby()">测试连接</el-button>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>用户映射</span>
          <el-button :icon="Plus" @click="addMapping">添加映射</el-button>
        </div>
      </template>
      <el-alert
        v-if="!plexTest?.ok || !embyTest?.ok"
        title="测试两边连接成功后，可直接从下拉列表选择用户"
        type="info"
        :closable="false"
        class="hint"
      />
      <el-table :data="cfg.mappings">
        <el-table-column label="Plex 用户" min-width="200">
          <template #default="{ row }">
            <el-select
              v-model="row.plex_user"
              clearable
              filterable
              allow-create
              :placeholder="plexOwner ? `服务器所有者（${plexOwner}）` : '服务器所有者'"
            >
              <el-option v-for="a in plexAccounts" :key="a.id" :label="a.name" :value="a.name" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="Plex 用户 Token" min-width="200">
          <template #default="{ row }">
            <el-input
              v-if="row.plex_user"
              v-model="row.plex_token"
              type="password"
              show-password
              placeholder="该用户自己的 token"
            />
            <el-text v-else type="info">使用所有者 Token</el-text>
          </template>
        </el-table-column>
        <el-table-column label="Emby 用户" min-width="200">
          <template #default="{ row }">
            <el-select v-model="row.emby_user" filterable allow-create placeholder="选择 Emby 用户">
              <el-option v-for="u in embyUsers" :key="u.id" :label="u.name" :value="u.id" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="Trakt 账户" min-width="160">
          <template #default="{ row }">
            <el-select v-model="row.trakt_user" clearable placeholder="不同步到 Trakt">
              <el-option v-for="u in traktAccounts" :key="u" :label="u" :value="u" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column width="70" align="center">
          <template #default="{ $index }">
            <el-button
              :icon="Delete"
              type="danger"
              text
              :disabled="cfg.mappings.length <= 1"
              @click="cfg.mappings.splice($index, 1)"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>Trakt（可选）</span>
          <el-tag type="info">自动推送实时进度，全量同步手动或定时执行</el-tag>
        </div>
      </template>
      <el-alert type="info" :closable="false" class="hint">
        <p>
          在
          <el-link type="primary" href="https://trakt.tv/oauth/applications/new" target="_blank">Trakt 新建 App</el-link>
          ，Redirect URI 填写 <code>urn:ietf:wg:oauth:2.0:oob</code>，然后把 Client ID 填到下面。
          新建的 App 不再提供 Client Secret（页面显示 Not issued），留空即可；较早创建、有 Secret 的 App 也可以填上。
        </p>
        <p>授权账户后，在上方“用户映射”中为每个映射选择要同步的 Trakt 账户。</p>
      </el-alert>
      <el-row :gutter="12">
        <el-col :xs="24" :md="12">
          <el-form-item label="Client ID">
            <el-input v-model="cfg.trakt.client_id" placeholder="Trakt App 的 Client ID" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-form-item label="Client Secret（可选）">
            <el-input v-model="cfg.trakt.client_secret" type="password" show-password placeholder="新建的 App 没有 Secret，留空即可" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="实时推送播放进度">
        <el-switch v-model="cfg.trakt.scrobble" />
        <el-text type="info" class="switch-hint">
          播放时 Trakt 显示“正在观看”，暂停 / 停止时保存进度，看完时记录一次观看
        </el-text>
      </el-form-item>
      <el-row :gutter="24">
        <el-col :xs="24" :sm="12">
          <el-form-item label="定时全量同步到 Trakt（小时）">
            <el-input-number v-model="cfg.trakt.push_interval" :min="0" :step="1" step-strictly />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12">
          <el-form-item label="定时从 Trakt 全量同步（小时）">
            <el-input-number v-model="cfg.trakt.pull_interval" :min="0" :step="1" step-strictly />
          </el-form-item>
        </el-col>
      </el-row>
      <el-text type="info" size="small" class="trakt-note">
        已看记录和进度的全量同步可在“运行状态”页的 Trakt 卡片中手动执行；上面的间隔填 0 表示只手动执行，大于 0 时引擎启动后每隔该时长自动执行一次（不预览，直接执行）。
        两个方向都只会补充，不会改为未看或删除记录。
      </el-text>
      <div class="trakt-accounts">
        <el-button :icon="Plus" :disabled="!traktReady" @click="traktDialog = true">授权 Trakt 账户</el-button>
        <el-tag
          v-for="u in traktAccounts"
          :key="u"
          closable
          size="large"
          @close="removeTraktAccount(u)"
        >
          {{ u }}
        </el-tag>
        <el-text v-if="!traktAccounts.length" type="info">尚未授权任何账户</el-text>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>同步设置</template>
      <el-row :gutter="24">
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="对账间隔（秒）">
            <el-input-number v-model="cfg.sync.reconcile_interval" :min="60" :step="60" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="增量轮询间隔（秒）">
            <el-input-number v-model="cfg.sync.poll_interval" :min="5" :step="5" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="Plex 标记未看检测间隔（秒）">
            <el-input-number v-model="cfg.sync.unwatch_poll_interval" :min="30" :step="30" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="播放进度推送间隔（秒）">
            <el-input-number v-model="cfg.sync.progress_interval" :min="10" :step="10" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="防回环窗口（秒）">
            <el-input-number v-model="cfg.sync.echo_window" :min="1" :max="120" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-form-item label="日志级别">
            <el-select v-model="cfg.log_level">
              <el-option v-for="l in ['DEBUG', 'INFO', 'WARNING', 'ERROR']" :key="l" :label="l" :value="l" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="Dry-run 模式">
        <el-switch v-model="cfg.sync.dry_run" />
        <el-text type="info" class="switch-hint">开启后只在日志中显示将要执行的修改，不实际写入</el-text>
      </el-form-item>
    </el-card>

    <div class="actions">
      <el-button type="primary" size="large" :loading="saving" @click="save">保存并重启同步</el-button>
    </div>
  </el-form>
  <TraktAuthDialog
    v-model="traktDialog"
    :client-id="cfg.trakt.client_id.trim()"
    :client-secret="cfg.trakt.client_secret.trim()"
    @authorized="onTraktAuthorized"
  />
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.panel > .el-row > .el-col {
  margin-bottom: 12px;
}
.panel > .el-row {
  margin-bottom: -12px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hint {
  margin-bottom: 12px;
}
.hint p {
  margin: 0;
}
.trakt-note {
  display: block;
  margin-bottom: 12px;
}
.trakt-accounts {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.switch-hint {
  margin-left: 12px;
}
.actions {
  display: flex;
  justify-content: flex-end;
}
</style>
