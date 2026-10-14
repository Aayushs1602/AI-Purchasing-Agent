import type { ScenarioValues, SupplierOption } from '../api'
import { Field, NumberField, SelectField, TextField } from './fields'

const NEW_SUPPLIER = '__new__'
const MAX_OPEN_POS = 5

interface Props {
  values: ScenarioValues
  update: (patch: ScenarioValues) => void
  errors: Record<string, string>
  filled: Set<string>
  suppliers: SupplierOption[]
  formulaQuantity: number | null
}

const pct = (rate: number) => `${Math.round(rate * 100)}%`
const money = (n: number) => `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`

// Every input the agent's world is built from, grouped the way a buyer thinks about it.
export function ScenarioForm({ values: v, update, errors, filled, suppliers, formulaQuantity }: Props) {
  const num = (key: string, label: string, extra: { hint?: string; placeholder?: string } = {}) => (
    <NumberField
      label={label}
      value={v[key]}
      onChange={(value) => update({ [key]: value })}
      error={errors[key]}
      filled={filled.has(key)}
      {...extra}
    />
  )

  const newSupplier = v.supplier_id === null || v.supplier_id === undefined
  const available =
    typeof v.budget_total === 'number' && typeof v.budget_committed === 'number'
      ? v.budget_total - v.budget_committed
      : null

  return (
    <>
      <section className="card">
        <header>
          <h2>Product</h2>
        </header>
        <div className="body form-grid">
          <TextField
            label="Product name"
            value={v.product_name}
            onChange={(value) => update({ product_name: value })}
            error={errors.product_name}
            filled={filled.has('product_name')}
            maxLength={60}
            hint="The agent sees it. One line, plain characters."
          />
          <TextField
            label="Category (budget line)"
            value={v.category}
            onChange={(value) => update({ category: value })}
            error={errors.category}
            filled={filled.has('category')}
            maxLength={40}
          />
        </div>
      </section>

      <section className="card">
        <header>
          <h2>Stock at the store</h2>
        </header>
        <div className="body form-grid">
          {num('on_hand', 'On hand (units)')}
          {num('reserved', 'Reserved for orders')}
          {num('safety_stock', 'Safety stock')}
          {num('storage_capacity', 'Storage capacity')}
          {num('review_period_days', 'Days to next review')}
          <div className={`field${filled.has('counted_days_ago') ? ' filled' : ''}`}>
            <span className="field-label">Last stock count (days ago)</span>
            <div className="inline">
              <input
                type="text"
                inputMode="numeric"
                disabled={v.counted_days_ago === null}
                value={v.counted_days_ago ?? ''}
                aria-label="Days since the last stock count"
                onChange={(e) => {
                  const n = Number(e.target.value)
                  if (Number.isInteger(n)) update({ counted_days_ago: n })
                }}
              />
              <label className="check">
                <input
                  type="checkbox"
                  checked={v.counted_days_ago === null}
                  onChange={(e) => update({ counted_days_ago: e.target.checked ? null : 2 })}
                />
                never counted
              </label>
            </div>
            {errors.counted_days_ago && (
              <small className="field-error">{errors.counted_days_ago}</small>
            )}
          </div>
        </div>
      </section>

      <section className="card">
        <header>
          <h2>Demand</h2>
        </header>
        <div className="body form-grid">
          {num('forecast_per_day', 'Forecast per day')}
          {num('sales_per_day', 'Typical sales per day', { hint: 'Last 28 days' })}
          {num('recent_sales_per_day', 'Sales per day, last 14 days', {
            placeholder: 'same as typical',
            hint: 'Set it to make sales drift from the forecast.',
          })}
        </div>
      </section>

      <OpenPOEditor
        rows={v.open_purchase_orders ?? []}
        onChange={(rows) => update({ open_purchase_orders: rows })}
        errors={errors}
        filled={filled.has('open_purchase_orders')}
      />

      <section className="card">
        <header>
          <h2>Supplier and terms</h2>
        </header>
        <div className="body form-grid">
          <SelectField
            label="Supplier"
            wide
            value={newSupplier ? NEW_SUPPLIER : v.supplier_id}
            error={errors.supplier_id}
            filled={filled.has('supplier_id')}
            onChange={(value) =>
              update(
                value === NEW_SUPPLIER
                  ? { supplier_id: null }
                  : {
                      supplier_id: value,
                      supplier_name: null,
                      supplier_on_time_rate: null,
                      supplier_fill_rate: null,
                    },
              )
            }
            options={[
              ...suppliers.map((s) => ({
                value: s.id,
                label: `${s.name} · on time ${pct(s.on_time_rate)} · fill ${pct(s.fill_rate)}`,
              })),
              { value: NEW_SUPPLIER, label: 'New supplier...' },
            ]}
          />
          {newSupplier && (
            <>
              <TextField
                label="New supplier name"
                value={v.supplier_name}
                onChange={(value) => update({ supplier_name: value })}
                error={errors.supplier_name}
                filled={filled.has('supplier_name')}
                maxLength={60}
              />
              {num('supplier_on_time_rate', 'On-time rate', { hint: '0 to 1 (0.9 = 90%)' })}
              {num('supplier_fill_rate', 'Fill rate', { hint: '0 to 1' })}
            </>
          )}
          {num('lead_time_days', 'Lead time (days)')}
          {num('moq', 'Minimum order (MOQ)')}
          {num('case_pack', 'Case pack')}
          {num('unit_cost', 'Unit cost (USD)')}
          <TextField
            label="Supplier portal note"
            wide
            multiline
            value={v.supplier_note}
            onChange={(value) => update({ supplier_note: value })}
            error={errors.supplier_note}
            filled={filled.has('supplier_note')}
            maxLength={1000}
            hint="Free text the agent reads as untrusted data. Try putting instructions in it."
          />
        </div>
      </section>

      <section className="card">
        <header>
          <h2>When the order is sent, the supplier...</h2>
        </header>
        <div className="body form-grid">
          <SelectField
            label="Supplier answer"
            value={v.supplier_response ?? 'confirm'}
            error={errors.supplier_response}
            filled={filled.has('supplier_response')}
            onChange={(value) => update({ supplier_response: value })}
            options={[
              { value: 'confirm', label: 'confirms it as sent' },
              { value: 'price_increase', label: 'confirms at a higher price' },
              { value: 'partial_fill', label: 'can only ship part of it' },
            ]}
          />
          {v.supplier_response === 'price_increase' && num('new_unit_cost', 'New unit cost (USD)')}
          {v.supplier_response === 'partial_fill' && num('max_fill_quantity', 'Most units it can ship')}
        </div>
      </section>

      <section className="card">
        <header>
          <h2>Budget this month</h2>
        </header>
        <div className="body form-grid">
          {num('budget_total', 'Category budget (USD)')}
          {num('budget_committed', 'Already committed (USD)')}
          <Field label="Available">
            <span className="readout num">{available === null ? '-' : money(available)}</span>
          </Field>
        </div>
      </section>

      <section className="card">
        <header>
          <h2>The recommendation</h2>
        </header>
        <div className="body form-grid">
          <NumberField
            label="Recommended quantity"
            value={v.recommended_quantity}
            onChange={(value) => update({ recommended_quantity: value })}
            error={errors.recommended_quantity}
            filled={filled.has('recommended_quantity')}
            hint={
              formulaQuantity !== null && formulaQuantity !== v.recommended_quantity ? (
                <button
                  type="button"
                  className="link"
                  onClick={() => update({ recommended_quantity: formulaQuantity })}
                >
                  Use the system's own formula: {formulaQuantity}
                </button>
              ) : undefined
            }
          />
          {num('recommendation_days_old', 'Made how many days ago')}
        </div>
        <div className="body form-grid">
          <p className="muted wide small">
            What the replenishment system saw when it computed the quantity. Leave empty for
            "same as now"; set a value to make the recommendation stale.
          </p>
          {num('seen_on_hand', 'It saw on hand', { placeholder: 'same as now' })}
          {num('seen_open_po_units', 'It saw on order', { placeholder: 'same as now' })}
          {num('seen_daily_demand', 'It saw demand per day', { placeholder: 'same as now' })}
        </div>
      </section>
    </>
  )
}

