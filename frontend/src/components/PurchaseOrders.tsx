import type { PurchaseOrder } from '../api'
import { StatusChip } from './StatusChip'

const money = (n: number | null) =>
  n === null ? '-' : `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`

export function PurchaseOrders({ pos }: { pos: PurchaseOrder[] }) {
  return (
    <section className="card">
      <header>
        <h2>Purchase orders</h2>
      </header>
      <div className="body">
        {pos.length === 0 && <p className="muted">This run placed no order.</p>}
        {pos.map((po) => (
          <div key={po.id} className="po">
            <div className="po-head">
              <span className="mono">{po.id}</span> <StatusChip value={po.status} />
              {po.revision > 1 && <span className="muted"> rev {po.revision}</span>}
            </div>
            <dl className="kv">
              <dt>Ordered</dt>
              <dd className="num">
                {po.quantity} × {money(po.unit_cost)}
              </dd>
              {po.confirmed_quantity !== null && (
                <>
                  <dt>Confirmed</dt>
                  <dd className="num">
                    {po.confirmed_quantity} × {money(po.confirmed_unit_cost)}
                  </dd>
                </>
              )}
              <dt>Value</dt>
              <dd className="num">{money(po.value)}</dd>
              {po.eta && (
                <>
                  <dt>ETA</dt>
                  <dd>{po.eta}</dd>
                </>
              )}
              {po.approval_status && (
                <>
                  <dt>Approval</dt>
                  <dd>
                    {po.approval_status}
                    {po.approval_by && ` by ${po.approval_by}`}
                    {po.approval_comment && ` - "${po.approval_comment}"`}
                  </dd>
                </>
              )}
            </dl>
            {po.supplier_message && (
              <p className="supplier-message">
                <span className="chip warn">supplier says</span> {po.supplier_message}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}
