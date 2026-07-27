import client, { API_BASE } from './client'

export interface PlanStep {
  id: string
  description: string
  tool?: string | null
  depends_on?: string[]
  status: 'pending' | 'running' | 'completed' | 'failed'
  output?: string | null
}

export interface PlanPreview {
  plan_id: string
  session_id: string
  goal: string
  steps: PlanStep[]
  status: string
}

export const sessionsApi = {
  list: (agentId?: string) =>
    client.get('/sessions', { params: { agent_id: agentId } }).then((r) => r.data),
  create: (agentId: string, mode = 'chat') =>
    client.post('/sessions', { agent_id: agentId, mode }).then((r) => r.data),
  get: (id: string) => client.get(`/sessions/${id}`).then((r) => r.data),
  delete: (id: string) => client.delete(`/sessions/${id}`),
  fork: (id: string, data: object) => client.post(`/sessions/${id}/fork`, data).then((r) => r.data),
  listRunners: (id: string) => client.get(`/sessions/${id}/runners`).then((r) => r.data),
  listSpans: (sessionId: string, runnerId: string) =>
    client.get(`/sessions/${sessionId}/runners/${runnerId}/spans`).then((r) => r.data),

  generatePlan: (sessionId: string, goal: string) =>
    client.post<PlanPreview>(`/sessions/${sessionId}/plan/generate`, { goal }).then((r) => r.data),
  getPlan: (sessionId: string, planId: string) =>
    client.get<PlanPreview>(`/sessions/${sessionId}/plan/${planId}`).then((r) => r.data),
  updatePlan: (sessionId: string, planId: string, steps: PlanStep[]) =>
    client.put<PlanPreview>(`/sessions/${sessionId}/plan/${planId}`, { steps }).then((r) => r.data),
  revisePlan: (sessionId: string, planId: string, instruction: string) =>
    client.post<PlanPreview>(`/sessions/${sessionId}/plan/${planId}/revise`, { instruction }).then((r) => r.data),
  executePlan: (sessionId: string, planId: string) =>
    client.post<{ task_id: string; plan_id: string }>(`/sessions/${sessionId}/plan/${planId}/execute`).then((r) => r.data),
}

export function chatStream(
  sessionId: string,
  message: string,
  onChunk: (text: string) => void,
  onDone?: () => void,
  onError?: (err: string) => void,
): () => void {
  const controller = new AbortController()
  const token = localStorage.getItem('access_token')

  ;(async () => {
    try {
      const res = await fetch(`${API_BASE}/sessions/${sessionId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      })

      if (!res.ok) {
        onError?.(`HTTP ${res.status}`)
        return
      }

      const reader = res.body!.getReader()
      // { stream: true } 让 decoder 缓冲跨 chunk 的多字节字符（中文等）
      const decoder = new TextDecoder('utf-8', { fatal: false })
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })

        // 按 SSE 分隔符 \n\n 切割完整事件
        const parts = buf.split('\n\n')
        // 最后一段可能不完整，留给下一次 read
        buf = parts.pop() ?? ''

        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data: ')) continue
            const raw = line.slice(6).trim()
            if (raw === '[DONE]') { onDone?.(); return }
            try {
              const event = JSON.parse(raw)
              if (event.type === 'stream_chunk' && event.content) onChunk(event.content)
              else if (event.type === 'done') onDone?.()
              else if (event.type === 'error') onError?.(event.error)
            } catch {
              // 忽略无法解析的行
            }
          }
        }
      }

      // flush decoder 剩余字节
      buf += decoder.decode()
      onDone?.()
    } catch (e: any) {
      if (e?.name !== 'AbortError') onError?.(e?.message ?? 'Unknown error')
    }
  })()

  return () => controller.abort()
}
