import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { llmApi, type LlmProvider } from '../api/llm'
import type { NewsOut } from '../api/types'
import { UiSelect } from '../components/UI/UiSelect'
import { useFetch } from '../hooks/useFetch'
import { useRecentModels } from '../lib/recentModels'
import '../style/llm.css'

type Turn = {
  id: number
  role: 'user' | 'assistant'
  text: string
  images?: string[]
  latency_ms?: number
  usage?: Record<string, unknown>
  reasoning?: string | null
}

const THINKING_LEVELS = ['off', 'low', 'medium', 'high', 'xhigh', 'max']
const RECENT_PREFIX = 'recent|'
const MAX_IMAGES = 6
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']

const EXAMPLES = [
  'Explain in 3 bullet points how a hotter-than-expected US CPI usually moves EURUSD.',
  'Reply with exactly {"direction":"up","confidence":70} and nothing else.',
  'In two sentences, why do FX traders watch Non-Farm Payrolls?',
]

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function read(key: string): string {
  try {
    return localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable */
  }
}

function tokensText(u: Record<string, unknown> | undefined): string | null {
  if (!u) return null
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const input = num(u.prompt_tokens) ?? num(u.input_tokens)
  const output = num(u.completion_tokens) ?? num(u.output_tokens)
  const total = num(u.total_tokens)
  if (input !== null && output !== null) return `${input} in · ${output} out tokens`
  if (total !== null) return `${total} tokens`
  return null
}

/** Prompt that asks for a direction prediction for one news / calendar item. */
function newsPrompt(n: NewsOut): string {
  const lines = [
    'Predict the direction of the market reaction to this high-impact event over the next hour.',
    '',
    `Type: ${n.kind === 'calendar' ? 'economic calendar release' : 'news headline'}`,
    `Time (UTC): ${n.event_time ? n.event_time.replace('T', ' ').slice(0, 16) : n.fetched_at.replace('T', ' ').slice(0, 16)}`,
    `Source: ${n.source}`,
    `Currency: ${n.currency || n.currencies.join(', ') || '-'}`,
    `Title: ${n.title}`,
  ]
  if (n.summary) lines.push(`Summary: ${n.summary.slice(0, 500)}`)
  if (n.kind === 'calendar') {
    lines.push(`Actual: ${n.actual || 'n/a'} | Forecast: ${n.forecast || 'n/a'} | Previous: ${n.previous || 'n/a'}`)
  }
  if (n.symbols.length) lines.push(`Symbols: ${n.symbols.join(', ')}`)
  lines.push('', 'Reply as JSON: {"direction": "up|down|flat", "confidence": <0-100>, "reasoning": "<one sentence>"}')
  return lines.join('\n')
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error(`Cannot read ${file.name}`))
    reader.readAsDataURL(file)
  })
}

const Icon = ({ children }: { children: React.ReactNode }) => (
  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" strokeLinejoin="miter" aria-hidden>
    {children}
  </svg>
)

