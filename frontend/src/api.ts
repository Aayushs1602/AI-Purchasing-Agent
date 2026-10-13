// Typed client for the FastAPI backend. In development Vite proxies /api to :8000.

export type Json = Record<string, unknown>

export interface RunSummary {
  id: string
  recommendation_id: string
  status: string
  provider: string
  model: string
  decision: string | null
  final_quantity: number | null
  confidence: number | null
  llm_calls: number
  prompt_tokens: number
  output_tokens: number
  started_at: string
  finished_at: string | null
}

export interface Recommendation {
  id: string
  scenario: string
  title: string
  sku: string
  product_name: string
  category: string
  node_id: string
  supplier_id: string
  supplier_name: string
  quantity: number
  created_on: string
  status: string
  latest_run: RunSummary | null
}

export interface RecommendationDetail extends Recommendation {
  source: string
  method: string
  inputs: Json
}

export interface Finding {
  code: string
  severity?: string
  message: string
}

export interface PurchaseOrder {
  id: string
  sku: string
  status: string
  quantity: number
  unit_cost: number
  confirmed_quantity: number | null
  confirmed_unit_cost: number | null
  outstanding_quantity: number
  value: number
  eta: string | null
  revision: number
  supplier_message: string | null
  approval_status: string | null
  approval_reasons: Finding[] | null
  approval_by: string | null
  approval_comment: string | null
}

export interface Step {
  seq: number
  kind: 'llm' | 'tool' | 'system' | 'guardrail'
  name: string | null
  payload: Json
  duration_ms: number | null
  created_at: string
}

export interface Escalation {
  id: string
  run_id: string
  recommendation_id: string
  severity: string
  reason: string
  questions: string[]
  options: string[]
  status: string
  resolved_by: string | null
  resolution: string | null
}

export interface KeyFactor {
  factor: string
  value: number | string
  impact: string
}

export interface RunDetail extends RunSummary {
  rationale: string | null
  key_factors: KeyFactor[] | null
  risks: string[] | null
  evidence: string[]
  error: string | null
  validation_issues: string[] | null
  fallback_proposal: Json | null
  paused_for: { po_id: string; reasons: Finding[] } | null
  purchase_orders: PurchaseOrder[]
  escalations: Escalation[]
  steps: Step[]
}

export interface Approval {
  purchase_order: PurchaseOrder
  product_name: string
  reasons: Finding[]
  run_id: string | null
  run_status: string | null
}

export interface Health {
  status: string
  agent: 'ready' | 'disabled'
  agent_error: string | null
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (Array.isArray(body.detail)) message = body.detail.map((d: Json) => d.msg).join('; ')
    } catch {
      // keep the status line
    }
    throw new ApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  health: () => request<Health>('/api/health'),
  recommendations: () => request<Recommendation[]>('/api/recommendations'),
  recommendation: (id: string) => request<RecommendationDetail>(`/api/recommendations/${id}`),
  startRun: (recommendationId: string) =>
    post<{ run_id: string }>(`/api/recommendations/${recommendationId}/runs`),
  run: (id: string, stepsAfter = 0) =>
    request<RunDetail>(`/api/runs/${id}?steps_after=${stepsAfter}`),
  approvals: () => request<Approval[]>('/api/approvals'),
  approve: (poId: string, approver: string, comment: string) =>
    post(`/api/purchase-orders/${poId}/approve`, { approver, comment: comment || null }),
  decline: (poId: string, approver: string, comment: string) =>
    post(`/api/purchase-orders/${poId}/decline`, { approver, comment }),
  resolveEscalation: (id: string, resolvedBy: string, resolution: string) =>
    post(`/api/escalations/${id}/resolve`, { resolved_by: resolvedBy, resolution }),
  reset: () => post<{ scenarios: number }>('/api/admin/reset'),
}

export const ACTIVE_RUN_STATUSES = ['queued', 'running']
