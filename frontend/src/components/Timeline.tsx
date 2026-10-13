import type { Json, Step } from '../api'
import { StatusChip } from './StatusChip'

type Any = Record<string, any> // tool payloads are free-form JSON from the backend

const money = (n: unknown) =>
  typeof n === 'number' ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '-'

function Codes({ items, tone }: { items?: { code: string; message?: string }[]; tone: string }) {
  if (!items?.length) return null
  return (
    <>
      {items.map((f) => (
        <span key={f.code} className={`chip ${tone}`} title={f.message}>
          {f.code}
        </span>
      ))}
    </>
  )
}

function Untrusted({ note }: { note?: Any | null }) {
  if (!note?.untrusted_text) return null
  return (
    <blockquote className="untrusted">
      <span className="chip warn">untrusted text · {note.source}</span> {note.untrusted_text}
    </blockquote>
  )
}

function OutcomeCheck({ check }: { check?: Any }) {
  if (!check) return null
  const ok = check.status === 'AS_EXPECTED'
  return (
    <div className={`outcome ${ok ? 'ok' : 'unexpected'}`}>
      <strong>{ok ? 'Outcome as expected' : 'Unexpected outcome'}</strong>
      {!ok && (
        <ul>
          {check.differences?.map((d: Any, i: number) => (
            <li key={i}>{d.message}</li>
          ))}
          {check.constraint_violations?.length > 0 && (
            <li>Now breaks: {check.constraint_violations.join(', ')}</li>
          )}
          {check.needs_human_approval?.length > 0 && (
            <li>Needs a human: {check.needs_human_approval.join(', ')}</li>
          )}
          {check.next_actions?.length > 0 && (
            <li className="muted">Allowed next: {check.next_actions.join(' · ')}</li>
          )}
        </ul>
      )}
    </div>
  )
}

function ReadSummary({ name, data }: { name: string; data: Any }) {
  switch (name) {
    case 'get_recommendation':
      return (
        <span>
          {data.recommended_quantity} units recommended by {data.source}, {data.age_days} day(s)
          old
        </span>
      )
    case 'get_inventory':
      return (
        <span>
          {data.on_hand} on hand · safety stock {data.safety_stock} · counted{' '}
          {data.days_since_count} day(s) ago
        </span>
      )
    case 'get_storage_capacity':
      return (
        <span>
          capacity {data.storage_capacity_units} · {data.free_space_today} free today
        </span>
      )
    case 'get_demand_forecast':
      return (
        <span>
          {data.avg_per_day_coverage_window}/day over the {data.coverage_window_days}-day
          coverage window
        </span>
      )
    case 'get_sales_history':
      return (
        <span>
          last 14 days {data.avg_last_14}/day vs forecast {data.forecast_avg_per_day}/day (
          {data.last_14_vs_forecast_pct}%)
        </span>
      )
    case 'get_open_purchase_orders':
      return (
        <div>
          {data.open_purchase_orders.length} open PO(s), {data.total_outstanding} units
          outstanding
          {data.open_purchase_orders.map((po: Any) => (
            <div key={po.po_id}>
              <span className="mono">{po.po_id}</span> {po.outstanding_quantity} units, ETA{' '}
              {po.eta} {po.overdue && <span className="chip bad">overdue</span>}
              <Untrusted note={po.note} />
            </div>
          ))}
        </div>
      )
    case 'get_supplier_terms':
      return (
        <div>
          {data.supplier_name}: lead {data.lead_time_days}d · MOQ {data.moq} · case{' '}
          {data.case_pack} · {money(data.unit_cost)}/unit · fill rate {data.fill_rate}
          <Untrusted note={data.supplier_note} />
        </div>
      )
    case 'get_budget':
      return (
        <span>
          {data.category} {data.period}: {money(data.available)} available of{' '}
          {money(data.total)}
        </span>
      )
    case 'check_recommendation_inputs':
      return data.differences.length ? (
        <Codes items={data.differences} tone="accent" />
      ) : (
        <span>inputs unchanged since the recommendation</span>
      )
    default:
      return null
  }
}

