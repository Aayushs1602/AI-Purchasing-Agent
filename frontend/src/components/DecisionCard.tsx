import type { ReviewerVerdict, RunDetail } from '../api'
import { StatusChip } from './StatusChip'

// The answer the person gave before the run (hidden from the agent), next to the agent's.
function YourCallLine({ verdict }: { verdict: ReviewerVerdict }) {
  const range =
    verdict.quantity_min !== null
      ? verdict.quantity_min === verdict.quantity_max
        ? ` ${verdict.quantity_min}`
        : ` ${verdict.quantity_min}-${verdict.quantity_max}`
      : ''
  return (
    <div className="your-call">
      <span className="muted">Your call:</span>{' '}
      <strong>
        {verdict.decision}
        {range}
      </strong>{' '}
      {verdict.agrees === null ? (
        <span className="chip">agent not decided yet</span>
      ) : verdict.agrees ? (
        <span className="chip ok">agent agrees</span>
      ) : (
        <span className="chip bad">agent disagrees</span>
      )}
      {verdict.note && <span className="muted small"> · {verdict.note}</span>}
    </div>
  )
}

export function DecisionCard({ run, recommended }: { run: RunDetail; recommended: number }) {
  const proposal = run.fallback_proposal as Record<string, any> | null
  return (
    <section className="card">
      <header>
        <h2>Decision</h2>
        <StatusChip value={run.status} />
      </header>
      <div className="body decision">
        {run.decision ? (
          <>
            <div className="verdict">
              <StatusChip value={run.decision} />
              <span className="num">
                <strong>{run.final_quantity}</strong> units in place
                <span className="muted"> · recommended {recommended}</span>
              </span>
              {run.confidence !== null && (
                <span className="muted">confidence {Math.round(run.confidence * 100)}%</span>
              )}
            </div>
            {run.rationale && <p>{run.rationale}</p>}
            {run.key_factors && run.key_factors.length > 0 && (
              <dl className="factors">
                {run.key_factors.map((f, i) => (
                  <div key={i}>
                    <dt>
                      <span className="mono">{f.factor}</span>{' '}
                      <span className="num">{String(f.value)}</span>
                    </dt>
                    <dd>{f.impact}</dd>
                  </div>
                ))}
              </dl>
            )}
            {run.risks && run.risks.length > 0 && (
              <ul className="risks">
                {run.risks.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="muted">
            {run.status === 'awaiting_approval'
              ? 'The agent is waiting for a human decision on its PO.'
              : run.status === 'queued' || run.status === 'running'
                ? 'The agent is working...'
                : 'No decision recorded.'}
          </p>
        )}
        {run.reviewer_call && <YourCallLine verdict={run.reviewer_call} />}
        {run.validation_issues && run.validation_issues.length > 0 && (
          <div className="notice error">
            <strong>Needs review.</strong> Finalize checks still found:
            <ul>
              {run.validation_issues.map((issue, i) => (
                <li key={i}>{issue}</li>
              ))}
            </ul>
          </div>
        )}
        {proposal && (
          <div className="notice info">
            <strong>Rules-only proposal for a human:</strong> {proposal.decision}{' '}
            {proposal.quantity} units ({proposal.escalation_id}). Nothing was ordered on it.
          </div>
        )}
        {run.error && !proposal && <div className="notice error">{run.error}</div>}
        <p className="meta muted mono">
          {run.provider}/{run.model} · {run.llm_calls} model calls ·{' '}
          {run.prompt_tokens.toLocaleString()} / {run.output_tokens.toLocaleString()} tokens ·
          evidence {run.evidence.length}/7
        </p>
      </div>
    </section>
  )
}
