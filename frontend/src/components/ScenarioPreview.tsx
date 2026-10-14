import type { Finding, PreviewResponse, ScenarioValues } from '../api'
import { StatusChip } from './StatusChip'

const money = (n: unknown) =>
  typeof n === 'number' ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '-'

function Codes({ items, tone }: { items: Finding[]; tone: string }) {
  return (
    <>
      {items.map((f) => (
        <span key={f.code} className={`chip ${tone}`} title={f.message}>
          {f.code}
        </span>
      ))}
    </>
  )
}

// What the deterministic engine and a rules-only system make of the form. The agent never
// sees this panel: it reads the raw data through its tools and decides for itself.
export function ScenarioPreview({
  result,
  values,
  updating,
}: {
  result: PreviewResponse | null
  values: ScenarioValues
  updating: boolean
}) {
  const preview = result?.preview
  const formProblems = result?.problems.filter((p) => !p.field || p.field === '(form)') ?? []

  return (
    <section className="card preview">
      <header>
        <h2>What the engine sees</h2>
        {updating && <span className="chip accent">updating</span>}
      </header>
      <div className="body">
        {!result && <p className="muted">Checking...</p>}
        {result && !preview && (
          <div className="notice error">
            Fix the highlighted fields to see the preview.
            {formProblems.length > 0 && (
              <ul>
                {formProblems.map((p) => (
                  <li key={p.message}>{p.message}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {preview && <Facts preview={preview} values={values} />}
      </div>
    </section>
  )
}

function Facts({
  preview,
  values,
}: {
  preview: NonNullable<PreviewResponse['preview']>
  values: ScenarioValues
}) {
  const { evaluation: ev, rules } = preview
  const p = ev.projection
  const l = ev.limits
  const twist =
    values.supplier_response === 'price_increase'
      ? `On confirmation the supplier will charge ${money(values.new_unit_cost)} instead of ${money(values.unit_cost)}.`
      : values.supplier_response === 'partial_fill'
        ? `On confirmation the supplier will ship at most ${values.max_fill_quantity} units.`
        : null

  return (
    <>
      <dl className="kv">
        <dt>Net need</dt>
        <dd className="num">
          <strong>{String(p.net_need)}</strong>{' '}
          <span className="muted">
            (order-up-to {String(p.order_up_to_level)} over {String(p.coverage_window_days)} days
            minus position {String(p.inventory_position)})
          </span>
        </dd>
        <dt>Smallest order for it</dt>
        <dd className="num">{l.order_for_need}</dd>
        <dt>Largest feasible</dt>
        <dd className="num">
          {l.max_feasible_quantity}{' '}
          <span className="muted">
            (budget {l.max_affordable_quantity}, storage {l.max_storable_quantity})
          </span>
        </dd>
        <dt>Recommended {ev.quantity}</dt>
        <dd>
          {money(p.order_value)} ·{' '}
          {ev.feasible ? (
            <span className="chip ok">feasible</span>
          ) : (
            <span className="chip bad">infeasible</span>
          )}{' '}
          <Codes items={ev.hard_violations} tone="bad" />
        </dd>
        <dt>After it arrives</dt>
        <dd className="num">
          peak {String(p.peak_on_hand_after_arrival)} units ·{' '}
          {String(p.cover_days_after_receipt)} days of cover
        </dd>
        {p.stockout_day_without_order !== null && (
          <>
            <dt>Without an order</dt>
            <dd>runs out on day {String(p.stockout_day_without_order)}</dd>
          </>
        )}
        <dt>Needs a human?</dt>
        <dd>
          {ev.approval_reasons.length === 0 ? (
            <span className="muted">no</span>
          ) : (
            <Codes items={ev.approval_reasons} tone="warn" />
          )}
        </dd>
        {preview.recommendation_findings.length > 0 && (
          <>
            <dt>Stale inputs</dt>
            <dd>
              <Codes items={preview.recommendation_findings} tone="accent" />
            </dd>
          </>
        )}
      </dl>
      {twist && <p className="small">{twist}</p>}
      <div className="rules">
        <span className="muted">A rules-only system would:</span>{' '}
        <StatusChip value={rules.decision} /> <strong className="num">{rules.quantity}</strong>
        {rules.requires_approval && <span className="muted"> (with approval)</span>}
        <ul className="small muted">
          {rules.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
          <li>It trusts the forecast, counts every open PO and cannot read notes.</li>
        </ul>
      </div>
      <p className="small muted">
        The agent does not see this panel. It reads the data through its tools and decides.
      </p>
    </>
  )
}
