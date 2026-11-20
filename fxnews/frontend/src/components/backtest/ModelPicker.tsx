import { useState } from 'react'
import type { LlmModel, LlmProvider } from '../../api/llm'
import { UiButton } from '../UI/UiButton'
import { UiSelect } from '../UI/UiSelect'
import { modelLabel } from './btFormat'
import { Popover } from './Popover'

const RECENT_PREFIX = 'recent|'

interface Catalog {
  providers: LlmProvider[]
  models: LlmModel[]
  loading: boolean
  error: Error | undefined
  onRetry: () => void
}

interface Props extends Catalog {
  /** Selected model ids ("provider:model"): used by every Predict button and Predict all. */
  picked: string[]
  onChange: (next: string[]) => void
  /** Recently used models, shared with the LLM Lab. */
  recent: string[]
  onClearRecent: () => void
}

function splitSpec(spec: string): { provider: string; model: string } {
  const i = spec.indexOf(':')
  return { provider: spec.slice(0, i), model: spec.slice(i + 1) }
}

/** Provider + model selects, like the LLM Lab (Recent group first), an Add button, and a Recent quick-pick row. */
function ModelSelector({ providers, models, loading, error, onRetry, picked, onChange, recent, onClearRecent }: Props) {
  const [providerPref, setProviderPref] = useState('')
  const [modelPref, setModelPref] = useState('')
  const [custom, setCustom] = useState('')

  const usable = providers.filter((p) => !p.problem)
  const provider = usable.find((p) => p.name === providerPref) ?? usable[0] ?? null
  const providerModels = provider ? models.filter((m) => m.provider === provider.name) : []
  const model = providerModels.some((m) => m.model === modelPref) ? modelPref : ''
  const defaultModel = provider?.model ?? ''
  const resolved = model || defaultModel
  const spec = provider && resolved ? `${provider.name}:${resolved}` : ''

  const providerOptions = providers.map((p) => ({ value: p.name, label: p.label || p.name, title: p.problem ?? undefined, disabled: Boolean(p.problem) }))
  const providerRecent = provider ? recent.filter((s) => s.startsWith(`${provider.name}:`)).map((s) => s.slice(provider.name.length + 1)) : []
  const modelOptions = [
    { value: '', label: defaultModel || 'llm.yaml default' },
    ...providerRecent.map((m) => ({ value: `${RECENT_PREFIX}${m}`, label: m, group: 'Recent' })),
    ...providerModels.filter((m) => m.available).map((m) => ({ value: m.model, label: m.model, title: m.spec, group: providerRecent.length ? 'All models' : undefined })),
  ]
  const recentUsable = recent.filter((s) => usable.some((p) => p.name === splitSpec(s).provider))

  const toggle = (id: string) => onChange(picked.includes(id) ? picked.filter((m) => m !== id) : [...picked, id])
  const add = (id: string) => {
    if (id && !picked.includes(id)) onChange([...picked, id])
  }
  const addCustom = () => {
    const id = custom.trim()
    if (id.includes(':')) add(id)
    setCustom('')
  }

  return (
    <div className="bt-msel">
      <div className="bt-pop-head">
        <span>Add a model to predict with</span>
      </div>
      {error ? (
        <div className="bt-err block">
          {error.message}{' '}
          <button type="button" className="bt-linkbtn" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : null}
      <div className="bt-msel-field">
        <span>Provider</span>
        <UiSelect
          size="xs"
          className="bt-msel-select"
          ariaLabel="Provider"
          value={provider?.name ?? ''}
          disabled={providers.length === 0}
          onChange={(v) => {
            setProviderPref(v)
            setModelPref('')
          }}
          options={providerOptions.length ? providerOptions : [{ value: '', label: loading ? 'Loading…' : 'No providers' }]}
        />
      </div>
      <div className="bt-msel-field">
        <span>Model</span>
        <UiSelect
          size="xs"
          className="bt-msel-select"
          ariaLabel="Model"
          searchable
          muted={model === ''}
          value={model}
          disabled={!provider}
          onChange={(v) => setModelPref(v.startsWith(RECENT_PREFIX) ? v.slice(RECENT_PREFIX.length) : v)}
          options={modelOptions}
        />
      </div>
      <UiButton size="xs" disabled={!spec || picked.includes(spec)} onClick={() => add(spec)} title={spec || 'Pick a provider and a model'}>
        {spec && picked.includes(spec) ? 'Added' : 'Add'}
      </UiButton>
      {providers.some((p) => p.problem) ? (
        <div className="bt-pop-empty">
          Unavailable:{' '}
          {providers
            .filter((p) => p.problem)
            .map((p) => `${p.label || p.name} (${p.problem})`)
            .join(' · ')}
        </div>
      ) : null}

      {recentUsable.length ? (
        <div className="bt-recent">
          <span className="bt-recent-label">Recent</span>
          {recentUsable.map((s) => {
            const on = picked.includes(s)
            const { provider: pn, model: mn } = splitSpec(s)
            return (
              <button key={s} type="button" className={`bt-rchip${on ? ' on' : ''}`} title={`${s}${on ? ' (selected, click to remove)' : ' (click to add)'}`} aria-pressed={on} onClick={() => toggle(s)}>
                <span className="bt-rchip-prov">{providers.find((p) => p.name === pn)?.label ?? pn}</span>
                {mn}
              </button>
            )
          })}
          <button type="button" className="bt-x" title="Forget recent models" aria-label="Forget recent models" onClick={onClearRecent}>
            ×
          </button>
        </div>
      ) : null}

      <div className="bt-pop-add">
        <input placeholder="or type provider:model" value={custom} onChange={(e) => setCustom(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && addCustom()} aria-label="Custom model id" />
        <UiButton tone="ghost" size="xs" onClick={addCustom} disabled={!custom.trim().includes(':')}>
          Add
        </UiButton>
      </div>
    </div>
  )
}

/** The selected models as removable chips plus the "+ Add model" selector. */
export function ModelPicker(p: Props) {
  const { picked, onChange, providers } = p
  const problems = new Map(providers.filter((x) => x.problem).map((x) => [x.name, x.problem as string]))
  return (
    <div className="bt-models" role="group" aria-label="Models used for predictions">
      <span className="bt-models-label">Models</span>
      {picked.length === 0 ? <span className="dim bt-models-none">none selected</span> : null}
      {picked.map((id) => {
        const problem = problems.get(splitSpec(id).provider)
        return (
          <span key={id} className={`bt-mpick${problem ? ' problem' : ''}`} title={problem ? `${id}: ${problem}` : id}>
            <span className="bt-mpick-prov">{splitSpec(id).provider}</span>
            {modelLabel(id)}
            <button type="button" className="bt-x" aria-label={`Remove ${id}`} onClick={() => onChange(picked.filter((m) => m !== id))}>
              ×
            </button>
          </span>
        )
      })}
      {picked.length > 1 ? (
        <button type="button" className="bt-linkbtn" onClick={() => onChange([])}>
          clear
        </button>
      ) : null}
      <Popover ariaLabel="Add a model" title="Pick a provider and model to predict with" label="+ Add model" triggerClass={picked.length ? '' : 'on'}>
        <ModelSelector {...p} />
      </Popover>
    </div>
  )
}

/** Compact checkbox list used inside the predictions modal. */
export function ModelChecklist({ ids, picked, problems, onChange }: { ids: string[]; picked: string[]; problems: Record<string, string | null>; onChange: (next: string[]) => void }) {
  const toggle = (id: string) => onChange(picked.includes(id) ? picked.filter((m) => m !== id) : [...picked, id])
  if (ids.length === 0) return <span className="dim">No models selected. Add some in the toolbar.</span>
  return (
    <div className="bt-checklist">
      {ids.map((id) => (
        <label key={id} className={`bt-check${problems[id] ? ' problem' : ''}`} title={problems[id] ?? id}>
          <input type="checkbox" checked={picked.includes(id)} onChange={() => toggle(id)} />
          <span className="bt-check-name">{id}</span>
          {problems[id] ? <span className="bt-warn">⚠</span> : null}
        </label>
      ))}
    </div>
  )
}
