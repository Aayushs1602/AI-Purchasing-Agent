import { useState, type ReactNode } from 'react'

// Small form controls for the scenario builder. Each shows the server's problem for its
// field (the preview validates the whole form) and can be marked as filled by a description.

interface FieldProps {
  label: string
  error?: string
  hint?: ReactNode
  filled?: boolean
  wide?: boolean
  children: ReactNode
}

export function Field({ label, error, hint, filled, wide, children }: FieldProps) {
  return (
    <label className={`field${filled ? ' filled' : ''}${wide ? ' wide' : ''}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <small className="muted">{hint}</small>}
      {error && <small className="field-error">{error}</small>}
    </label>
  )
}

function parseNumber(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '') return null
  const n = Number(trimmed)
  return Number.isFinite(n) ? n : null
}

const show = (value: number | null | undefined) =>
  value === null || value === undefined ? '' : String(value)

interface NumberProps extends Omit<FieldProps, 'children'> {
  value: number | null | undefined
  onChange: (value: number | null) => void
  placeholder?: string
}

// Keeps what is typed (e.g. "-" on the way to "-2") and reports a number, or null when empty.
export function NumberField({ value, onChange, placeholder, ...field }: NumberProps) {
  const [text, setText] = useState(show(value))
  const [seen, setSeen] = useState(value)
  if (value !== seen) {
    // Changed from outside (a preset or a description): show the new value.
    setSeen(value)
    if (parseNumber(text) !== value) setText(show(value))
  }
  return (
    <Field {...field}>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        placeholder={placeholder}
        aria-invalid={Boolean(field.error)}
        onChange={(e) => {
          const next = parseNumber(e.target.value)
          setText(e.target.value)
          setSeen(next)
          onChange(next)
        }}
      />
    </Field>
  )
}

interface TextProps extends Omit<FieldProps, 'children'> {
  value: string | null | undefined
  onChange: (value: string | null) => void
  maxLength?: number
  multiline?: boolean
  placeholder?: string
}

export function TextField({ value, onChange, maxLength, multiline, placeholder, ...field }: TextProps) {
  const props = {
    value: value ?? '',
    maxLength,
    placeholder,
    'aria-invalid': Boolean(field.error),
    onChange: (e: { target: { value: string } }) => onChange(e.target.value || null),
  }
  return (
    <Field {...field}>{multiline ? <textarea rows={3} {...props} /> : <input {...props} />}</Field>
  )
}

interface SelectProps extends Omit<FieldProps, 'children'> {
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
}

export function SelectField({ value, onChange, options, ...field }: SelectProps) {
  return (
    <Field {...field}>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  )
}
