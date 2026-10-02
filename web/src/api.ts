export interface Mapping {
  plex_user: string | null
  emby_user: string
  plex_token: string | null
}

export interface Config {
  plex: { url: string; token: string }
  emby: { url: string; api_key: string }
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
  pairs: { id: string; plex_user: string | null; emby_user: string; reconcile: ReconcileStats | null }[]
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
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
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
  logs: (after: number) => request<{ seq: number; logs: LogRecord[] }>('GET', `/api/logs?after=${after}`),
}

export function defaultConfig(): Config {
  return {
    plex: { url: 'http://127.0.0.1:32400', token: '' },
    emby: { url: 'http://127.0.0.1:8096', api_key: '' },
    mappings: [{ plex_user: null, emby_user: '', plex_token: null }],
    sync: { reconcile_interval: 900, poll_interval: 30, unwatch_poll_interval: 120, progress_interval: 60, echo_window: 10, dry_run: true },
    db_path: 'data/state.db',
    log_level: 'INFO',
  }
}
