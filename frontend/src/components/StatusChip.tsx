const TONES: Record<string, string> = {
  // run / recommendation states
  completed: 'ok',
  decided: 'ok',
  resolved: 'ok',
  running: 'accent',
  queued: 'accent',
  in_review: 'accent',
  awaiting_approval: 'warn',
  escalated: 'violet',
  needs_review: 'bad',
  failed: 'bad',
  open: '',
  // purchase orders
  confirmed: 'ok',
  partially_confirmed: 'warn',
  pending_approval: 'warn',
  draft: '',
  submitted: 'accent',
  declined: 'bad',
  cancelled: '',
  // decisions
  accept: 'ok',
  modify: 'accent',
  reject: '',
  investigate: 'violet',
}

export function StatusChip({ value }: { value: string | null | undefined }) {
  if (!value) return null
  return <span className={`chip ${TONES[value] ?? ''}`}>{value.replace(/_/g, ' ')}</span>
}
