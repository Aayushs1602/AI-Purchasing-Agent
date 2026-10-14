import { useCallback, useEffect, useState } from 'react'
import { ACTIVE_RUN_STATUSES, api, type Recommendation } from '../api'
import { StatusChip } from '../components/StatusChip'
import { go } from '../router'

interface Props {
  agentReady: boolean
  // False in replay mode, which only has recordings of the demo scenarios.
  liveModel: boolean
  onDataChanged: () => void
}

export function Inbox({ agentReady, liveModel, onDataChanged }: Props) {
  const [recs, setRecs] = useState<Recommendation[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState<string | null>(null)
  const [confirmReset, setConfirmReset] = useState(false)

  const load = useCallback(async () => {
    try {
      setRecs(await api.recommendations())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Keep the list fresh while any run is still working.
  const anyActive = recs?.some((r) => ACTIVE_RUN_STATUSES.includes(r.latest_run?.status ?? ''))
  useEffect(() => {
    if (!anyActive) return
    const timer = setInterval(load, 3000)
    return () => clearInterval(timer)
  }, [anyActive, load])

  async function start(rec: Recommendation) {
    setStarting(rec.id)
    try {
      const { run_id } = await api.startRun(rec.id)
      go(`/runs/${run_id}`)
    } catch (e) {
      setError((e as Error).message)
      setStarting(null)
    }
  }

  async function reset() {
    if (!confirmReset) {
      setConfirmReset(true)
      return
    }
    setConfirmReset(false)
    try {
      await api.reset()
      await load()
      onDataChanged()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Purchase recommendations</h1>
          <p>
            The replenishment system's suggestions for this dark store. Each is a claim to check:
            run the agent to investigate, decide, act and verify.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="primary" onClick={() => go('/new')}>
            New scenario
          </button>
          {confirmReset && (
            <button onClick={() => setConfirmReset(false)}>Keep current data</button>
          )}
          <button className={confirmReset ? 'danger' : ''} onClick={reset}>
            {confirmReset ? 'Confirm: wipe runs and restore scenarios' : 'Reset demo data'}
          </button>
        </div>
      </div>

      {error && <div className="notice error">{error}</div>}
      {!agentReady && (
        <div className="notice info">
          The agent is disabled (no LLM configured). You can browse data; set GEMINI_API_KEY or
          LLM_PROVIDER=replay to run it.
        </div>
      )}

      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>Recommendation</th>
              <th>Scenario</th>
              <th className="num">Recommended</th>
              <th>Status</th>
              <th>Latest agent run</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {recs === null && (
              <tr>
                <td colSpan={6} className="muted">
                  Loading...
                </td>
              </tr>
            )}
            {recs?.map((rec) => {
              const run = rec.latest_run
              const active = ACTIVE_RUN_STATUSES.includes(run?.status ?? '') ||
                run?.status === 'awaiting_approval'
              // An earlier run already placed an order: a new review would only count it as
              // stock on the way, so the button stays off until the demo data is reset.
              const ordered = rec.live_po && !active ? rec.live_po : null
              const replayOnly = rec.custom && !liveModel
              return (
                <tr key={rec.id}>
                  <td>
                    <strong>{rec.product_name}</strong>
                    <span className="sub mono">
                      {rec.id} · {rec.sku} · {rec.supplier_name}
                    </span>
                  </td>
                  <td>
                    {rec.title} {rec.custom && <span className="chip violet">custom</span>}
                    <span className="sub mono">{rec.scenario}</span>
                  </td>
                  <td className="num mono">{rec.quantity}</td>
                  <td>
                    <StatusChip value={rec.status} />
                  </td>
                  <td>
                    {run ? (
                      <a href={`#/runs/${run.id}`}>
                        <StatusChip value={run.status} />{' '}
                        {run.decision && (
                          <span className="mono">
                            {run.decision} {run.final_quantity}
                          </span>
                        )}
                        <span className="sub mono">{run.id}</span>
                      </a>
                    ) : (
                      <span className="muted">not run</span>
                    )}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      className="primary"
                      disabled={
                        !agentReady || active || ordered !== null || replayOnly || starting !== null
                      }
                      title={replayOnly ? 'Replay mode cannot run custom scenarios' : undefined}
                      onClick={() => start(rec)}
                    >
                      {starting === rec.id
                        ? 'Starting...'
                        : ordered
                          ? 'Ordered'
                          : run
                            ? 'Run again'
                            : 'Run agent'}
                    </button>
                    {ordered && (
                      <span className="sub">
                        <span className="mono">{ordered.id}</span> · {ordered.quantity} units,{' '}
                        {ordered.status}.{' '}
                        {rec.custom ? (
                          <a href={`#/new/${rec.id}`}>Duplicate as new</a>
                        ) : (
                          'Reset demo data to review again.'
                        )}
                      </span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
