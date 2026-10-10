import client from './client'
import type { AxiosResponse } from 'axios'
import type { AgentEvalCaseDefinition, AgentSummary } from './agents'

export interface EvalSource {
  run_id: string; session_id: string; status: string; input_preview: string; created_at: string
}
export interface EvalComparison {
  baseline_id: string; candidate_id: string; same_candidate: boolean; score_delta: string
  regressed_count: number; improved_count: number
  cases: Array<{
    case_key: string; name: string; change: string; baseline_status: string; candidate_status: string
    baseline_response: string | null; candidate_response: string | null
    candidate_assertions: Array<Record<string, unknown>>
  }>
  tags: Array<{ tag: string; count: number; baseline_passed: number; candidate_passed: number }>
}
export const evalsApi = {
  agents: async (admin: boolean) => {
    const items: AgentSummary[] = []
    let cursor: string | null = null
    do {
      const response: AxiosResponse<{ items: AgentSummary[]; next_cursor: string | null }> = await client.get('/agents', {
        params: { owner: admin ? undefined : 'me', limit: 100, cursor: cursor || undefined },
      })
      items.push(...response.data.items)
      cursor = response.data.next_cursor
    } while (cursor)
    return items
  },
  sources: (agent: string) => client.get<{ items: EvalSource[] }>(`/agents/${agent}/eval-sources`).then(r => r.data.items),
  preview: (agent: string, run: string) => client.get<AgentEvalCaseDefinition>(`/agents/${agent}/eval-sources/${run}`).then(r => r.data),
  compare: (agent: string, baseline: string, candidate: string) => client.get<EvalComparison>(`/agents/${agent}/eval-comparison`, {
    params: { baseline_id: baseline, candidate_id: candidate },
  }).then(r => r.data),
}
