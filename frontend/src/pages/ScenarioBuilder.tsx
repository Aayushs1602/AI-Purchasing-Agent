import { useEffect, useState } from 'react'
import {
  api,
  type BuilderOptions,
  type Health,
  type PreviewResponse,
  type ReviewerCall,
  type ScenarioValues,
} from '../api'
import { DescribeBox } from '../components/DescribeBox'
import { NumberField, SelectField, TextField } from '../components/fields'
import { ScenarioForm } from '../components/ScenarioForm'
import { ScenarioPreview } from '../components/ScenarioPreview'
import { go } from '../router'

const DEFAULT_PRESET = 'accept_baseline'
const PREVIEW_DELAY_MS = 400

// Why the agent (or the describe box) cannot run here, or null if it can.
function liveModelProblem(health: Health | null): string | null {
  if (health === null) return 'The API is unreachable.'
  if (!health.custom_scenarios) return 'Custom scenarios are turned off (ALLOW_CUSTOM_SCENARIOS).'
  if (health.agent !== 'ready') return 'No model is configured (set GEMINI_API_KEY in .env).'
  if (health.llm_provider === 'replay')
    return 'Replay mode only plays back the recorded demo scenarios. Set LLM_PROVIDER=gemini to use the agent on your own.'
  return null
}