interface PORow {
  quantity: number | null
  eta_in_days: number | null
  status: string
  created_days_ago: number | null
  note: string | null
}

function OpenPOEditor({
  rows,
  onChange,
  errors,
  filled,
}: {
  rows: PORow[]
  onChange: (rows: PORow[]) => void
  errors: Record<string, string>
  filled: boolean
}) {
  const set = (index: number, patch: Partial<PORow>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const rowError = (index: number) =>
    Object.entries(errors)
      .filter(([field]) => field.startsWith(`open_purchase_orders.${index}`))
      .map(([field, message]) => `${field.split('.').slice(2).join('.') || 'row'}: ${message}`)

  return (
    <section className={`card${filled ? ' filled' : ''}`}>
      <header>
        <h2>Purchase orders already on the way</h2>
        <span className="muted small">{rows.length}/{MAX_OPEN_POS}</span>
      </header>
      <div className="body">
        {rows.length === 0 && <p className="muted">None. Add one to test how the agent counts it.</p>}
        {rows.map((row, i) => (
          <div className="po-row" key={i}>
            <div className="form-grid">
              <NumberField
                label="Units"
                value={row.quantity}
                onChange={(value) => set(i, { quantity: value })}
              />
              <NumberField
                label="Due in (days)"
                value={row.eta_in_days}
                hint="Negative = overdue"
                onChange={(value) => set(i, { eta_in_days: value })}
              />
              <SelectField
                label="Status"
                value={row.status}
                onChange={(value) => set(i, { status: value })}
                options={[
                  { value: 'confirmed', label: 'confirmed' },
                  { value: 'submitted', label: 'submitted, not confirmed' },
                ]}
              />
              <NumberField
                label="Placed (days ago)"
                value={row.created_days_ago}
                onChange={(value) => set(i, { created_days_ago: value })}
              />
              <TextField
                label="Latest note from the supplier"
                wide
                value={row.note}
                maxLength={300}
                onChange={(value) => set(i, { note: value })}
                hint="Untrusted free text, e.g. 'shipment on hold' or 'delivery confirmed for tomorrow'."
              />
            </div>
            {rowError(i).map((message) => (
              <small key={message} className="field-error">
                {message}
              </small>
            ))}
            <button type="button" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              Remove
            </button>
          </div>
        ))}
        {errors.open_purchase_orders && (
          <small className="field-error">{errors.open_purchase_orders}</small>
        )}
        <div className="actions">
          <button
            type="button"
            disabled={rows.length >= MAX_OPEN_POS}
            onClick={() =>
              onChange([
                ...rows,
                { quantity: 300, eta_in_days: 2, status: 'confirmed', created_days_ago: 3, note: null },
              ])
            }
          >
            Add open PO
          </button>
        </div>
      </div>
    </section>
  )
}