export default function LlmLabPage() {
  const modelsQ = useFetch('llm:models', (s) => llmApi.models(s))
  const newsQ = useFetch('llm:latest-high-news', (s) => api.news({ impact: 'High', limit: 1 }, s))

  const providers: LlmProvider[] = useMemo(() => modelsQ.data?.providers ?? [], [modelsQ.data])
  const allModels = useMemo(() => modelsQ.data?.models ?? [], [modelsQ.data])

  const [providerPref, setProviderPref] = useState(() => read('llm.provider'))
  const [modelPref, setModelPref] = useState(() => read('llm.model'))
  const [thinkingPref, setThinkingPref] = useState(() => read('llm.thinking'))
  const [systemPrompt, setSystemPrompt] = useState(() => read('llm.systemPrompt'))
  const [systemOpen, setSystemOpen] = useState(false)
  const { recent, push: pushRecent, clear: clearRecent } = useRecentModels()

  const [turns, setTurns] = useState<Turn[]>([])
  const [prompt, setPrompt] = useState('')
  const [images, setImages] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [presetBusy, setPresetBusy] = useState(false)

  const nextId = useRef(1)
  const abortRef = useRef<AbortController | null>(null)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  // Effective selection: a stored provider that is gone or has a problem falls back to the first usable one.
  const provider = useMemo(() => {
    const usable = providers.filter((p) => !p.problem)
    return usable.find((p) => p.name === providerPref) ?? usable[0] ?? null
  }, [providers, providerPref])

  const providerModels = useMemo(
    () => (provider ? allModels.filter((m) => m.provider === provider.name) : []),
    [allModels, provider],
  )
  const model = providerModels.some((m) => m.model === modelPref) ? modelPref : ''
  const defaultModel = provider?.model ?? ''
  const resolvedModel = model || defaultModel
  const defaultThinking = provider?.thinking ?? ''
  const thinkingChoices = provider?.thinkingChoices
  const thinking =
    THINKING_LEVELS.includes(thinkingPref) && (!thinkingChoices || thinkingChoices.includes(thinkingPref)) ? thinkingPref : ''

  useEffect(() => write('llm.provider', providerPref), [providerPref])
  useEffect(() => write('llm.model', modelPref), [modelPref])
  useEffect(() => write('llm.thinking', thinkingPref), [thinkingPref])
  useEffect(() => write('llm.systemPrompt', systemPrompt), [systemPrompt])
  useEffect(() => () => abortRef.current?.abort(), [])

  // Keep the newest message in view.
  useEffect(() => {
    const el = messagesRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [turns, loading, error])

  // Auto-growing composer.
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [prompt])

  const providerOptions = providers.map((p) => ({
    value: p.name,
    label: p.label || p.name,
    title: p.problem ?? undefined,
    disabled: Boolean(p.problem),
  }))

  const providerRecent = provider
    ? recent.filter((spec) => spec.startsWith(`${provider.name}:`)).map((spec) => spec.slice(provider.name.length + 1))
    : []
  const modelOptions = [
    { value: '', label: defaultModel || 'llm.yaml default' },
    ...providerRecent.map((m) => ({ value: `${RECENT_PREFIX}${m}`, label: m, group: 'Recent' })),
    ...providerModels
      .filter((m) => m.available)
      .map((m) => ({ value: m.model, label: m.model, title: m.spec, group: providerRecent.length ? 'All models' : undefined })),
  ]
  // Recent models of every usable provider (for the quick-pick row).
  const recentAll = recent.filter((spec) => {
    const name = spec.slice(0, spec.indexOf(':'))
    return providers.some((p) => p.name === name && !p.problem)
  })
  const pickRecent = (spec: string) => {
    const i = spec.indexOf(':')
    setProviderPref(spec.slice(0, i))
    setModelPref(spec.slice(i + 1))
  }

  const thinkingOptions = [
    { value: '', label: `llm.yaml (${defaultThinking || 'model default'})` },
    ...THINKING_LEVELS.map((level) => {
      const supported = !thinkingChoices || thinkingChoices.includes(level)
      return {
        value: level,
        label: level,
        disabled: !supported,
        title: supported ? undefined : `Not supported by ${provider?.label ?? 'this provider'}`,
      }
    }),
  ]

  const run = useCallback(
    async (history: Turn[]) => {
      if (!provider || !resolvedModel) {
        setError('Pick a provider and a model first.')
        return
      }
      abortRef.current?.abort()
      const ctl = new AbortController()
      abortRef.current = ctl
      setLoading(true)
      setError(null)
      try {
        const res = await llmApi.chat(
          {
            model: `${provider.name}:${resolvedModel}`,
            messages: history.map((t) => ({ role: t.role, text: t.text, ...(t.images?.length ? { images: t.images } : {}) })),
            system: systemPrompt.trim() ? systemPrompt : null,
            thinking: thinking || null,
          },
          ctl.signal,
        )
        if (ctl.signal.aborted) return
        const used = `${provider.name}:${resolvedModel}`
        pushRecent([used])
        setTurns((prev) => [
          ...prev,
          { id: nextId.current++, role: 'assistant', text: res.text, latency_ms: res.latency_ms, usage: res.usage, reasoning: res.reasoning },
        ])
        setLoading(false)
      } catch (err) {
        if (ctl.signal.aborted) return
        setError(errMessage(err))
        setLoading(false)
      }
    },
    [provider, resolvedModel, systemPrompt, thinking, pushRecent],
  )

  const canSend = (prompt.trim() !== '' || images.length > 0) && !loading && Boolean(provider)

  const send = () => {
    if (!canSend) return
    const turn: Turn = { id: nextId.current++, role: 'user', text: prompt.trim(), ...(images.length ? { images } : {}) }
    const history = [...turns, turn]
    setTurns(history)
    setPrompt('')
    setImages([])
    void run(history)
  }

  const clear = () => {
    abortRef.current?.abort()
    setTurns([])
    setLoading(false)
    setError(null)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  const fillComposer = (text: string) => {
    setPrompt(text)
    inputRef.current?.focus()
  }

  const latestNews = newsQ.data?.[0] ?? null
  const newsSuffix = newsQ.error ? ' (unavailable)' : !newsQ.loading && !latestNews ? ' (none yet)' : ''

  const loadBacktestPrompt = async () => {
    setPresetBusy(true)
    try {
      const presets = await llmApi.presets()
      setSystemPrompt(presets.backtest_system_prompt)
      setSystemOpen(true)
      setError(null)
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setPresetBusy(false)
    }
  }

  const attach = async (files: FileList | File[] | null) => {
    if (!files) return
    const picked = Array.from(files)
    const added: string[] = []
    let problem: string | null = null
    for (const file of picked) {
      if (images.length + added.length >= MAX_IMAGES) {
        problem = `At most ${MAX_IMAGES} images per message.`
        break
      }
      if (!IMAGE_TYPES.includes(file.type)) {
        problem = `${file.name}: only png, jpeg, gif and webp images are supported.`
        continue
      }
      if (file.size > MAX_IMAGE_BYTES) {
        problem = `${file.name}: larger than 5 MB.`
        continue
      }
      try {
        added.push(await readDataUrl(file))
      } catch (err) {
        problem = errMessage(err)
      }
    }
    if (added.length) setImages((prev) => [...prev, ...added].slice(0, MAX_IMAGES))
    setError(problem)
  }

  const systemSummary = systemPrompt.trim() ? systemPrompt.trim().replace(/\s+/g, ' ').slice(0, 80) : 'none'

  return (
    <div className="llm-page">
      <section className="llm-chat" aria-label="Test chat">
        <header className="llm-head">
          <div className="llm-head-text">
            <h1 className="llm-title">Test chat</h1>
            <p className="llm-subtitle">Not saved · nothing here changes backtest data</p>
          </div>
          <button type="button" className="llm-btn llm-clear" onClick={clear} disabled={turns.length === 0 && !loading && !error}>
            <Icon>
              <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />
            </Icon>
            Clear
          </button>
        </header>

        <div className="llm-settings">
          <div className="llm-field">
            <span className="llm-label">Provider</span>
            <UiSelect
              size="sm"
              className="llm-select"
              ariaLabel="Provider"
              value={provider?.name ?? ''}
              disabled={providers.length === 0}
              onChange={(v) => {
                setProviderPref(v)
                setModelPref('')
                setThinkingPref('')
              }}
              options={providerOptions.length ? providerOptions : [{ value: '', label: modelsQ.loading ? 'Loading…' : 'No providers' }]}
            />
          </div>
          <div className="llm-field">
            <span className="llm-label">Model</span>
            <UiSelect
              size="sm"
              className="llm-select"
              ariaLabel="Model"
              searchable
              muted={model === ''}
              value={model}
              disabled={!provider}
              onChange={(v) => setModelPref(v.startsWith(RECENT_PREFIX) ? v.slice(RECENT_PREFIX.length) : v)}
              options={modelOptions}
            />
          </div>
          <div className="llm-field">
            <span className="llm-label">Thinking</span>
            <UiSelect
              size="sm"
              className="llm-select"
              ariaLabel="Thinking"
              muted={thinking === ''}
              value={thinking}
              disabled={!provider}
              onChange={setThinkingPref}
              options={thinkingOptions}
            />
          </div>
        </div>
        {modelsQ.error ? (
          <div className="llm-note llm-note-error" role="alert">
            {modelsQ.error.message}
            <button type="button" className="llm-link" onClick={modelsQ.reload}>
              Retry
            </button>
          </div>
        ) : null}

        {recentAll.length ? (
          <div className="llm-recent">
            <span className="llm-label">Recent</span>
            {recentAll.map((spec) => {
              const i = spec.indexOf(':')
              const name = spec.slice(0, i)
              const active = provider?.name === name && resolvedModel === spec.slice(i + 1)
              return (
                <button
                  key={spec}
                  type="button"
                  className={`llm-recent-chip${active ? ' on' : ''}`}
                  title={spec}
                  onClick={() => pickRecent(spec)}
                >
                  <span className="llm-recent-provider">{providers.find((p) => p.name === name)?.label ?? name}</span>
                  {spec.slice(i + 1)}
                </button>
              )
            })}
            <button type="button" className="llm-recent-clear" title="Forget recent models" onClick={clearRecent}>
              ×
            </button>
          </div>
        ) : null}
        <div className="llm-system">
          <button type="button" className="llm-system-row" aria-expanded={systemOpen} onClick={() => setSystemOpen((o) => !o)}>
            <span className={`llm-chevron${systemOpen ? ' is-open' : ''}`} aria-hidden>
              ›
            </span>
            <span className="llm-label">System prompt</span>
            <span className="llm-system-value">{systemSummary}</span>
          </button>
          {systemOpen ? (
            <textarea
              className="llm-system-input"
              aria-label="System prompt"
              rows={5}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="Optional system prompt sent before the conversation"
            />
          ) : null}
        </div>

        <div className="llm-messages" ref={messagesRef}>
          {turns.length === 0 && !loading ? (
            <div className="llm-empty">
              <h2 className="llm-empty-title">Try prompts on any model</h2>
              <p className="llm-empty-text">Pick a provider and model above (empty = llm.yaml). Nothing here is saved.</p>
              <button type="button" className="llm-btn llm-wide" disabled={!latestNews} onClick={() => latestNews && fillComposer(newsPrompt(latestNews))}>
                Load the latest high-impact news{newsSuffix}
              </button>
              <button type="button" className="llm-btn llm-wide" disabled={presetBusy} onClick={() => void loadBacktestPrompt()}>
                Use the backtest system prompt
              </button>
              <ul className="llm-examples">
                {EXAMPLES.map((ex) => (
                  <li key={ex}>
                    <button type="button" className="llm-example" onClick={() => fillComposer(ex)}>
                      {ex}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="llm-thread">
              {turns.map((t) =>
                t.role === 'user' ? (
                  <div key={t.id} className="llm-msg llm-msg-user">
                    {t.images?.length ? (
                      <div className="llm-msg-images">
                        {t.images.map((src, i) => (
                          <img key={i} src={src} alt="" />
                        ))}
                      </div>
                    ) : null}
                    <div className="llm-bubble">{t.text}</div>
                  </div>
                ) : (
                  <div key={t.id} className="llm-msg llm-msg-assistant">
                    <div className="llm-text">{t.text || '(empty reply)'}</div>
                    <div className="llm-meta">
                      {t.latency_ms != null ? <span>{(t.latency_ms / 1000).toFixed(1)}s</span> : null}
                      {tokensText(t.usage) ? <span>{tokensText(t.usage)}</span> : null}
                      {t.reasoning ? (
                        <details className="llm-reasoning">
                          <summary>Reasoning</summary>
                          <pre>{t.reasoning}</pre>
                        </details>
                      ) : null}
                    </div>
                  </div>
                ),
              )}
              {loading ? (
                <div className="llm-msg llm-msg-assistant" role="status" aria-label="Waiting for the model">
                  <span className="llm-dots" aria-hidden>
                    <i />
                    <i />
                    <i />
                  </span>
                </div>
              ) : null}
            </div>
          )}
          {error ? (
            <div className="llm-note llm-note-error" role="alert">
              <span>{error}</span>
              {!loading && turns.length > 0 && turns[turns.length - 1].role === 'user' ? (
                <button type="button" className="llm-link" onClick={() => void run(turns)}>
                  Retry
                </button>
              ) : null}
            </div>
          ) : null}
        </div>

        <div
          className="llm-composer"
          onDragOver={(e) => {
            if (e.dataTransfer.types.includes('Files')) e.preventDefault()
          }}
          onDrop={(e) => {
            const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith('image/'))
            if (files.length) {
              e.preventDefault()
              void attach(files)
            }
          }}
        >
          {images.length ? (
            <div className="llm-thumbs">
              {images.map((src, i) => (
                <span key={i} className="llm-thumb">
                  <img src={src} alt={`Attachment ${i + 1}`} />
                  <button type="button" aria-label={`Remove image ${i + 1}`} onClick={() => setImages((prev) => prev.filter((_, j) => j !== i))}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          ) : null}
          <div className="llm-composer-row">
            <input
              ref={fileRef}
              type="file"
              accept={IMAGE_TYPES.join(',')}
              multiple
              hidden
              onChange={(e) => {
                void attach(e.target.files)
                e.target.value = ''
              }}
            />
            <button
              type="button"
              className="llm-btn llm-icon-btn"
              aria-label="Attach images"
              title="Attach images"
              disabled={images.length >= MAX_IMAGES}
              onClick={() => fileRef.current?.click()}
            >
              <Icon>
                <rect x="3" y="4" width="18" height="16" />
                <circle cx="9" cy="10" r="1.6" />
                <path d="M21 16l-5-5-8 9" />
              </Icon>
            </button>
            <textarea
              ref={inputRef}
              className="llm-input"
              aria-label="Message"
              rows={1}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={onKeyDown}
              onPaste={(e) => {
                const files = Array.from(e.clipboardData.items)
                  .filter((it) => it.kind === 'file' && it.type.startsWith('image/'))
                  .map((it) => it.getAsFile())
                  .filter((f): f is File => f !== null)
                if (files.length) {
                  e.preventDefault()
                  void attach(files)
                }
              }}
              placeholder="Message… (Enter to send, Shift+Enter for a new line)"
            />
            <button type="button" className="llm-btn llm-send" onClick={send} disabled={!canSend}>
              <Icon>
                <path d="M22 2L11 13M22 2l-7 20-4-9-9-4z" />
              </Icon>
              SEND
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}
