import React, { useEffect, useState } from 'react'
import {
  BarChart3, BookOpen, BrainCircuit, Check, ChevronDown, ClipboardCheck, Database, FileText, FileUp, FolderOpen, GitBranch,
  LayoutDashboard, ListChecks, Network, Play, Settings2, ShieldCheck, Sparkles, Workflow,
} from 'lucide-react'
import { api } from './api.js'
import ErrorBoundary from './ErrorBoundary.jsx'
import { Button, StatusBanner, SummaryNote } from './ui.jsx'
import ImportPage, { StartScreen } from './pages/ImportPage.jsx'
import ContextPage from './pages/ContextPage.jsx'
import AnalysisPage from './pages/AnalysisPage.jsx'
import MapPage from './pages/MapPage.jsx'
import ReviewPage from './pages/ReviewPage.jsx'
import ReportPage from './pages/ReportPage.jsx'
import InsightsPage from './pages/InsightsPage.jsx'
import WorkflowMapPage from './pages/WorkflowMapPage.jsx'
import ReadoutPage from './pages/ReadoutPage.jsx'
import BusinessMapPage from './pages/BusinessMapPage.jsx'
import { CaseAnalysisPage, CaseStepsPage, QualityPage } from './pages/CasePages.jsx'
import { AnalysesPage, LibraryPage, SettingsPage } from './pages/WorkspacePages.jsx'

const ICONS = { import: FileUp, context: BookOpen, nc_overview: Play, nc_map: GitBranch, nc_steps: ListChecks, nc_report: FileText,
  quality: ShieldCheck, analysis: Play, definition: ListChecks, case_map: GitBranch, report: FileText }
const TOOLS = [['bpm', GitBranch, 'Business process map'], ['readout', BrainCircuit, 'AI Analyst'], ['insights', BarChart3, 'Data Insights'], ['system', Network, 'App workflow map']]
const WORKSPACE = [['library', BookOpen, 'Process library'], ['analyses', FolderOpen, 'Analyses'], ['settings', Settings2, 'Settings']]
const NEEDS_JOB = new Set(['import', 'context', 'nc_overview', 'nc_map', 'nc_steps', 'nc_report', 'quality', 'analysis', 'definition', 'case_map', 'report', 'insights', 'readout'])
const store = { get: k => { try { return localStorage.getItem(k) } catch { return null } }, set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v) } catch { /* ignore */ } } }

