import { useState } from 'react'
import { api, type Escalation } from '../api'
import { StatusChip } from './StatusChip'

function ResolveForm({ escalation, onResolved }: { escalation: Escalation; onResolved: () => void }) {
  const [by, setBy] = useState('buyer:demo')
  const [resolution, setResolution] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function resolve() {
    try {
      await api.resolveEscalation(escalation.id, by, resolution)
      onResolved()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="resolve">
      <label htmlFor={`res-${escalation.id}`}>How was it resolved?</label>
      <textarea
        id={`res-${escalation.id}`}
        rows={2}
        value={resolution}
        onChange={(e) => setResolution(e.target.value)}
      />
      <div className="actions">
        <input
          aria-label="Resolved by"
          value={by}
          onChange={(e) => setBy(e.target.value)}
          style={{ maxWidth: 160 }}
        />
        <button disabled={resolution.trim().length < 3} onClick={resolve}>
          Mark resolved
        </button>
      </div>
      {error && <div className="notice error">{error}</div>}
    </div>
  )
}

export function Escalations({ items, onChanged }: { items: Escalation[]; onChanged: () => void }) {
  if (!items.length) return null
  return (
    <section className="card">
      <header>
        <h2>Escalations</h2>
      </header>
      <div className="body">
        {items.map((e) => (
          <div key={e.id} className="escalation">
            <div>
              <span className="mono">{e.id}</span> <StatusChip value={e.status} />{' '}
              <span className={`chip ${e.severity === 'high' ? 'bad' : 'warn'}`}>{e.severity}</span>
            </div>
            <p>{e.reason}</p>
            <ul>
              {e.questions.map((q, i) => (
                <li key={i}>{q}</li>
              ))}
            </ul>
            {e.options.length > 0 && (
              <p className="muted">Options: {e.options.join(' · ')}</p>
            )}
            {e.status === 'open' ? (
              <ResolveForm escalation={e} onResolved={onChanged} />
            ) : (
              <p className="muted">
                Resolved by {e.resolved_by}: {e.resolution}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
