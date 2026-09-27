import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, Archive, Bot, CheckCircle2, ChevronRight, CircleDollarSign, Clock, Eye,
  Gauge, History, KeyRound, LayoutDashboard, Play, RefreshCw, Route, Save, Settings,
  TerminalSquare, Trash2, X,
} from 'lucide-react'

type View = 'overview' | 'run' | 'providers' | 'capsules' | 'settings' | 'secrets'
type QuotaWindow = { kind: string; percent: number; resetAt?: string }
type QuotaMeter = { label: string; windows: QuotaWindow[] }
type QuotaExtra = { planName?: string; hideDaily?: boolean; dailyPercent?: number; dailyResetAt?: string; weeklyPercent?: number; weeklyResetAt?: string; overageUsd?: number; meters?: QuotaMeter[] }
type Quota = { state: string; detail: string; remainingPercent: number | null; resetAt: string | null; observedAt: string; extra?: QuotaExtra }
type Adapter = { name: string; kind: string; reachable: boolean; note: string; quota: Quota }
type Run = { task: string; taskClass: string; adapter: string; model: string; cost: number; timestamp: number }
type Overview = { metrics: { runs: number; spend: number; savings: number }; recentRuns: Run[]; spendByAdapter: Array<{ adapter: string; runs: number; spend: number }> }
type Capsule = { path: string; task: string; adapter: string; created_at: string; triggered_by: string }
type Decision = { adapter: string; model: string; task_class: string; reason: string; est_cost_usd: number }

const navigation = [
  ['overview', LayoutDashboard, 'Overview'],
  ['run', Play, 'Run task'],
  ['providers', Bot, 'Providers'],
  ['capsules', Archive, 'Capsules'],
  ['settings', Settings, 'Settings'],
  ['secrets', KeyRound, 'Secrets'],
] as const

const providerStyles: Record<string, { color: string; icon: string }> = {
  claude: { color: '#d4a574', icon: 'CL' },
  cursor: { color: '#74d4a5', icon: 'CU' },
  gemini: { color: '#74a5d4', icon: 'GM' },
  codex: { color: '#d4749f', icon: 'CD' },
  devin: { color: '#a574d4', icon: 'DV' },
  perplexity: { color: '#74d4d4', icon: 'PP' },
  openrouter: { color: '#d4d474', icon: 'OR' },
}

function initials(name: string) {
  return providerStyles[name]?.icon || name.slice(0, 2).toUpperCase()
}

function providerColor(name: string) {
  return providerStyles[name]?.color || '#8e9992'
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail || 'Request failed')
  }
  return response.json()
}

const money = (value: number) => `$${value.toFixed(4)}`
const date = (value: string | number) => new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString()