export default function App() {
  const [health, setHealth] = useState(null)
  const [jobs, setJobs] = useState([])
  const [jobId, setJobId] = useState(() => store.get('pm_job'))
  const [job, setJob] = useState(null)
  const [page, setPage] = useState(() => store.get('pm_page') || 'analyses')
  const [fresh, setFresh] = useState(null)
  const [error, setError] = useState('')
  const [procs, setProcs] = useState([])
  const [proc, setProc] = useState(() => store.get('pm_process') || '')
  const loadProcs = () => api.processes().then(setProcs).catch(() => {})
  useEffect(() => { api.health().then(setHealth).catch(e => setError(e.message)); api.jobs().then(setJobs).catch(() => {}); loadProcs() }, [])
  const refreshJob = id => api.job(id).then(j => { setJob(j); api.jobs().then(setJobs).catch(() => {}); loadProcs() }).catch(() => { setJob(null); setJobId(null); store.set('pm_job', null) })
  useEffect(() => { if (jobId) refreshJob(jobId); else setJob(null) }, [jobId])
  useEffect(() => { store.set('pm_page', page) }, [page])
  useEffect(() => { store.set('pm_process', proc || null) }, [proc])
  useEffect(() => { if (job && (job.process || '__none__') !== proc) setProc(job.process || '__none__') }, [job?.job_id, job?.process])
  const open = id => { setJobId(id); store.set('pm_job', id); setFresh(null); setPage('import') }
  const select = id => { setJobId(id); store.set('pm_job', id); setFresh(null) }
  const inProc = j => !proc || (proc === '__none__' ? !j.process : j.process === proc)
  const procJobs = jobs.filter(j => !j.broken && inProc(j))
  const unlinked = jobs.some(j => !j.broken && !j.process)
  /** Switch process: open its latest analysis on the same page, or the Process library when it has none yet. */
  const switchProcess = k => {
    if (k === '__new__') { go('start'); return }
    setProc(k)
    const latest = jobs.find(j => !j.broken && (k === '__none__' ? !j.process : j.process === k))
    if (latest) select(latest.job_id)
    else { setJobId(null); store.set('pm_job', null); if (NEEDS_JOB.has(page)) setPage('library') }
  }
  const go = p => { setPage(p); window.scrollTo(0, 0) }
  const steps = job?.steps || []
  // A workflow page of the other mode (e.g. Review steps of a case-ID analysis while a no-case one is open): go to
  // the same place in this analysis's workflow instead of a page that does not exist for it.
  const WORKFLOW = new Set(['import', 'context', 'nc_overview', 'nc_steps', 'nc_map', 'nc_report', 'quality', 'analysis', 'definition', 'case_map', 'report'])
  const SAME = { nc_report: 'report', report: 'nc_report', nc_map: 'case_map', case_map: 'nc_map', nc_steps: 'definition', definition: 'nc_steps', nc_overview: 'analysis', analysis: 'nc_overview', quality: 'nc_overview' }
  useEffect(() => {
    if (!job || !steps.length || !WORKFLOW.has(page) || steps.some(s => s.key === page)) return
    setPage(steps.some(s => s.key === SAME[page]) ? SAME[page] : 'import')
  }, [job?.job_id, page])
  const label = [...steps.map(s => [s.key, s.label]), ...TOOLS.map(t => [t[0], t[2]]), ...WORKSPACE.map(t => [t[0], t[2]]), ['start', 'New analysis']].find(x => x[0] === page)?.[1] || 'Process mining'
  const imported = steps[0]?.done
  function body() {
    if (page === 'start') return <StartScreen onUploaded={r => { loadProcs(); if (r.job.process) setProc(r.job.process); setFresh(r); setJobId(r.job.job_id); store.set('pm_job', r.job.job_id); setJob({ ...r.job, run: null }); setPage('import') }} />
    if (page === 'system') return <WorkflowMapPage job={job} />
    if (page === 'bpm') return <BusinessMapPage job={job} onNav={go} />
    if (page === 'library') return <LibraryPage job={job} processKey={proc && proc !== '__none__' ? proc : ''} onProcess={switchProcess} onOpen={id => { select(id); go('import') }} />
    if (page === 'analyses') return <AnalysesPage current={jobId} onOpen={open} onNew={() => go('start')} onDeleted={id => { if (id === jobId) { setJobId(null); store.set('pm_job', null) } }} />
    if (page === 'settings') return <SettingsPage health={health} />
    if (NEEDS_JOB.has(page) && !job) return <div className="empty-state"><Database size={30} /><h3>No analysis selected</h3><p>Start a new analysis or open one from the list.</p><div className="button-row center"><Button onClick={() => go('start')}><FileUp size={15} /> New analysis</Button><Button variant="outline" onClick={() => go('analyses')}><FolderOpen size={15} /> Analyses</Button></div></div>
    const props = { job, onJob: refreshJob, onNav: go }
    if (WORKFLOW.has(page) && steps.length && !steps.some(s => s.key === page)) return null   // redirecting (see above)
    if (page === 'import') return <ImportPage {...props} fresh={fresh} />
    if (!imported) return <SummaryNote warn>Finish <b>Import data</b> first (map the columns and convert). <button className="text-button" onClick={() => go('import')}>Go to Import data</button></SummaryNote>
    switch (page) {
      case 'context': return <ContextPage {...props} />
      case 'nc_overview': return <AnalysisPage {...props} />
      case 'nc_map': return <MapPage {...props} />
      case 'nc_steps': return <ReviewPage {...props} />
      case 'nc_report': case 'report': return <ReportPage {...props} />
      case 'quality': return <QualityPage {...props} />
      case 'analysis': return <CaseAnalysisPage {...props} />
      case 'definition': return <CaseStepsPage {...props} />
      case 'case_map': return <MapPage {...props} />
      case 'insights': return <InsightsPage {...props} />
      case 'readout': return <ReadoutPage {...props} />
      default: return null
    }
  }
  return <div className="app-shell">
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand"><div className="brand-mark"><Workflow size={22} /></div><div><span>ProcessLens</span><small>PROCESS INTELLIGENCE</small></div></div>
      <div className="sidebar-workspace"><div className="sidebar-small-title">PROCESS</div>
        <label className="workspace-chip select-chip"><div className="workspace-avatar"><BookOpen size={16} /></div>
          <div className="chip-text"><strong>{proc === '__none__' ? 'Not linked to a process' : procs.find(p => p.key === proc)?.name || 'Choose a process'}</strong><small>{procJobs.length} analys{procJobs.length === 1 ? 'is' : 'es'}</small></div><ChevronDown size={14} />
          <select aria-label="Current process" value={proc || ''} onChange={e => switchProcess(e.target.value)}>
            <option value="" disabled>Choose a process</option>{procs.map(p => <option key={p.key} value={p.key}>{p.name}</option>)}
            {unlinked && <option value="__none__">Not linked to a process</option>}<option value="__new__">+ New process (new analysis)…</option>
          </select></label>
        <div className="sidebar-small-title">CURRENT ANALYSIS</div>
        <label className="workspace-chip select-chip"><div className="workspace-avatar"><Database size={16} /></div>
          <div className="chip-text"><strong>{job?.source_name || 'None selected'}</strong><small>{job ? (job.mode ? (job.mode === 'nocase' ? 'No case ID' : 'Case ID') + (job.process_name ? ` · ${job.process_name}` : '') : 'Not imported yet') : 'Open or start one'}</small></div><ChevronDown size={14} />
          <select aria-label="Current analysis" value={jobId || ''} onChange={e => e.target.value === '__new__' ? go('start') : select(e.target.value)}>
            <option value="" disabled>{procJobs.length ? 'Choose an analysis' : 'No analysis of this process yet'}</option>{procJobs.map(j => <option key={j.job_id} value={j.job_id}>{j.source_name} · {String(j.created_at || j.job_id).slice(0, 16).replace('T', ' ')}</option>)}<option value="__new__">+ New analysis…</option>
          </select></label></div>
      <nav className="sidebar-nav" aria-label="Workflow">
        <div className="sidebar-small-title">{job?.mode === 'nocase' ? 'WORKFLOW · NO CASE ID' : job?.mode === 'case' ? 'WORKFLOW · CASE ID' : 'WORKFLOW'}</div>
        {(steps.length ? steps : [{ key: 'import', label: 'Import data' }]).map((s, i) => { const Icon = ICONS[s.key] || LayoutDashboard; return <button key={s.key} className={(page === s.key || (page === 'start' && s.key === 'import') ? 'active' : '') + (s.done ? ' done' : '')} onClick={() => go(job ? s.key : 'start')} title={s.label}>
          <span className="step-num">{s.done ? <Check size={12} /> : i + 1}</span><Icon size={17} /><span>{s.label}</span></button> })}
        <div className="sidebar-small-title spaced">TOOLS</div>
        {TOOLS.map(([id, Icon, l]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => go(id)} title={l}><Icon size={18} /><span>{l}</span></button>)}
        <div className="sidebar-small-title spaced">WORKSPACE</div>
        {WORKSPACE.map(([id, Icon, l]) => <button key={id} className={page === id ? 'active' : ''} onClick={() => go(id)} title={l}><Icon size={18} /><span>{l}</span></button>)}
      </nav>
      <div className="sidebar-bottom"><div className="local-indicator"><span /> LOCAL PROCESSING</div><p>React · FastAPI · same engine as the classic app<br />AI: {health ? (health.ai?.available ? 'available, approve-before-send' : 'not available (rules only)') : '…'}</p></div>
    </aside>
    <div className="main-zone">
      <header className="topbar"><div className="breadcrumb"><span className="topbar-app-label">{job?.process_name || procs.find(p => p.key === proc)?.name || 'ProcessLens'}</span><span>/</span>{job && <><span className="topbar-app-label">{job.source_name}</span><span>/</span></>}<strong>{label}</strong></div>
        <div className="top-actions"><div className="backend-chip"><span className={health ? '' : 'off'} /> {health ? 'API connected' : 'API offline'}</div>
          {job && steps.at(-1)?.done && <a className="header-export" href={`/api/jobs/${job.job_id}/report.pdf`}><FileText size={16} /> Stakeholder PDF</a>}
          <button className="new-data" onClick={() => go('start')}><FileUp size={16} /> New analysis</button></div></header>
      <main className="content">{error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}<ErrorBoundary resetKey={page + (jobId || '')}>{body()}</ErrorBoundary></main>
    </div>
  </div>
}
