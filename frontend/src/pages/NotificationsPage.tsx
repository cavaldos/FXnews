import { useState } from 'react'

import { api } from '../api/client'
import type { Impact, NotifyAccountIn, NotifyChannel, NotifyRules, NotifySettingsIn } from '../api/types'
import { UiButton } from '../components/UI/UiButton'
import { UiSelect } from '../components/UI/UiSelect'
import { ErrorNote, Spinner } from '../components/widgets'
import { useFetch } from '../hooks/useFetch'
import '../style/notify.css'

const IMPACT_OPTIONS = [
  { value: 'High', label: 'High only' },
  { value: 'Medium', label: 'Medium and above' },
  { value: 'Low', label: 'Everything' },
]

const CHANNEL_OPTIONS = [
  { value: 'telegram', label: 'Telegram' },
  { value: 'discord', label: 'Discord' },
]

/** One account in the editor: the stored one plus what the user is typing. */
interface Draft {
  key: string
  id: string
  channel: NotifyChannel
  name: string
  enabled: boolean
  chatId: string
  secret: string  // a new secret ("" = keep the stored one)
  clear: boolean  // remove the stored secret
  stored: boolean // a secret is stored
  source: 'file' | 'env'
}

function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function draftsOf(accounts: { id: string; channel: NotifyChannel; name: string; enabled: boolean; chat_id: string; secret_set: boolean; source: 'file' | 'env' }[]): Draft[] {
  return accounts.map((a) => ({
    key: a.id,
    id: a.id,
    channel: a.channel,
    name: a.name,
    enabled: a.enabled,
    chatId: a.chat_id,
    secret: '',
    clear: false,
    stored: a.secret_set,
    source: a.source,
  }))
}

