import { useCallback, useMemo, useRef, useState, type DragEvent } from 'react'
import {
  backupUrl,
  dataApi,
  exportUrl,
  importFile,
  type BackupInfo,
  type DataTable,
  type ExportFormat,
  type ImportMode,
  type ImportResult,
} from '../api/data'
import { Modal } from '../components/Modal'
import { UiSelect } from '../components/UI/UiSelect'
import { ErrorNote, Spinner } from '../components/widgets'
import { useFetch } from '../hooks/useFetch'
import { fmtDateTime } from '../lib/format'
import '../style/data.css'

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}

const fmtN = (n: number) => n.toLocaleString('en-US')

const MODE_OPTIONS = [
  { value: 'merge', label: 'Merge (recommended)' },
  { value: 'replace', label: 'Replace selected tables' },
]

function TableChecks({
  tables,
  selected,
  onChange,
  disabled,
}: {
  tables: DataTable[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
  disabled?: boolean
}) {
  const toggle = (name: string) => {
    const next = new Set(selected)
    if (next.has(name)) next.delete(name)
    else next.add(name)
    onChange(next)
  }
  return (
    <div className="data-checks" role="group" aria-label="Tables">
      {tables.map((t) => (
        <label key={t.name} className="data-check">
          <input type="checkbox" checked={selected.has(t.name)} disabled={disabled} onChange={() => toggle(t.name)} />
          <span className="data-check-name">{t.name}</span>
          <span className="data-check-meta">{t.label}</span>
        </label>
      ))}
    </div>
  )
}

function ExportSection({ tables }: { tables: DataTable[] }) {
  const [format, setFormat] = useState<ExportFormat>('sqlite')
  const [picked, setPicked] = useState<Set<string> | null>(null) // null = all
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const selected = useMemo(() => picked ?? new Set(tables.map((t) => t.name)), [picked, tables])
  const isJson = format === 'json'
  const rangeBad = isJson && from !== '' && to !== '' && from > to
  const none = isJson && selected.size === 0
  const url = exportUrl({
    format,
    tables: selected.size === tables.length ? undefined : [...selected],
    from: from || undefined,
    to: to || undefined,
  })
  const blocked = rangeBad || none

  return (
    <section className="data-panel">
      <header className="data-panel-head">
        <h2>Export</h2>
        <p>Download a copy of your data to move it to another machine or keep it as an archive.</p>
      </header>
      <div className="data-panel-body">
        <div className="data-radios" role="radiogroup" aria-label="Export format">
          <label className={`data-radio${format === 'sqlite' ? ' on' : ''}`}>
            <input type="radio" name="fmt" checked={format === 'sqlite'} onChange={() => setFormat('sqlite')} />
            <span className="data-radio-title">SQLite file (.db)</span>
            <span className="data-radio-meta">Whole database, exact snapshot. Best for a full backup.</span>
          </label>
          <label className={`data-radio${isJson ? ' on' : ''}`}>
            <input type="radio" name="fmt" checked={isJson} onChange={() => setFormat('json')} />
            <span className="data-radio-title">JSON, gzip (.json.gz)</span>
            <span className="data-radio-meta">Choose tables and a date range. Readable and portable.</span>
          </label>
        </div>

        {isJson ? (
          <>
            <div className="data-field">
              <div className="data-label-row">
                <span className="data-label">Tables</span>
                <span className="data-links">
                  <button type="button" className="data-link" onClick={() => setPicked(new Set(tables.map((t) => t.name)))}>
                    All
                  </button>
                  <button type="button" className="data-link" onClick={() => setPicked(new Set())}>
                    None
                  </button>
                </span>
              </div>
              <TableChecks tables={tables} selected={selected} onChange={setPicked} />
            </div>
            <div className="data-field">
              <span className="data-label">Date range (UTC, optional)</span>
              <div className="data-range">
                <input type="date" aria-label="From date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} />
                <span className="data-range-sep">to</span>
                <input type="date" aria-label="To date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} />
                {from || to ? (
                  <button type="button" className="data-link" onClick={() => (setFrom(''), setTo(''))}>
                    Clear
                  </button>
                ) : null}
              </div>
              <p className="data-hint">
                Filters news by event time, analyses and signals by creation time, and backtest events by release time
                (their outcomes and predictions follow).
              </p>
            </div>
          </>
        ) : null}

        {rangeBad ? <p className="data-error">The start date is after the end date.</p> : null}
        <div className="data-actions">
          <a
            className={`data-btn primary${blocked ? ' disabled' : ''}`}
            href={blocked ? undefined : url}
            download
            aria-disabled={blocked}
            onClick={(e) => blocked && e.preventDefault()}
          >
            Download {isJson ? '.json.gz' : '.db'}
          </a>
        </div>
      </div>
    </section>
  )
}

function ResultView({ result, onDismiss }: { result: ImportResult; onDismiss: () => void }) {
  const rows = Object.entries(result.tables)
  return (
    <div className="data-result" role="status">
      <div className="data-result-head">
        <b>{result.mode === 'replace' ? 'Replace' : 'Merge'} finished</b>
        <button type="button" className="data-link" onClick={onDismiss}>
          Dismiss
        </button>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Table</th>
            <th className="num">Inserted</th>
            <th className="num">Updated</th>
            <th className="num">Skipped</th>
            {result.mode === 'replace' ? <th className="num">Deleted</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, c]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="num">{fmtN(c.inserted)}</td>
              <td className="num">{fmtN(c.updated)}</td>
              <td className="num">{fmtN(c.skipped)}</td>
              {result.mode === 'replace' ? <td className="num">{fmtN(c.deleted ?? 0)}</td> : null}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="data-hint">
        Your previous data was saved first as <span className="mono">{result.backup}</span> (see Backups below).
      </p>
      {result.warnings.length ? (
        <ul className="data-warnings">
          {result.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function ImportSection({ tables, onDone }: { tables: DataTable[]; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [mode, setMode] = useState<ImportMode>('merge')
  const [picked, setPicked] = useState<Set<string> | null>(null)
  const [dragging, setDragging] = useState(false)
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ImportResult | null>(null)
  const [confirm, setConfirm] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const selected = useMemo(() => picked ?? new Set(tables.map((t) => t.name)), [picked, tables])
  const allSelected = selected.size === tables.length

  const pick = (f: File | null | undefined) => {
    if (!f) return
    setFile(f)
    setResult(null)
    setError(null)
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    pick(e.dataTransfer.files[0])
  }

  const run = useCallback(async () => {
    if (!file) return
    setConfirm(false)
    setBusy(true)
    setError(null)
    setResult(null)
    setProgress({ loaded: 0, total: file.size })
    try {
      const res = await importFile(file, {
        mode,
        tables: allSelected ? undefined : [...selected],
        onProgress: (loaded, total) => setProgress({ loaded, total }),
      })
      setResult(res)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      onDone()
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }, [file, mode, allSelected, selected, onDone])

  const start = () => {
    if (mode === 'replace') setConfirm(true)
    else void run()
  }
  const pct = progress && progress.total > 0 ? Math.round((progress.loaded / progress.total) * 100) : 0
  const names = allSelected ? 'all tables in the file' : [...selected].join(', ')

  return (
    <section className="data-panel">
      <header className="data-panel-head">
        <h2>Import</h2>
        <p>Load a SQLite file or a JSON export. A backup of the current data is always taken first.</p>
      </header>
      <div className="data-panel-body">
        <div
          className={`data-drop${dragging ? ' over' : ''}${file ? ' has-file' : ''}`}
          onDragOver={(e) => (e.preventDefault(), setDragging(true))}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".db,.sqlite,.sqlite3,.json,.gz,application/json,application/gzip"
            hidden
            onChange={(e) => pick(e.target.files?.[0])}
          />
          {file ? (
            <div className="data-file">
              <span className="mono">{file.name}</span>
              <span className="data-hint">{fmtBytes(file.size)}</span>
            </div>
          ) : (
            <span className="data-drop-text">Drop a .db, .json or .json.gz file here</span>
          )}
          <button type="button" className="data-btn" disabled={busy} onClick={() => inputRef.current?.click()}>
            {file ? 'Choose another' : 'Choose file'}
          </button>
        </div>

        <div className="data-field">
          <span className="data-label">Mode</span>
          <UiSelect value={mode} options={MODE_OPTIONS} onChange={(v) => setMode(v as ImportMode)} ariaLabel="Import mode" size="md" disabled={busy} />
          {mode === 'merge' ? (
            <p className="data-hint">
              Adds rows that are missing and updates rows that are older than the imported ones. Nothing is deleted;
              importing the same file twice changes nothing.
            </p>
          ) : (
            <p className="data-warn">
              Replace deletes every existing row in the selected tables (and the tables that depend on them) and loads
              the file instead. A backup is taken first and can be restored below.
            </p>
          )}
        </div>

        <div className="data-field">
          <div className="data-label-row">
            <span className="data-label">Tables</span>
            <span className="data-links">
              <button type="button" className="data-link" onClick={() => setPicked(new Set(tables.map((t) => t.name)))}>
                All
              </button>
              <button type="button" className="data-link" onClick={() => setPicked(new Set())}>
                None
              </button>
            </span>
          </div>
          <TableChecks tables={tables} selected={selected} onChange={setPicked} disabled={busy} />
          <p className="data-hint">Tables that are not in the file are skipped.</p>
        </div>

        {progress ? (
          <div className="data-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
            <div className="data-progress-bar" style={{ width: `${pct}%` }} />
            <span>{pct >= 100 ? 'Processing…' : `Uploading ${pct}% (${fmtBytes(progress.loaded)} / ${fmtBytes(progress.total)})`}</span>
          </div>
        ) : null}
        {error ? <p className="data-error">{error}</p> : null}
        {result ? <ResultView result={result} onDismiss={() => setResult(null)} /> : null}

        <div className="data-actions">
          <button
            type="button"
            className={`data-btn ${mode === 'replace' ? 'danger' : 'primary'}`}
            disabled={!file || busy || selected.size === 0}
            onClick={start}
          >
            {busy ? 'Importing…' : mode === 'replace' ? 'Replace data' : 'Import'}
          </button>
        </div>
      </div>

      {confirm && file ? (
        <Modal title="Replace existing data?" onClose={() => setConfirm(false)}>
          <div className="data-confirm">
            <p>
              This will <b>delete all current rows</b> in {names} and load <span className="mono">{file.name}</span>{' '}
              instead.
            </p>
            <p className="data-hint">The current database is saved as a backup first, so you can restore it later.</p>
            <div className="data-actions">
              <button type="button" className="data-btn" onClick={() => setConfirm(false)}>
                Cancel
              </button>
              <button type="button" className="data-btn danger" onClick={() => void run()}>
                Replace
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </section>
  )
}

function BackupsSection({ rev, onRestored }: { rev: number; onRestored: () => void }) {
  const q = useFetch<BackupInfo[]>(`backups:${rev}`, (signal) => dataApi.backups(signal))
  const [target, setTarget] = useState<BackupInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const create = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const b = await dataApi.createBackup()
      setNotice(`Backup created: ${b.name}`)
      q.reload()
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    if (!target) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const r = await dataApi.restore(target.name)
      setNotice(`Restored ${r.restored}. The previous data was saved as ${r.backup}.`)
      setTarget(null)
      q.reload()
      onRestored()
    } catch (err) {
      setError(errMessage(err))
      setTarget(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="data-panel">
      <header className="data-panel-head">
        <h2>Backups</h2>
        <p>Snapshots are created automatically before every import or restore; the newest ones are kept.</p>
      </header>
      <div className="data-panel-body">
        {q.loading ? <Spinner label="Loading backups" /> : null}
        {q.error && !q.data ? <ErrorNote error={q.error} onRetry={q.reload} /> : null}
        {q.data && q.data.length === 0 ? <p className="data-hint">No backups yet.</p> : null}
        {q.data && q.data.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Created</th>
                <th className="num">Size</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {q.data.map((b) => (
                <tr key={b.name}>
                  <td className="mono">{b.name}</td>
                  <td>{fmtDateTime(Date.parse(b.created_at))}</td>
                  <td className="num">{fmtBytes(b.size_bytes)}</td>
                  <td className="data-row-actions">
                    <a className="data-btn sm" href={backupUrl(b.name)} download>
                      Download
                    </a>
                    <button type="button" className="data-btn sm danger" disabled={busy} onClick={() => setTarget(b)}>
                      Restore
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {notice ? <p className="data-ok">{notice}</p> : null}
        {error ? <p className="data-error">{error}</p> : null}
        <div className="data-actions">
          <button type="button" className="data-btn" disabled={busy} onClick={() => void create()}>
            Create backup now
          </button>
        </div>
      </div>

      {target ? (
        <Modal title="Restore this backup?" onClose={() => (busy ? undefined : setTarget(null))}>
          <div className="data-confirm">
            <p>
              The whole database will be replaced with <span className="mono">{target.name}</span> (
              {fmtDateTime(Date.parse(target.created_at))}). Anything added since then is lost, unless you restore the
              automatic backup that is taken right before.
            </p>
            <div className="data-actions">
              <button type="button" className="data-btn" disabled={busy} onClick={() => setTarget(null)}>
                Cancel
              </button>
              <button type="button" className="data-btn danger" disabled={busy} onClick={() => void restore()}>
                {busy ? 'Restoring…' : 'Restore'}
              </button>
            </div>
          </div>
        </Modal>
      ) : null}
    </section>
  )
}

export function DataPage() {
  const overview = useFetch('data-overview', (signal) => dataApi.overview(signal))
  const [rev, setRev] = useState(0)
  const changed = useCallback(() => {
    overview.reload()
    setRev((r) => r + 1)
  }, [overview])

  const data = overview.data
  const total = data ? data.tables.reduce((a, t) => a + t.rows, 0) : 0

  return (
    <div className="data-page">
      <div className="data-wrap">
        <section className="data-panel">
          <header className="data-panel-head">
            <h2>Database</h2>
            <p>Everything FXNews stores lives in one SQLite file.</p>
          </header>
          <div className="data-panel-body">
            {overview.loading ? <Spinner label="Loading" /> : null}
            {overview.error && !data ? <ErrorNote error={overview.error} onRetry={overview.reload} /> : null}
            {data ? (
              <>
                <dl className="data-facts">
                  <div>
                    <dt>File</dt>
                    <dd className="mono" title={data.db_path}>
                      {data.db_path}
                    </dd>
                  </div>
                  <div>
                    <dt>Size</dt>
                    <dd>{fmtBytes(data.size_bytes)}</dd>
                  </div>
                  <div>
                    <dt>Rows</dt>
                    <dd>{fmtN(total)}</dd>
                  </div>
                  <div>
                    <dt>Backups folder</dt>
                    <dd className="mono" title={data.backup_dir}>
                      {data.backup_dir}
                    </dd>
                  </div>
                </dl>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Table</th>
                      <th>Contents</th>
                      <th className="num">Rows</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.tables.map((t) => (
                      <tr key={t.name}>
                        <td className="mono">{t.name}</td>
                        <td>{t.label}</td>
                        <td className="num">{fmtN(t.rows)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : null}
          </div>
        </section>

        {data ? (
          <>
            <ExportSection tables={data.tables} />
            <ImportSection tables={data.tables} onDone={changed} />
          </>
        ) : null}
        <BackupsSection rev={rev} onRestored={changed} />
      </div>
    </div>
  )
}