function ToolResult({ name, args, result }: { name: string; args: Any; result: Any }) {
  if (!result.ok) {
    const details = result.error.details ?? {}
    return (
      <div className="result error">
        <strong>{result.error.code}</strong> {result.error.message}
        {details.problems && (
          <ul>
            {details.problems.map((p: string, i: number) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        )}
        <Codes items={details.hard_violations} tone="bad" />
      </div>
    )
  }
  const data: Any = result.data
  if (name === 'evaluate_order_quantity') {
    const p = data.projection
    return (
      <div className="result">
        <span className={`chip ${data.feasible ? 'ok' : 'bad'}`}>
          {data.feasible ? 'feasible' : 'infeasible'}
        </span>{' '}
        {args.quantity} units ({data.evaluated_as}) · need {p.net_need} · {money(p.order_value)} ·
        peak {p.peak_on_hand_after_arrival} · {p.cover_days_after_receipt} days cover{' '}
        {data.requires_approval && <span className="chip warn">needs approval</span>}{' '}
        <Codes items={data.hard_violations} tone="bad" />{' '}
        <Codes items={data.soft_warnings} tone="warn" />{' '}
        <Codes items={data.data_quality_flags} tone="warn" />
      </div>
    )
  }
  if (data.purchase_order) {
    const po = data.purchase_order
    return (
      <div className="result">
        <span className="mono">{po.po_id}</span> <StatusChip value={po.status} />{' '}
        {data.outcome && <span className="chip accent">{data.outcome}</span>}{' '}
        {po.outstanding_quantity} units · {money(po.value)}
        {data.approval && (
          <div className="muted">
            {data.approval.approved ? 'Approved' : 'Declined'} by {data.approval.by}
            {data.approval.comment ? `: "${data.approval.comment}"` : ''}
          </div>
        )}
        {data.supplier_ack && <Untrusted note={data.supplier_ack.message} />}
        <OutcomeCheck check={data.outcome_check} />
        {data.circuit_breaker?.tripped && (
          <div className="result error">
            Circuit breaker tripped: {data.circuit_breaker.message} (
            {data.circuit_breaker.escalation_id})
          </div>
        )}
      </div>
    )
  }
  if (name === 'escalate_to_human') return <div className="result">Opened {data.escalation_id}</div>
  if (name === 'finalize_decision')
    return (
      <div className="result">
        Decision recorded: <StatusChip value={data.decision} />
        {data.flagged_for_review && <span className="chip bad">flagged for review</span>}
      </div>
    )
  return (
    <div className="result">
      <ReadSummary name={name} data={data} />
    </div>
  )
}

function argsLabel(args: Any): string {
  const shown = Object.entries(args).filter(([k]) => !['expectation', 'rationale', 'key_factors'].includes(k))
  return shown.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')
}

const SYSTEM_LABELS: Record<string, string> = {
  kickoff: 'Run started',
  paused_for_approval: 'Paused: waiting for a human to approve the PO',
  resumed: 'Resumed after the human decision',
  settled: 'Run settled',
  degraded_mode: 'Degraded mode: rules-only proposal handed to a human',
  llm_error: 'The LLM failed',
}

function StepView({ step }: { step: Step }) {
  const payload = step.payload as Any
  let body
  if (step.kind === 'llm') {
    const calls = (payload.tool_calls as Any[]).map((c) => c.name)
    body = (
      <>
        <div className="step-title">
          <span className="kind llm">model</span>
          {calls.length ? `calls ${calls.join(', ')}` : 'replies without calling a tool'}
          <span className="meta">
            {step.duration_ms} ms · {payload.usage?.prompt_tokens ?? 0} tokens in
          </span>
        </div>
        {payload.text && <p className="model-text">{payload.text}</p>}
      </>
    )
  } else if (step.kind === 'tool') {
    body = (
      <>
        <div className="step-title">
          <span className="kind tool">tool</span>
          <span className="mono">{step.name}</span>
          <span className="args mono">{argsLabel(payload.args ?? {})}</span>
        </div>
        <ToolResult name={step.name ?? ''} args={payload.args ?? {}} result={payload.result} />
      </>
    )
  } else {
    const label = step.kind === 'guardrail' ? `Guardrail: ${step.name}` : SYSTEM_LABELS[step.name ?? ''] ?? step.name
    body = (
      <div className="step-title">
        <span className={`kind ${step.kind}`}>{step.kind}</span>
        {label}
        {step.name === 'kickoff' && <span className="muted"> · {String(payload.message)}</span>}
        {step.name === 'paused_for_approval' && (
          <span className="mono"> {String(payload.po_id)} ({(payload.reasons as string[]).join(', ')})</span>
        )}
        {step.name === 'resumed' && (
          <span className="muted">
            {' '}
            · {payload.approved ? 'approved' : 'declined'} by {String(payload.approver)}
          </span>
        )}
        {step.name === 'settled' && (
          <span>
            {' '}
            <StatusChip value={String(payload.status)} />
          </span>
        )}
        {step.name === 'llm_error' && <span className="muted"> · {String(payload.error)}</span>}
      </div>
    )
  }
  return (
    <li className={`step ${step.kind} ${step.kind === 'tool' ? 'indent' : ''}`}>
      <span className="seq mono">{step.seq}</span>
      <div className="step-body">
        {body}
        <details>
          <summary>raw</summary>
          <pre>{JSON.stringify(step.payload as Json, null, 2)}</pre>
        </details>
      </div>
    </li>
  )
}

export function Timeline({ steps }: { steps: Step[] }) {
  if (!steps.length) return <p className="muted">Waiting for the first step...</p>
  return (
    <ol className="timeline">
      {steps.map((step) => (
        <StepView key={step.seq} step={step} />
      ))}
    </ol>
  )
}
