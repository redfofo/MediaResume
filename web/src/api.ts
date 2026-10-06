export interface Mapping {
  plex_user: string | null
  emby_user: string
  plex_token: string | null
  trakt_user: string | null
}

export interface Config {
  plex: { url: string; token: string }
  emby: { url: string; api_key: string }
  trakt: {
    client_id: string
    client_secret: string
    scrobble: boolean
    push_interval: number
    pull_interval: number
  }
  mappings: Mapping[]
  sync: {
    reconcile_interval: number
    poll_interval: number
    unwatch_poll_interval: number
    progress_interval: number
    echo_window: number
    dry_run: boolean
  }
  db_path: string
  log_level: string
}

export interface ReconcileStats {
  at: number
  plex?: number
  emby?: number
  matched?: number
  changed?: number
  seconds?: number
  error?: string
}

export interface Status {
  configured: boolean
  running: boolean
  error: string | null
  started_at: number | null
  dry_run: boolean
  plex_ws: boolean
  last_poll: number | null
  poll_error: string | null
  reconciling: boolean
  queue: number
  pairs: {
    id: string
    plex_user: string | null
    emby_user: string
    trakt_user: string | null
    reconcile: ReconcileStats | null
  }[]
  trakt: TraktStatus[]
  // 定时全量同步的下次执行时间（unix 秒），未开启的方向没有
  trakt_next: Partial<Record<TraktDirection, number>>
}

export interface TraktStatus {
  user: string
  pending: number
  progress_pushed: number
  last_push: number | null
  busy: boolean
  error: string | null
}

export type TraktDirection = 'to_trakt' | 'from_trakt'

export interface TraktPlanEntry {
  key: string
  title: string
  watched_at?: number
  local_pct?: number
  trakt_pct?: number | null
}

export interface TraktPlan {
  direction: TraktDirection
  trakt_user: string
  // to_trakt
  history?: TraktPlanEntry[]
  clear?: TraktPlanEntry[]
  // from_trakt
  watched?: TraktPlanEntry[]
  skipped_newer?: number
  kept_unwatched?: number
  // 两个方向都有
  progress: TraktPlanEntry[]
}

export interface TraktDeviceCode {
  ok: boolean
  error?: string
  device_code?: string
  user_code?: string
  verification_url?: string
  expires_in?: number
  interval?: number
}

export interface TraktDeviceToken {
  status: 'ok' | 'pending' | 'slow_down' | 'invalid' | 'used' | 'expired' | 'denied' | 'error'
  username?: string
  error?: string
}

export interface LogRecord {
  seq: number
  time: number
  level: string
  name: string
  message: string
}

export interface PlexTest {
  ok: boolean
  error?: string
  name?: string
  version?: string
  accounts?: { id: number; name: string; owner: boolean }[]
}

export interface EmbyTest {
  ok: boolean
  error?: string
  name?: string
  version?: string
  users?: { id: string; name: string }[]
}

export type ResumeDirection = 'plex_to_emby' | 'emby_to_plex'

export interface ResumePlanEntry {
  group: string
  title: string
  episode?: string
  ids?: string[]
  action?: 'progress' | 'unhide'
  position_ms?: number
  reason?: string
}

export interface ResumePlan {
  src: 'plex' | 'emby'
  dst: 'plex' | 'emby'
  remove: ResumePlanEntry[]
  add: ResumePlanEntry[]
  unsupported: ResumePlanEntry[]
  same: number
}

export interface ResumeResult extends ResumePlan {
  dry_run: boolean
  removed: number
  added: number
  errors: string[]
  dst_count: number | null
}

export interface ResumePairBase {
  id: string
  plex_user: string | null
  emby_user: string
  error?: string
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method,
    // 写接口一律以 JSON 发送（服务端据此拦截跨站请求）
    headers: method === 'GET' ? undefined : { 'Content-Type': 'application/json' },
    body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`)
  return data as T
}

export const api = {
  getConfig: () => request<{ exists: boolean; config: Config | null; error?: string }>('GET', '/api/config'),
  saveConfig: (cfg: Config) => request<{ ok: boolean; status: Status }>('PUT', '/api/config', cfg),
  testPlex: (url: string, token: string) => request<PlexTest>('POST', '/api/test/plex', { url, token }),
  testEmby: (url: string, api_key: string) => request<EmbyTest>('POST', '/api/test/emby', { url, api_key }),
  status: () => request<Status>('GET', '/api/status'),
  reconcile: () => request<{ ok: boolean }>('POST', '/api/reconcile'),
  restart: () => request<{ ok: boolean; status: Status }>('POST', '/api/restart'),
  resumePreview: (direction: ResumeDirection) =>
    request<{ pairs: (ResumePairBase & { plan?: ResumePlan })[]; dry_run: boolean }>('POST', '/api/resume-sync/preview', {
      direction,
    }),
  resumeExecute: (direction: ResumeDirection) =>
    request<{ pairs: (ResumePairBase & { result?: ResumeResult })[] }>('POST', '/api/resume-sync/execute', { direction }),
  traktAccounts: () => request<{ accounts: string[] }>('GET', '/api/trakt/accounts'),
  traktDeviceCode: (client_id: string, client_secret: string) =>
    request<TraktDeviceCode>('POST', '/api/trakt/device-code', { client_id, client_secret }),
  traktDeviceToken: (client_id: string, client_secret: string, device_code: string) =>
    request<TraktDeviceToken>('POST', '/api/trakt/device-token', { client_id, client_secret, device_code }),
  traktSyncPreview: (direction: TraktDirection) =>
    request<{ pairs: (ResumePairBase & { plan?: TraktPlan })[]; dry_run: boolean }>('POST', '/api/trakt/sync/preview', {
      direction,
    }),
  traktSyncExecute: (direction: TraktDirection) =>
    request<{ pairs: (ResumePairBase & { counts?: Record<string, number> })[] }>('POST', '/api/trakt/sync/execute', {
      direction,
    }),
  traktDeleteAccount: (username: string) =>
    request<{ ok: boolean }>('DELETE', `/api/trakt/accounts/${encodeURIComponent(username)}`),
  logs: (after: number) => request<{ seq: number; logs: LogRecord[] }>('GET', `/api/logs?after=${after}`),
}

export function defaultConfig(): Config {
  return {
    plex: { url: 'http://127.0.0.1:32400', token: '' },
    emby: { url: 'http://127.0.0.1:8096', api_key: '' },
    trakt: { client_id: '', client_secret: '', scrobble: true, push_interval: 0, pull_interval: 0 },
    mappings: [{ plex_user: null, emby_user: '', plex_token: null, trakt_user: null }],
    sync: { reconcile_interval: 900, poll_interval: 30, unwatch_poll_interval: 120, progress_interval: 60, echo_window: 10, dry_run: true },
    db_path: 'data/state.db',
    log_level: 'INFO',
  }
}