function useToasts() {
  const [toasts, setToasts] = useState<Array<{ id: number; message: string; type: 'success' | 'error' }>>([])
  const push = useCallback((message: string, type: 'success' | 'error' = 'success') => {
    const id = Date.now() + Math.random()
    setToasts(prev => [...prev, { id, message, type }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 4000)
  }, [])
  const remove = useCallback((id: number) => setToasts(prev => prev.filter(t => t.id !== id)), [])
  return { toasts, push, remove }
}

function App() {
  const [view, setView] = useState<View>('overview')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [adapters, setAdapters] = useState<Adapter[]>([])
  const [capsules, setCapsules] = useState<Capsule[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [resumePath, setResumePath] = useState('')
  const { toasts, push, remove } = useToasts()

  const refresh = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const [nextOverview, providerData, capsuleData] = await Promise.all([
        api<Overview>('/api/overview'),
        api<{ adapters: Adapter[] }>('/api/adapters'),
        api<{ capsules: Capsule[] }>('/api/capsules'),
      ])
      setOverview(nextOverview); setAdapters(providerData.adapters); setCapsules(capsuleData.capsules)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to load application data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  const healthy = adapters.filter(item => item.reachable && item.quota.state !== 'depleted').length

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Route size={21}/></div>
          <div><strong>Tollkeeper</strong><span>Local control center</span></div>
        </div>
        <nav>{navigation.map(([id, Icon, label]) => (
          <button key={id} className={view === id ? 'active' : ''} onClick={() => setView(id as View)}>
            <Icon size={18}/><span>{label}</span>
          </button>
        ))}</nav>
        <div className="sidebar-foot">
          <span className="live-dot"/>
          <div><strong>{healthy} providers ready</strong><span>Local service online</span></div>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">Workspace</span>
            <h1>{navigation.find(item => item[0] === view)?.[2]}</h1>
          </div>
          <button className="icon-button" onClick={() => void refresh()} aria-label="Refresh" title="Refresh">
            <RefreshCw size={18} className={loading ? 'spin' : ''}/>
          </button>
        </header>
        {error && <div className="alert">{error}</div>}
        {view === 'overview' && <OverviewView data={overview} adapters={adapters} loading={loading} onRun={() => setView('run')}/>}
        {view === 'run' && <RunView adapters={adapters} capsules={capsules} initialResume={resumePath} onComplete={refresh} pushToast={push}/>}
        {view === 'providers' && <ProvidersView adapters={adapters}/>}
        {view === 'capsules' && <CapsulesView capsules={capsules} onResume={path => { setResumePath(path); setView('run') }}/>}
        {view === 'settings' && <SettingsView pushToast={push}/>}
        {view === 'secrets' && <SecretsView pushToast={push}/>}
      </main>
      <div className="toast-stack">
        {toasts.map(t => (
          <div key={t.id} className={`toast ${t.type}`}>
            {t.type === 'success' ? <CheckCircle2 size={16}/> : <X size={16}/>}
            <span>{t.message}</span>
            <button onClick={() => remove(t.id)} aria-label="Dismiss"><X size={14}/></button>
          </div>
        ))}
      </div>
    </div>
  )
}

function SkeletonMetrics() {
  return (
    <section className="metrics">
      {[0, 1, 2, 3].map(i => <div key={i} className="metric skeleton"><div className="metric-icon shimmer"/><span className="shimmer"/></div>)}
    </section>
  )
}

function SkeletonTable() {
  return (
    <div className="skeleton-table">
      {[0, 1, 2, 3, 4].map(i => <div key={i} className="skeleton-row"><div className="shimmer"/><div className="shimmer short"/></div>)}
    </div>
  )
}

function OverviewView({ data, adapters, loading, onRun }: { data: Overview | null; adapters: Adapter[]; loading: boolean; onRun: () => void }) {
  if (loading || !data) {
    return (
      <div className="page-stack">
        <section className="hero skeleton"><div className="shimmer title"/><div className="shimmer subtitle"/></section>
        <SkeletonMetrics/>
        <div className="content-grid">
          <section className="panel wide"><SkeletonTable/></section>
          <section className="panel"><div className="provider-list">{adapters.map(a => <ProviderLine key={a.name} adapter={a}/>)}</div></section>
        </div>
      </div>
    )
  }
  return (
    <div className="page-stack">
      <section className="hero">
        <div>
          <span className="eyebrow">Subscription-first routing</span>
          <h2>Send the next task to the right model.</h2>
          <p>Route across the tools you already pay for, preserve context, and keep metered spend visible.</p>
        </div>
        <button className="primary" onClick={onRun}><Play size={17}/>New task</button>
      </section>
      <section className="metrics">
        <Metric icon={History} label="Runs, 30 days" value={String(data.metrics.runs)}/>
        <Metric icon={CircleDollarSign} label="Actual spend" value={money(data.metrics.spend)}/>
        <Metric icon={Gauge} label="Estimated savings" value={money(data.metrics.savings)}/>
        <Metric icon={Activity} label="Providers ready" value={`${adapters.filter(a => a.reachable).length}/${adapters.length}`}/>
      </section>
      <div className="content-grid">
        <section className="panel wide">
          <div className="section-title">
            <div><span className="eyebrow">Latest activity</span><h3>Recent runs</h3></div>
          </div>
          <RunTable runs={data.recentRuns}/>
        </section>
        <section className="panel">
          <div className="section-title">
            <div><span className="eyebrow">Availability</span><h3>Provider pulse</h3></div>
          </div>
          <div className="provider-list">{adapters.map(adapter => <ProviderLine key={adapter.name} adapter={adapter}/>)}</div>
        </section>
      </div>
    </div>
  )
}

function Metric({ icon: Icon, label, value }: { icon: typeof Activity; label: string; value: string }) {
  return (
    <div className="metric">
      <div className="metric-icon"><Icon size={19}/></div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function RunView({ adapters, capsules, initialResume, onComplete, pushToast }: {
  adapters: Adapter[]; capsules: Capsule[]; initialResume: string; onComplete: () => void; pushToast: (m: string, t?: 'success' | 'error') => void
}) {
  const [task, setTask] = useState('')
  const [adapter, setAdapter] = useState('')
  const [model, setModel] = useState('')
  const [resume, setResume] = useState(initialResume)
  const [decision, setDecision] = useState<Decision | null>(null)
  const [output, setOutput] = useState('')
  const [state, setState] = useState<'idle' | 'starting' | 'running' | 'completed' | 'failed'>('idle')
  const [error, setError] = useState('')
  const [planning, setPlanning] = useState(false)
  const terminalRef = useRef<HTMLPreElement>(null)
  const payload = useMemo(() => ({ task, adapter: adapter || null, model: model || null, resume: resume || null }), [task, adapter, model, resume])

  useEffect(() => setResume(initialResume), [initialResume])

  useEffect(() => {
    if (terminalRef.current) terminalRef.current.scrollTop = terminalRef.current.scrollHeight
  }, [output])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && task && state !== 'running' && state !== 'starting') {
        event.preventDefault()
        void submitTask()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [task, state])

  async function preview() {
    setError(''); setPlanning(true)
    try {
      setDecision(await api<Decision>('/api/plan', { method: 'POST', body: JSON.stringify(payload) }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to plan route')
    } finally {
      setPlanning(false)
    }
  }

  async function submitTask() {
    setError(''); setOutput(''); setState('starting')
    try {
      const job = await api<{ id: string }>('/api/runs', { method: 'POST', body: JSON.stringify(payload) })
      const stream = new EventSource(`/api/runs/${job.id}/events`)
      stream.addEventListener('state', () => setState('running'))
      stream.addEventListener('output', event => {
        const data = JSON.parse((event as MessageEvent).data)
        setOutput(current => current + data.text)
      })
      stream.addEventListener('completed', event => {
        const data = JSON.parse((event as MessageEvent).data)
        setDecision(data.decision)
        setState('completed')
        stream.close()
        void onComplete()
        pushToast('Run completed', 'success')
      })
      stream.addEventListener('failed', event => {
        const data = JSON.parse((event as MessageEvent).data)
        setError(data.error)
        setState('failed')
        stream.close()
        pushToast(data.error, 'error')
      })
      stream.onerror = () => { if (state === 'running') setError('Output stream disconnected') }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to start run')
      setState('failed')
      pushToast('Failed to start run', 'error')
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    void submitTask()
  }

  return (
    <div className="run-layout">
      <form className="panel composer" onSubmit={submit}>
        <span className="eyebrow">Task composer</span>
        <h2>What should the router work on?</h2>
        <textarea
          id="task"
          name="task"
          value={task}
          onChange={event => setTask(event.target.value)}
          placeholder="Describe a coding task, bug, refactor, or research question…"
          required
        />
        <div className="form-row">
          <label htmlFor="provider">Provider
            <select id="provider" name="provider" value={adapter} onChange={event => setAdapter(event.target.value)}>
              <option value="">Automatic routing</option>
              {adapters.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
            </select>
          </label>
          <label htmlFor="model">Model
            <input id="model" name="model" value={model} onChange={event => setModel(event.target.value)} placeholder="Policy default"/>
          </label>
        </div>
        <label htmlFor="resume">Resume capsule
          <select id="resume" name="resume" value={resume} onChange={event => setResume(event.target.value)}>
            <option value="">Start fresh</option>
            {capsules.map(item => <option value={item.path} key={item.path}>{item.task}</option>)}
          </select>
        </label>
        {error && <div className="alert compact">{error}</div>}
        <div className="actions">
          <button type="button" className="secondary" onClick={() => void preview()} disabled={!task || planning || state === 'running'}>
            {planning ? <RefreshCw size={16} className="spin"/> : <Route size={16}/>}
            Preview route
          </button>
          <button className="primary" disabled={!task || state === 'running' || state === 'starting'}>
            <Play size={17}/>
            {state === 'starting' ? 'Starting…' : state === 'running' ? 'Running…' : 'Run task'}
          </button>
        </div>
        <p className="hint">Tip: Press <kbd>Cmd</kbd>+<kbd>Enter</kbd> to run.</p>
      </form>
      <div className="run-side">
        {decision && (
          <section className="route-card">
            <span className="eyebrow">Selected route</span>
            <div className="route-path">
              <span className="route-avatar" style={{ background: providerColor(decision.adapter) }}>{initials(decision.adapter)}</span>
              <strong>{decision.adapter}</strong>
              <ChevronRight size={18}/>
              <span>{decision.model}</span>
            </div>
            <p>{decision.reason}</p>
            <div className="route-meta">
              <span>{decision.task_class}</span>
              <span>{money(decision.est_cost_usd)} estimated</span>
            </div>
          </section>
        )}
        <section className="terminal">
          <div className="terminal-head">
            <span><TerminalSquare size={16}/>Live output</span>
            <span className={`run-state ${state}`}>
              {state === 'idle' && 'idle'}
              {state === 'starting' && 'starting'}
              {state === 'running' && <><span className="pulse-dot"/>running</>}
              {state === 'completed' && 'completed'}
              {state === 'failed' && 'failed'}
            </span>
          </div>
          <pre ref={terminalRef}>{output || 'Output will stream here when the run begins.'}</pre>
        </section>
      </div>
    </div>
  )
}

const UNTESTED_PROVIDERS = new Set(['perplexity', 'zcode', 'deepseek', 'grok', 'amp', 'kimi', 'minimax'])

function UntestedPill({ name }: { name: string }) {
  if (!UNTESTED_PROVIDERS.has(name.toLowerCase())) return null
  return <span className="untested-pill" title="Quota probe for this provider hasn't been verified against a live account">untested</span>
}

function ProvidersView({ adapters }: { adapters: Adapter[] }) {
  return (
    <div className="cards-grid">
      {adapters.map(adapter => {
        const pct = adapter.quota.remainingPercent
        const barColor = pct === null ? '#3b4a42' : pct > 50 ? '#63db91' : pct > 20 ? '#f0c674' : '#ff968a'
        return (
          <article className="provider-card" key={adapter.name}>
            <div className="provider-head">
              <div className="provider-glyph" style={{ background: `${providerColor(adapter.name)}20`, color: providerColor(adapter.name), borderColor: `${providerColor(adapter.name)}40` }}>
                {initials(adapter.name)}
              </div>
              <div><h3>{adapter.name} <UntestedPill name={adapter.name}/></h3><span>{adapter.kind}</span></div>
              <Status state={adapter.reachable ? adapter.quota.state : 'offline'}/>
            </div>
            <p>{adapter.quota.detail}</p>
            <div className="quota-label">
              <span>Provider quota</span>
              <strong>{pct === null ? 'Not reported' : `${pct}% left`}</strong>
            </div>
            <div className="quota-track">
              <div style={{ width: `${pct ?? 0}%`, background: barColor }}/>
            </div>
            {adapter.quota.extra?.meters && (
              <div className="quota-meters">
                {adapter.quota.extra.meters.map(meter => (
                  <div className="quota-meter" key={meter.label}>
                    <div className="quota-meter-label"><span>{meter.label}</span></div>
                    {meter.windows.map((window, index) => {
                      const wPct = Math.min(Math.max(window.percent, 0), 100)
                      const wColor = wPct > 50 ? '#63db91' : wPct > 20 ? '#f0c674' : '#ff968a'
                      return (
                        <div className="quota-meter-row" key={`${meter.label}-${window.kind}-${index}`}>
                          <div className="quota-label">
                            <span>{window.kind === 'other' ? 'Quota' : window.kind}</span>
                            <strong>{window.percent}% left{window.resetAt ? ` · resets ${date(window.resetAt)}` : ''}</strong>
                          </div>
                          <div className="quota-track">
                            <div style={{ width: `${wPct}%`, background: wColor }}/>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                ))}
              </div>
            )}
            {adapter.quota.extra && !adapter.quota.extra.meters && (
              <div className="quota-breakdown">
                <div>
                  Daily:{' '}
                  {adapter.quota.extra.dailyPercent !== undefined ? (
                    <><strong>{adapter.quota.extra.dailyPercent}% left</strong> {adapter.quota.extra.dailyResetAt && <span>· resets {date(adapter.quota.extra.dailyResetAt)}</span>}</>
                  ) : (
                    <span>Not reported</span>
                  )}
                </div>
                <div>
                  Weekly:{' '}
                  {adapter.quota.extra.weeklyPercent !== undefined ? (
                    <><strong>{adapter.quota.extra.weeklyPercent}% left</strong> {adapter.quota.extra.weeklyResetAt && <span>· resets {date(adapter.quota.extra.weeklyResetAt)}</span>}</>
                  ) : (
                    <span>Not reported</span>
                  )}
                </div>
                {adapter.quota.extra.overageUsd !== undefined && (
                  <div>Overage balance: ${adapter.quota.extra.overageUsd.toFixed(2)}</div>
                )}
              </div>
            )}
            {pct === null && <p className="quota-help">Numeric percentage is only shown when the provider CLI reports it.</p>}
            <dl>
              <div><dt>Last checked</dt><dd>{date(adapter.quota.observedAt)}</dd></div>
              <div><dt>Reset</dt><dd>{adapter.quota.resetAt ? date(adapter.quota.resetAt) : 'Not reported'}</dd></div>
            </dl>
          </article>
        )
      })}
    </div>
  )
}

function CapsulesView({ capsules, onResume }: { capsules: Capsule[]; onResume: (path: string) => void }) {
  const [detail, setDetail] = useState<Capsule | null>(null)
  return (
    <section className="panel">
      <div className="section-title">
        <div><span className="eyebrow">Context handoffs</span><h3>Session capsules</h3></div>
        <span>{capsules.length} saved</span>
      </div>
      {capsules.length ? (
        <div className="capsule-list">
          {capsules.map(item => (
            <article key={item.path}>
              <Archive size={18}/>
              <div><strong>{item.task}</strong><span>{item.adapter} · {item.triggered_by} · {date(item.created_at)}</span></div>
              <div className="capsule-actions">
                <button className="icon-button small" onClick={() => setDetail(item)} title="View details"><Eye size={15}/></button>
                <button className="secondary" onClick={() => onResume(item.path)}>Resume</button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <Empty title="No capsules yet" text="Completed and interrupted tasks will appear here for seamless handoffs."/>
      )}
      {detail && (
        <div className="modal" onClick={() => setDetail(null)}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <div className="modal-head">
              <h3>{detail.task}</h3>
              <button className="icon-button" onClick={() => setDetail(null)} aria-label="Close"><X size={16}/></button>
            </div>
            <div className="modal-body">
              <p><strong>Adapter:</strong> {detail.adapter}</p>
              <p><strong>Triggered by:</strong> {detail.triggered_by}</p>
              <p><strong>Created:</strong> {date(detail.created_at)}</p>
              <p className="muted break-all"><strong>Path:</strong> {detail.path}</p>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

function SettingsView({ pushToast }: { pushToast: (m: string, t?: 'success' | 'error') => void }) {
  const [content, setContent] = useState('')
  const [path, setPath] = useState('')
  const [original, setOriginal] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    api<{ content: string; path: string }>('/api/config')
      .then(data => { setContent(data.content); setOriginal(data.content); setPath(data.path) })
      .catch(error => pushToast(error.message, 'error'))
  }, [])
  async function save() {
    setSaving(true)
    try {
      await api('/api/config', { method: 'PUT', body: JSON.stringify({ content }) })
      setOriginal(content)
      pushToast('Configuration saved')
    } catch (error) {
      pushToast(error instanceof Error ? error.message : 'Unable to save', 'error')
    } finally {
      setSaving(false)
    }
  }
  const dirty = content !== original
  return (
    <section className="panel settings-panel">
      <div className="section-title">
        <div><span className="eyebrow">Router policy</span><h3>Configuration</h3></div>
        <div className="settings-actions">
          {dirty && <span className="dirty-badge">Unsaved changes</span>}
          <button className="primary" onClick={() => void save()} disabled={!dirty || saving}>
            {saving ? <RefreshCw size={16} className="spin"/> : <Save size={16}/>}
            Save
          </button>
        </div>
      </div>
      <p className="muted">{path}</p>
      <textarea className="code-editor" value={content} onChange={event => setContent(event.target.value)} spellCheck={false} placeholder="[policy]\ndaily_cap_usd = 5.0"/>
    </section>
  )
}

const SECRET_PRESETS: { label: string; key: string }[] = [
  { label: 'Perplexity Cookie Key', key: 'PERPLEXITY_SESSION_COOKIE' },
  { label: 'Perplexity Session Cookie (pplx)', key: 'PERPLEXITY_PPLX_SESSION' },
  { label: 'Zed Session Cookie', key: 'ZED_SESSION_COOKIE' },
  { label: 'Zed Editor Token', key: 'ZED_EDITOR_TOKEN' },
  { label: 'Amp Session Cookie', key: 'AMP_SESSION_COOKIE' },
  { label: 'OpenRouter API Key', key: 'OPENROUTER_API_KEY' },
  { label: 'DeepSeek API Key', key: 'DEEPSEEK_API_KEY' },
  { label: 'xAI API Key', key: 'XAI_API_KEY' },
  { label: 'Gemini API Key', key: 'GEMINI_API_KEY' },
  { label: 'Kimi API Key', key: 'KIMI_API_KEY' },
  { label: 'MiniMax API Key', key: 'MINIMAX_API_KEY' },
  { label: 'Z.ai API Key', key: 'ZAI_API_KEY' },
  { label: 'Amp API Key', key: 'AMP_API_KEY' },
]

function SecretsView({ pushToast }: { pushToast: (m: string, t?: 'success' | 'error') => void }) {
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const data = await api<{ secrets: Record<string, string> }>('/api/secrets')
      setSecrets(data.secrets)
    } catch (error) {
      pushToast(error instanceof Error ? error.message : 'Unable to load secrets', 'error')
    }
  }, [pushToast])

  useEffect(() => { void refresh() }, [refresh])

  async function saveSecret(event: FormEvent) {
    event.preventDefault()
    if (!name || !value) return
    setLoading(true)
    try {
      await api('/api/secrets', { method: 'PUT', body: JSON.stringify({ name, value }) })
      pushToast(`${name} saved`)
      setName('')
      setValue('')
      await refresh()
    } catch (error) {
      pushToast(error instanceof Error ? error.message : 'Unable to save secret', 'error')
    } finally {
      setLoading(false)
    }
  }

  async function deleteSecret(key: string) {
    if (!confirm(`Delete ${key}?`)) return
    try {
      await api(`/api/secrets/${key}`, { method: 'DELETE' })
      pushToast(`${key} deleted`)
      await refresh()
    } catch (error) {
      pushToast(error instanceof Error ? error.message : 'Unable to delete secret', 'error')
    }
  }

  return (
    <section className="panel secrets-panel">
      <div className="section-title">
        <div><span className="eyebrow">Local-only storage</span><h3>Secrets</h3></div>
      </div>
      <p className="muted">Secrets are stored in <code>~/.config/router/secrets.json</code> with 0600 permissions and loaded into environment variables for adapters.</p>
      <form className="secret-form" onSubmit={saveSecret}>
        <label htmlFor="secret-preset">Storage key</label>
        <select id="secret-preset" value=""
                onChange={event => { if (event.target.value) setName(event.target.value) }}>
          <option value="">Pick a provider key…</option>
          {SECRET_PRESETS.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
        </select>
        <label htmlFor="secret-name">Name</label>
        <input id="secret-name" value={name} onChange={event => setName(event.target.value)} placeholder="DEVIN_API_KEY" required/>
        <label htmlFor="secret-value">Value</label>
        <input id="secret-value" type="password" value={value} onChange={event => setValue(event.target.value)} placeholder="cog-..." required/>
        <button className="primary" disabled={loading || !name || !value}>
          {loading ? <RefreshCw size={16} className="spin"/> : <Save size={16}/>}
          Save secret
        </button>
      </form>
      <div className="secret-list">
        {Object.keys(secrets).length === 0 && <Empty title="No secrets saved" text="Add OPENROUTER_API_KEY above. Devin Desktop quota is read automatically from its local credentials."/>}
        {Object.entries(secrets).map(([key, masked]) => (
          <div key={key} className="secret-row">
            <div><strong>{key}</strong><span>{masked}</span></div>
            <button className="icon-button" onClick={() => void deleteSecret(key)} title="Delete"><Trash2 size={16}/></button>
          </div>
        ))}
      </div>
    </section>
  )
}

function RunTable({ runs }: { runs: Run[] }) {
  return runs.length ? (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Task</th><th>Route</th><th>Class</th><th>Cost</th><th>When</th></tr></thead>
        <tbody>
          {runs.map((run, index) => (
            <tr key={`${run.timestamp}-${index}`}>
              <td>{run.task}</td>
              <td><strong>{run.adapter}</strong><span>{run.model}</span></td>
              <td><span className="tag">{run.taskClass}</span></td>
              <td>{money(run.cost)}</td>
              <td>{date(run.timestamp)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : <Empty title="No runs yet" text="Your first routed task will appear here."/>
}

function ProviderLine({ adapter }: { adapter: Adapter }) {
  return (
    <div className="provider-line">
      <span className={`status-dot ${adapter.reachable ? adapter.quota.state : 'offline'}`}/>
      <div>
        <strong>{adapter.name} <UntestedPill name={adapter.name}/></strong>
        <span>{adapter.quota.remainingPercent === null ? adapter.quota.state : `${adapter.quota.remainingPercent}% left`}</span>
      </div>
      <span>{adapter.kind}</span>
    </div>
  )
}

function Status({ state }: { state: string }) {
  return <span className={`status ${state}`}>{state}</span>
}

function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty">
      <Archive size={25}/>
      <strong>{title}</strong>
      <span>{text}</span>
    </div>
  )
}

export default App