function AccountCard({
  draft,
  onChange,
  onRemove,
  onTest,
  testing,
}: {
  draft: Draft
  onChange: (next: Draft) => void
  onRemove: () => void
  onTest: () => void
  testing: boolean
}) {
  const isTelegram = draft.channel === 'telegram'
  if (draft.source === 'env') {
    // The .env fallback: read-only here (it is not written to notify.yaml).
    return (
      <div className="notify-account">
        <header className="notify-account-head">
          <span className={`notify-account-type ${draft.channel}`}>{isTelegram ? 'Telegram' : 'Discord'}</span>
          <span className="notify-account-name">{draft.name}</span>
          <span className="notify-account-env">from .env</span>
          <span className="notify-account-env">{draft.enabled ? 'enabled' : 'disabled'}</span>
          <UiButton tone="ghost" size="xs" onClick={onTest} disabled={testing}>
            {testing ? 'Sending…' : 'Test'}
          </UiButton>
        </header>
      </div>
    )
  }
  return (
    <div className="notify-account">
      <header className="notify-account-head">
        <span className={`notify-account-type ${draft.channel}`}>{isTelegram ? 'Telegram' : 'Discord'}</span>
        <input
          className="notify-account-name"
          aria-label="Account name"
          value={draft.name}
          onChange={(e) => onChange({ ...draft, name: e.target.value })}
        />
        <label className="notify-check">
          <input type="checkbox" checked={draft.enabled} onChange={(e) => onChange({ ...draft, enabled: e.target.checked })} />
          <span>Enabled</span>
        </label>
        <UiButton tone="ghost" size="xs" onClick={onTest} disabled={testing}>
          {testing ? 'Sending…' : 'Test'}
        </UiButton>
        <UiButton tone="ghost" size="xs" onClick={onRemove} title="Remove this account">
          Remove
        </UiButton>
      </header>

      <div className="notify-account-body">
        <div className="notify-field">
          <label className="notify-label" htmlFor={`secret-${draft.key}`}>
            {isTelegram ? 'Bot token' : 'Webhook URL'}
          </label>
          <div className="notify-secret">
            <input
              id={`secret-${draft.key}`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={draft.secret}
              placeholder={draft.clear ? '(cleared — save to apply)' : draft.stored ? '(saved — type to replace)' : isTelegram ? '123456:ABC-DEF…' : 'https://discord.com/api/webhooks/…'}
              onChange={(e) => onChange({ ...draft, secret: e.target.value, clear: false })}
            />
            {draft.stored || draft.clear ? (
              <UiButton
                tone="ghost"
                size="xs"
                onClick={() => onChange({ ...draft, secret: '', clear: !draft.clear })}
              >
                {draft.clear ? 'Keep' : 'Clear'}
              </UiButton>
            ) : null}
          </div>
          <p className="notify-hint">
            {isTelegram
              ? 'From @BotFather. Stored in backend/notify.yaml, never shown again.'
              : 'Server settings → Integrations → Webhooks. Stored in backend/notify.yaml, never shown again.'}
          </p>
        </div>

        {isTelegram ? (
          <div className="notify-field">
            <label className="notify-label" htmlFor={`chat-${draft.key}`}>
              Chat ID
            </label>
            <input
              id={`chat-${draft.key}`}
              type="text"
              inputMode="numeric"
              value={draft.chatId}
              placeholder="-1001234567890"
              onChange={(e) => onChange({ ...draft, chatId: e.target.value.trim() })}
            />
            <p className="notify-hint">A private chat is usually a positive number, a group a negative one.</p>
          </div>
        ) : null}
      </div>
    </div>
  )
}

/** The editable form. Remounted when new settings arrive (key = the fetch timestamp). */
function NotifyForm({
  initial,
  onSaved,
}: {
  initial: { rules: NotifyRules; accounts: Parameters<typeof draftsOf>[0]; file: string }
  onSaved: () => void
}) {
  const [rules, setRules] = useState<NotifyRules>(initial.rules)
  const [accounts, setAccounts] = useState<Draft[]>(draftsOf(initial.accounts))
  const [newChannel, setNewChannel] = useState<NotifyChannel>('telegram')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const patchRule = (values: Partial<NotifyRules>) => setRules((r) => ({ ...r, ...values }))

  const addAccount = () => {
    const name = `account ${accounts.length + 1}`
    setAccounts((prev) => [
      ...prev,
      {
        key: `new:${newChannel}:${name}:${prev.length}`,
        id: `new:${newChannel}:${name}:${prev.length}`,
        channel: newChannel,
        name,
        enabled: true,
        chatId: '',
        secret: '',
        clear: false,
        stored: false,
        source: 'file',
      },
    ])
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    setNotice(null)
    const payload: NotifyAccountIn[] = accounts.map((a) => ({
      id: a.id,
      channel: a.channel,
      name: a.name.trim() || 'main',
      enabled: a.enabled,
      chat_id: a.channel === 'telegram' ? a.chatId : '',
      source: a.source,
      ...(a.channel === 'telegram'
        ? { bot_token: a.secret !== '' ? a.secret : a.clear ? '' : null }
        : { webhook_url: a.secret !== '' ? a.secret : a.clear ? '' : null }),
    }))
    const body: NotifySettingsIn = { rules, accounts: payload }
    try {
      await api.saveNotifySettings(body)
      setNotice(`Saved to ${initial.file || 'backend/notify.yaml'}.`)
      onSaved()
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const test = async (channel?: NotifyChannel, name?: string) => {
    setTesting(channel ? `${channel}:${name ?? ''}` : 'all')
    setError(null)
    setNotice(null)
    try {
      const res = await api.notifyTest(channel, name)
      const ok = Object.entries(res.accounts)
        .filter(([, sent]) => sent)
        .map(([id]) => id)
      const failed = Object.entries(res.accounts)
        .filter(([, sent]) => !sent)
        .map(([id]) => id)
      setNotice(
        [
          ok.length ? `Sent to ${ok.join(', ')}.` : '',
          failed.length ? `Failed: ${failed.join(', ')}.` : '',
        ]
          .filter(Boolean)
          .join(' '),
      )
    } catch (err) {
      setError(errMessage(err))
    } finally {
      setTesting(null)
    }
  }

  return (
    <>
      <section className="notify-panel">
        <header className="notify-panel-head">
          <h2>Accounts</h2>
          <p>
            Every enabled account gets every message. The accounts live in <span className="mono">backend/notify.yaml</span>{' '}
            (same shape as llm.yaml).
          </p>
        </header>
        <div className="notify-panel-body">
          {accounts.length === 0 ? <p className="notify-hint">No account yet — add one below.</p> : null}
          {accounts.map((draft) => (
            <AccountCard
              key={draft.key}
              draft={draft}
              testing={testing === `${draft.channel}:${draft.name}`}
              onChange={(next) => setAccounts((prev) => prev.map((a) => (a.key === draft.key ? next : a)))}
              onRemove={() => setAccounts((prev) => prev.filter((a) => a.key !== draft.key))}
              onTest={() => void test(draft.channel, draft.name)}
            />
          ))}
          <div className="notify-add">
            <UiSelect
              value={newChannel}
              options={CHANNEL_OPTIONS}
              ariaLabel="Channel type of the new account"
              size="sm"
              onChange={(v) => setNewChannel(v as NotifyChannel)}
            />
            <UiButton tone="ghost" size="sm" onClick={addAccount}>
              Add account
            </UiButton>
          </div>
        </div>
      </section>

      <section className="notify-panel">
        <header className="notify-panel-head">
          <h2>When to notify</h2>
          <p>The rules that decide what is worth an alert.</p>
        </header>
        <div className="notify-panel-body">
          <div className="notify-field">
            <span className="notify-label">Impact threshold</span>
            <UiSelect
              value={rules.min_impact}
              options={IMPACT_OPTIONS}
              ariaLabel="Impact threshold"
              size="md"
              onChange={(v) => patchRule({ min_impact: v as Impact })}
            />
            <p className="notify-hint">A new headline at or above this impact is announced as soon as it arrives.</p>
          </div>

          <div className="notify-field">
            <label className="notify-label" htmlFor="notify-lead">
              Reminder before a release (minutes)
            </label>
            <input
              id="notify-lead"
              type="number"
              min={0}
              max={1440}
              value={rules.pre_event_lead_minutes}
              onChange={(e) => patchRule({ pre_event_lead_minutes: Math.max(0, Number(e.target.value) || 0) })}
            />
            <p className="notify-hint">
              A calendar release at or above the threshold is announced this many minutes before it comes out. 0 turns
              the reminder off.
            </p>
          </div>

          <div className="notify-field">
            <label className="notify-check">
              <input
                type="checkbox"
                checked={rules.notify_analyses}
                onChange={(e) => patchRule({ notify_analyses: e.target.checked })}
              />
              <span>Send trade ideas from the LLM</span>
            </label>
          </div>
          <div className="notify-field">
            <label className="notify-check">
              <input
                type="checkbox"
                checked={rules.notify_pre_event}
                onChange={(e) => patchRule({ notify_pre_event: e.target.checked })}
              />
              <span>Include the pre-event scenarios</span>
            </label>
            <p className="notify-hint">The what-if analysis (actual above / below forecast) sent with a pre-event trade idea.</p>
          </div>
          <div className="notify-field">
            <label className="notify-label" htmlFor="notify-confidence">
              Minimum confidence (%)
            </label>
            <input
              id="notify-confidence"
              type="number"
              min={0}
              max={100}
              value={rules.min_confidence}
              onChange={(e) => patchRule({ min_confidence: Math.max(0, Math.min(100, Number(e.target.value) || 0)) })}
            />
            <p className="notify-hint">Headline trade ideas below this confidence are not sent.</p>
          </div>
        </div>
      </section>

      <section className="notify-panel">
        <header className="notify-panel-head">
          <h2>Actions</h2>
          <p>Save the settings, or send a test message to check the accounts.</p>
        </header>
        <div className="notify-panel-body">
          {error ? <p className="notify-error">{error}</p> : null}
          {notice ? <p className="notify-ok">{notice}</p> : null}
          <div className="notify-actions">
            <UiButton tone="ghost" size="sm" onClick={() => void test()} disabled={testing !== null || saving}>
              {testing === 'all' ? 'Sending…' : 'Test all accounts'}
            </UiButton>
            <UiButton tone="primary" size="sm" onClick={() => void save()} disabled={saving || testing !== null}>
              {saving ? 'Saving…' : 'Save settings'}
            </UiButton>
          </div>
        </div>
      </section>
    </>
  )
}

export function NotificationsPage() {
  const q = useFetch('notify-settings', (s) => api.notifySettings(s))
  if (q.error && !q.data) return <ErrorNote error={q.error} onRetry={q.reload} />
  if (!q.data) return <Spinner label="Loading notification settings" />
  return (
    <div className="notify-page">
      <div className="notify-wrap">
        <NotifyForm key={q.updatedAt ?? 'once'} initial={q.data} onSaved={q.reload} />
      </div>
    </div>
  )
}
