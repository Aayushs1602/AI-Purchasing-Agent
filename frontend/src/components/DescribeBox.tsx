import { useState } from 'react'
import { api, type Problem, type ScenarioValues } from '../api'

interface Props {
  values: ScenarioValues
  disabledReason: string | null
  onApply: (input: ScenarioValues, filled: string[]) => void
}

// Describe a situation in plain words; one model call fills in the form below for review.
export function DescribeBox({ values, disabledReason, onApply }: Props) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ filled: string[]; problems: Problem[] } | null>(null)

  async function fill() {
    setBusy(true)
    setError(null)
    try {
      const draft = await api.draftScenario(text, values)
      onApply(draft.input, draft.filled)
      setResult({ filled: draft.filled, problems: draft.problems })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card">
      <header>
        <h2>Describe it in words</h2>
        <span className="muted small">optional · one model call</span>
      </header>
      <div className="body">
        <textarea
          rows={3}
          maxLength={2000}
          value={text}
          placeholder="e.g. Only 120 colas left. A 320-unit PO was due two days ago and the supplier says it is on hold. $1,500 of budget left."
          onChange={(e) => setText(e.target.value)}
          aria-label="Describe the situation"
        />
        <div className="actions">
          <button
            className="primary"
            disabled={busy || text.trim().length < 5 || disabledReason !== null}
            onClick={fill}
          >
            {busy ? 'Filling the form...' : 'Fill the form'}
          </button>
          <span className="muted small">
            {disabledReason ??
              'Changes only what you mention. Review the fields before creating the scenario.'}
          </span>
        </div>
        {error && <div className="notice error">{error}</div>}
        {result && (
          <div className="small">
            {result.filled.length === 0 ? (
              <span className="muted">Nothing in the description changed the form.</span>
            ) : (
              <>
                <span className="muted">Filled: </span>
                {result.filled.map((field) => (
                  <span key={field} className="chip accent">
                    {field}
                  </span>
                ))}
              </>
            )}
            {result.problems.length > 0 && (
              <ul className="muted">
                {result.problems.map((p) => (
                  <li key={p.field + p.message}>
                    <span className="mono">{p.field}</span>: {p.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
