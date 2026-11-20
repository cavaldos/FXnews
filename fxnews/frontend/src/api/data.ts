import { ApiError } from './client'

const BASE = '/api/data'

export interface DataTable {
  name: string
  rows: number
  label: string
}

export interface DataOverview {
  tables: DataTable[]
  db_path: string
  size_bytes: number
  backup_dir: string
}

export interface BackupInfo {
  name: string
  size_bytes: number
  created_at: string
}

export type ImportMode = 'merge' | 'replace'

export interface TableImportResult {
  inserted: number
  updated: number
  skipped: number
  deleted?: number
}

export interface ImportResult {
  mode: ImportMode
  backup: string
  format: 'sqlite' | 'json'
  filename: string | null
  tables: Record<string, TableImportResult>
  warnings: string[]
}

export interface RestoreResult {
  restored: string
  backup: string
}

export type ExportFormat = 'sqlite' | 'json'

export interface ExportOptions {
  format: ExportFormat
  /** JSON only; empty = all tables. */
  tables?: string[]
  /** JSON only: YYYY-MM-DD (inclusive). */
  from?: string
  to?: string
}

function detail(status: number, text: string): string {
  try {
    const json = JSON.parse(text) as { detail?: unknown }
    if (typeof json.detail === 'string') return json.detail
    if (Array.isArray(json.detail)) {
      return json.detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join('; ')
    }
  } catch {
    /* not JSON */
  }
  if (status === 0 || status === 502 || status === 503 || status === 504) return 'Backend not responding (is uvicorn running on :8000?)'
  return text.trim().slice(0, 200) || `HTTP ${status}`
}

async function call<T>(method: 'GET' | 'POST', path: string, signal?: AbortSignal): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, { method, signal, headers: { Accept: 'application/json' } })
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err
    throw new ApiError('Could not reach the backend', 0)
  }
  if (!res.ok) throw new ApiError(detail(res.status, await res.text().catch(() => '')), res.status)
  return (await res.json()) as T
}

/** URL of the export download; used as a plain link so the browser streams the file to disk. */
export function exportUrl(opts: ExportOptions): string {
  const qs = new URLSearchParams({ format: opts.format })
  if (opts.format === 'json') {
    if (opts.tables?.length) qs.set('tables', opts.tables.join(','))
    if (opts.from) qs.set('from', opts.from)
    if (opts.to) qs.set('to', opts.to)
  }
  return `${BASE}/export?${qs.toString()}`
}

export const backupUrl = (name: string) => `${BASE}/backups/${encodeURIComponent(name)}`

export interface ImportOptions {
  mode: ImportMode
  tables?: string[]
  onProgress?: (loaded: number, total: number) => void
  signal?: AbortSignal
}

/** Uploads the file as the raw request body (application/octet-stream) with progress reporting. */
export function importFile(file: File, opts: ImportOptions): Promise<ImportResult> {
  return new Promise((resolve, reject) => {
    const qs = new URLSearchParams({ mode: opts.mode, filename: file.name })
    if (opts.tables?.length) qs.set('tables', opts.tables.join(','))
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${BASE}/import?${qs.toString()}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.setRequestHeader('Accept', 'application/json')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onProgress?.(e.loaded, e.total)
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as ImportResult)
        } catch {
          reject(new ApiError('Unexpected response from the backend', xhr.status))
        }
      } else {
        reject(new ApiError(detail(xhr.status, xhr.responseText), xhr.status))
      }
    }
    xhr.onerror = () => reject(new ApiError('Could not reach the backend', 0))
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'))
    opts.signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(file)
  })
}

export const dataApi = {
  overview: (signal?: AbortSignal) => call<DataOverview>('GET', '/tables', signal),
  backups: (signal?: AbortSignal) => call<BackupInfo[]>('GET', '/backups', signal),
  createBackup: () => call<BackupInfo>('POST', '/backups'),
  restore: (name: string) => call<RestoreResult>('POST', `/backups/${encodeURIComponent(name)}/restore`),
}
