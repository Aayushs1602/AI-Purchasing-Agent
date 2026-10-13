import { useCallback, useEffect, useRef, useState } from 'react'
import { ACTIVE_RUN_STATUSES, api, type RecommendationDetail, type RunDetail as Run, type Step } from '../api'
import { ApprovalPanel } from '../components/ApprovalPanel'
import { DecisionCard } from '../components/DecisionCard'
import { Escalations } from '../components/Escalations'
import { PurchaseOrders } from '../components/PurchaseOrders'
import { StatusChip } from '../components/StatusChip'
import { Timeline } from '../components/Timeline'

const POLL_MS = 1500

export function RunDetail({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null)
  const [steps, setSteps] = useState<Step[]>([])
  const [rec, setRec] = useState<RecommendationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  // After a human decides, keep polling until the resumed run moves on.
  const [resuming, setResuming] = useState(false)
  const lastSeq = useRef(0)
  const currentId = useRef(id)

  // Fetch the run and only the steps we have not seen yet. Polls can overlap (and React's
  // dev mode mounts twice), so steps are merged by seq rather than appended blindly.
  const refresh = useCallback(async () => {
    try {
      const next = await api.run(id, lastSeq.current)
      if (currentId.current !== id) return // the user has moved to another run
      if (next.steps.length) {
        lastSeq.current = Math.max(lastSeq.current, next.steps[next.steps.length - 1].seq)
        setSteps((prev) => {
          const seen = new Set(prev.map((s) => s.seq))
          const fresh = next.steps.filter((s) => !seen.has(s.seq))
          return fresh.length ? [...prev, ...fresh] : prev
        })
      }
      setRun(next)
      if (next.status !== 'awaiting_approval') setResuming(false)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [id])

  useEffect(() => {
    currentId.current = id
    lastSeq.current = 0
    setSteps([])
    setRun(null)
    setRec(null)
    setError(null)
    setResuming(false)
    refresh()
  }, [id, refresh])

  useEffect(() => {
    if (run && !rec) api.recommendation(run.recommendation_id).then(setRec, () => {})
  }, [run, rec])

  const working = !run || resuming || ACTIVE_RUN_STATUSES.includes(run.status)
  const pausedPo = run?.paused_for
    ? run.purchase_orders.find((po) => po.id === run.paused_for?.po_id)
    : undefined
  useEffect(() => {
    if (!working) return
    const timer = setInterval(refresh, POLL_MS)
    return () => clearInterval(timer)
  }, [working, refresh])

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <a href="#/" className="muted">
            ← Recommendations
          </a>
          <h1>
            {rec ? `${rec.product_name}: ${rec.title}` : 'Agent run'}{' '}
            {run && <StatusChip value={run.status} />}
          </h1>
          <p className="mono">
            {id}
            {rec && ` · ${rec.id} · recommended ${rec.quantity} units from ${rec.supplier_name}`}
          </p>
        </div>
      </div>
      {error && <div className="notice error">{error}</div>}

      <div className="run-grid">
        <section className="card">
          <header>
            <h2>Agent trace</h2>
            {working && <span className="chip accent">live</span>}
          </header>
          <div className="body">
            <Timeline steps={steps} />
          </div>
        </section>

        <aside className="side">
          {run && pausedPo && !resuming && (
            <ApprovalPanel
              po={pausedPo}
              reasons={run.paused_for?.reasons ?? []}
              onDecided={() => {
                setResuming(true)
                refresh()
              }}
            />
          )}
          {resuming && <div className="notice info">Decision sent. The agent is resuming...</div>}
          {run && <DecisionCard run={run} recommended={rec?.quantity ?? 0} />}
          {run && <PurchaseOrders pos={run.purchase_orders} />}
          {run && <Escalations items={run.escalations} onChanged={refresh} />}
          {rec && (
            <section className="card">
              <header>
                <h2>Recommendation</h2>
              </header>
              <div className="body">
                <p className="muted">{rec.method}</p>
                <p className="mono">inputs: {JSON.stringify(rec.inputs)}</p>
              </div>
            </section>
          )}
        </aside>
      </div>
    </div>
  )
}
