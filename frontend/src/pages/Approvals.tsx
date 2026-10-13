import { useEffect, useState } from 'react'
import { api, type Approval } from '../api'

export function Approvals() {
  const [items, setItems] = useState<Approval[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.approvals().then(setItems, (e: Error) => setError(e.message))
  }, [])

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Waiting for approval</h1>
          <p>
            Purchase orders the policy would not let the agent send on its own. Open the run to
            see the agent's reasoning, then approve or decline.
          </p>
        </div>
      </div>
      {error && <div className="notice error">{error}</div>}
      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>PO</th>
              <th>Product</th>
              <th className="num">Units</th>
              <th className="num">Value</th>
              <th>Why it needs a human</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items?.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  Nothing is waiting.
                </td>
              </tr>
            )}
            {items?.map((a) => (
              <tr key={a.purchase_order.id}>
                <td className="mono">{a.purchase_order.id}</td>
                <td>{a.product_name}</td>
                <td className="num mono">{a.purchase_order.quantity}</td>
                <td className="num mono">
                  ${a.purchase_order.value.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                </td>
                <td>
                  {a.reasons.map((r) => (
                    <span key={r.code} className="chip warn" title={r.message}>
                      {r.code}
                    </span>
                  ))}
                </td>
                <td style={{ textAlign: 'right' }}>
                  {a.run_id ? <a href={`#/runs/${a.run_id}`}>Review run →</a> : <span className="muted">no run</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
