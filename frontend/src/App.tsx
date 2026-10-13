import { useCallback, useEffect, useState } from 'react'
import { api, type Health } from './api'
import { Inbox } from './pages/Inbox'
import { RunDetail } from './pages/RunDetail'
import { useRoute } from './router'

export default function App() {
  const route = useRoute()
  const [health, setHealth] = useState<Health | null>(null)
  const [pending, setPending] = useState(0)

  const refreshShell = useCallback(async () => {
    try {
      const [h, approvals] = await Promise.all([api.health(), api.approvals()])
      setHealth(h)
      setPending(approvals.length)
    } catch {
      setHealth(null)
    }
  }, [])

  useEffect(() => {
    refreshShell()
    const timer = setInterval(refreshShell, 5000)
    return () => clearInterval(timer)
  }, [refreshShell])

  const agentReady = health?.agent === 'ready'

  return (
    <>
      <header className="topbar">
        <span className="brand">
          Purchasing Agent<small>Chapinero dark store · DS-BOG-01</small>
        </span>
        <nav>
          <a href="#/" className={route.page === 'inbox' ? 'active' : ''}>
            Recommendations
          </a>
          <a href="#/approvals" className={route.page === 'approvals' ? 'active' : ''}>
            Approvals{pending > 0 && <span className="badge">{pending}</span>}
          </a>
        </nav>
        <span className="spacer" />
        <span className={`agent-state ${agentReady ? '' : 'disabled'}`}>
          <span className="dot" />
          {health === null
            ? 'API unreachable'
            : agentReady
              ? 'Agent ready'
              : 'Agent disabled'}
        </span>
      </header>

      {route.page === 'inbox' && <Inbox agentReady={agentReady} onDataChanged={refreshShell} />}
      {route.page === 'run' && <RunDetail id={route.id} />}
      {route.page === 'approvals' && (
        <div className="page">
          <p className="muted">Coming next.</p>
        </div>
      )}
    </>
  )
}