export function ScenarioBuilder({ from, health }: { from?: string; health: Health | null }) {
  const [options, setOptions] = useState<BuilderOptions | null>(null)
  const [presetKey, setPresetKey] = useState(DEFAULT_PRESET)
  const [values, setValues] = useState<ScenarioValues | null>(null)
  const [filled, setFilled] = useState<Set<string>>(new Set())
  const [title, setTitle] = useState<string | null>(null)
  const [call, setCall] = useState<ReviewerCall | null>(null)
  const [preview, setPreview] = useState<PreviewResponse | null>(null)
  const [updating, setUpdating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  // Presets, and the custom scenario to duplicate when the route names one.
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const opts = await api.builderOptions()
        if (cancelled) return
        setOptions(opts)
        if (from) {
          const stored = await api.customScenario(from)
          if (cancelled) return
          setValues(stored.input)
          setPresetKey(stored.preset ?? DEFAULT_PRESET)
          setTitle(`${stored.title} (copy)`.slice(0, 120))
          setCall(stored.reviewer_call)
        } else {
          const preset = opts.presets.find((p) => p.key === DEFAULT_PRESET) ?? opts.presets[0]
          setValues(preset.input)
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [from])

  // Live preview: debounced, and a newer edit cancels the request for an older one.
  useEffect(() => {
    if (!values) return
    const controller = new AbortController()
    setUpdating(true)
    const timer = setTimeout(async () => {
      try {
        setPreview(await api.previewScenario(values, controller.signal))
        setError(null)
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message)
      } finally {
        if (!controller.signal.aborted) setUpdating(false)
      }
    }, PREVIEW_DELAY_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [values])

  function update(patch: ScenarioValues) {
    setValues((current) => ({ ...current, ...patch }))
    setFilled((current) => {
      const next = new Set(current)
      Object.keys(patch).forEach((key) => next.delete(key))
      return next
    })
  }

  function choosePreset(key: string) {
    const preset = options?.presets.find((p) => p.key === key)
    if (!preset) return
    setPresetKey(key)
    setValues(preset.input)
    setFilled(new Set())
  }

  async function create(run: boolean) {
    if (!values) return
    setCreating(true)
    setError(null)
    try {
      const created = await api.createScenario({
        input: values,
        reviewer_call: call,
        title: title?.trim() || null,
        preset: presetKey,
        run,
      })
      go(created.run_id ? `/runs/${created.run_id}` : '/')
    } catch (e) {
      setError((e as Error).message)
      setCreating(false)
    }
  }

  const modelProblem = liveModelProblem(health)
  const errors = Object.fromEntries((preview?.problems ?? []).map((p) => [p.field, p.message]))
  const invalid = !preview?.preview || updating
  const createOff = health !== null && !health.custom_scenarios
  const preset = options?.presets.find((p) => p.key === presetKey)

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>New scenario</h1>
          <p>
            Set up your own purchasing situation, see what the engine and a rules-only system
            make of it, then let the agent review it live. The agent sees only the data, through
            its tools: not the preset, the preview or your call.
          </p>
        </div>
      </div>
      {error && <div className="notice error">{error}</div>}
      {!options || !values ? (
        <p className="muted">Loading...</p>
      ) : (
        <div className="run-grid">
          <div className="stack">
            <section className="card">
              <header>
                <h2>Start from</h2>
              </header>
              <div className="body">
                <SelectField
                  label="Preset"
                  value={presetKey}
                  onChange={choosePreset}
                  options={options.presets.map((p) => ({ value: p.key, label: p.title }))}
                />
                {preset && <p className="small muted">{preset.summary}</p>}
              </div>
            </section>
            <DescribeBox
              values={values}
              disabledReason={modelProblem}
              onApply={(input, fields) => {
                setValues(input)
                setFilled(new Set(fields))
              }}
            />
            <ScenarioForm
              values={values}
              update={update}
              errors={errors}
              filled={filled}
              suppliers={options.suppliers}
              formulaQuantity={preview?.preview?.formula_quantity ?? null}
            />
          </div>

          <div className="side sticky">
            <ScenarioPreview result={preview} values={values} updating={updating} />

            <section className="card">
              <header>
                <h2>Your call</h2>
                <span className="muted small">optional</span>
              </header>
              <div className="body">
                <label className="check">
                  <input
                    type="checkbox"
                    checked={call !== null}
                    onChange={(e) =>
                      setCall(
                        e.target.checked
                          ? { decision: 'modify', quantity_min: null, quantity_max: null, note: null }
                          : null,
                      )
                    }
                  />
                  Say what you think the right answer is before the agent runs
                </label>
                {call && (
                  <div className="form-grid">
                    <SelectField
                      label="Decision"
                      value={call.decision}
                      onChange={(decision) => setCall({ ...call, decision })}
                      options={['accept', 'modify', 'reject', 'investigate'].map((d) => ({
                        value: d,
                        label: d,
                      }))}
                    />
                    <NumberField
                      label="Units from"
                      value={call.quantity_min}
                      placeholder="any"
                      onChange={(quantity_min) => setCall({ ...call, quantity_min })}
                    />
                    <NumberField
                      label="Units to"
                      value={call.quantity_max}
                      placeholder="any"
                      onChange={(quantity_max) => setCall({ ...call, quantity_max })}
                    />
                    <TextField
                      label="Why (for you)"
                      wide
                      value={call.note}
                      maxLength={300}
                      onChange={(note) => setCall({ ...call, note })}
                    />
                  </div>
                )}
                <p className="small muted">
                  Stored apart from the scenario and never shown to the agent. The run page shows
                  whether the agent agrees.
                </p>
              </div>
            </section>

            <section className="card">
              <header>
                <h2>Create</h2>
              </header>
              <div className="body">
                <TextField
                  label="Name in the inbox"
                  value={title}
                  maxLength={120}
                  placeholder={`Custom: ${values.product_name ?? ''}`}
                  onChange={setTitle}
                  hint="For you; the agent does not see it."
                />
                <div className="actions">
                  <button
                    className="primary"
                    disabled={creating || invalid || modelProblem !== null}
                    onClick={() => create(true)}
                  >
                    {creating ? 'Creating...' : 'Create and run the agent'}
                  </button>
                  <button disabled={creating || invalid || createOff} onClick={() => create(false)}>
                    Create only
                  </button>
                </div>
                {modelProblem && <p className="small muted">{modelProblem}</p>}
                {invalid && preview && !preview.preview && (
                  <p className="small field-error">Fix the highlighted fields first.</p>
                )}
                <p className="small muted">
                  The run uses the live model (about 5 to 8 calls). Reset demo data removes custom
                  scenarios.
                </p>
              </div>
            </section>
          </div>
        </div>
      )}
    </div>
  )
}
