import { useCallback, useEffect, useRef, useState } from 'react'
import { ACTIVE_RUN_STATUSES, api, type RecommendationDetail, type RunDetail as Run, type Step } from '../api'
import { DecisionCard } from '../components/DecisionCard'
import { PurchaseOrders } from '../components/PurchaseOrders'
import { StatusChip } from '../components/StatusChip'
import { Timeline } from '../components/Timeline'

const POLL_MS = 1500

export function RunDetail({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null)
  const [steps, setSteps] = useState<Step[]>([])
  const [rec, setRec] = useState<RecommendationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lastSeq = useRef(0)

  // Fetch the run and only the steps we have not seen yet.
  const refresh = useCallback(async () => {
    try {
      const next = await api.run(id, lastSeq.current)
      if (next.steps.length) {
        lastSeq.current = next.steps[next.steps.length - 1].seq
        setSteps((prev) => [...prev, ...next.steps])
      }
      setRun(next)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [id])

  useEffect(() => {
    lastSeq.current = 0
    setSteps([])
    setRun(null)
    refresh()
  }, [id, refresh])

  useEffect(() => {
    if (run && !rec) api.recommendation(run.recommendation_id).then(setRec, () => {})
  }, [run, rec])

  const working = !run || ACTIVE_RUN_STATUSES.includes(run.status)
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
          {run && <DecisionCard run={run} recommended={rec?.quantity ?? 0} />}
          {run && <PurchaseOrders pos={run.purchase_orders} />}
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
