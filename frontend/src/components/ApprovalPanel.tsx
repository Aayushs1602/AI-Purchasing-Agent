import { useState } from 'react'
import { api, type Finding, type PurchaseOrder } from '../api'

const APPROVER_KEY = 'purchasing-agent.approver'

function savedApprover(): string {
  try {
    return localStorage.getItem(APPROVER_KEY) || 'buyer:demo'
  } catch {
    return 'buyer:demo'
  }
}

interface Props {
  po: PurchaseOrder
  reasons: Finding[]
  onDecided: () => void
}

// Shown while a run is paused: the human decides, the agent then continues from the result.
export function ApprovalPanel({ po, reasons, onDecided }: Props) {
  const [approver, setApprover] = useState(savedApprover)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function decide(approve: boolean) {
    if (!approve && comment.trim().length < 3) {
      setError('Say why you are declining: the agent uses your reason to choose what to do next.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      localStorage.setItem(APPROVER_KEY, approver)
    } catch {
      // storage unavailable: nothing to remember
    }
    try {
      if (approve) await api.approve(po.id, approver, comment)
      else await api.decline(po.id, approver, comment)
      onDecided()
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <section className="card approval">
      <header>
        <h2>Approval needed</h2>
        <span className="chip warn">run paused</span>
      </header>
      <div className="body">
        <p>
          <span className="mono">{po.id}</span>: {po.quantity} units · $
          {po.value.toLocaleString(undefined, { maximumFractionDigits: 2 })}. Policy will not let
          the agent send this on its own:
        </p>
        <ul>
          {reasons.map((r) => (
            <li key={r.code}>
              <span className="mono">{r.code}</span> {r.message}
            </li>
          ))}
        </ul>
        <label htmlFor="approver">Approver</label>
        <input id="approver" value={approver} onChange={(e) => setApprover(e.target.value)} />
        <label htmlFor="approval-comment">Comment (required to decline)</label>
        <textarea
          id="approval-comment"
          rows={2}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
        {error && <div className="notice error">{error}</div>}
        <div className="actions">
          <button className="primary" disabled={busy || approver.length < 2} onClick={() => decide(true)}>
            Approve and send
          </button>
          <button className="danger" disabled={busy || approver.length < 2} onClick={() => decide(false)}>
            Decline
          </button>
        </div>
        <p className="muted small">
          The agent resumes with your decision and the supplier's answer, then finishes its work.
        </p>
      </div>
    </section>
  )
}
