// LLM Lab API (backend /api/llm). Kept apart from client.ts so the shared client stays small.
import { ApiError } from './client'

export interface LlmProvider {
  name: string
  label: string | null
  problem: string | null
  /** Default model of the provider (llm.yaml); empty when it has none. */
  model: string
  /** Default thinking level (llm.yaml); empty = the model's own default. */
  thinking: string
  /** Levels `thinking` accepts: null = any, [] = no thinking option. */
  thinkingChoices: string[] | null
}

export interface LlmModel {
  spec: string
  provider: string
  model: string
  available: boolean
}

export interface LlmModelsOut {
  providers: LlmProvider[]
  models: LlmModel[]
}

export interface LlmMessage {
  role: 'user' | 'assistant'
  text: string
  /** Base64 data URLs (png, jpeg, gif, webp). */
  images?: string[]
}

export interface LlmPresets {
  backtest_system_prompt: string
}

export interface LlmChatRequest {
  model: string
  messages: LlmMessage[]
  system?: string | null
  temperature?: number | null
  max_tokens?: number | null
  /** Thinking level; empty/null = the provider's llm.yaml level. */
  thinking?: string | null
}

export interface LlmChatResult {
  text: string
  model: string
  provider: string
  finish_reason: string | null
  usage: Record<string, unknown>
  reasoning: string | null
  latency_ms: number
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/llm${path}`, {
      method,
      signal,
      headers: { Accept: 'application/json', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err
    throw new ApiError('Cannot reach the backend', 0)
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`.trim()
    try {
      const json = (await res.json()) as { detail?: unknown }
      if (typeof json.detail === 'string') detail = json.detail
      else if (Array.isArray(json.detail)) detail = json.detail.map((d) => (d as { msg?: string }).msg ?? String(d)).join('; ')
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail, res.status)
  }
  return (await res.json()) as T
}

export const llmApi = {
  models: (signal?: AbortSignal) => call<LlmModelsOut>('GET', '/models', undefined, signal),
  presets: (signal?: AbortSignal) => call<LlmPresets>('GET', '/presets', undefined, signal),
  chat: (body: LlmChatRequest, signal?: AbortSignal) => call<LlmChatResult>('POST', '/chat', body, signal),
}