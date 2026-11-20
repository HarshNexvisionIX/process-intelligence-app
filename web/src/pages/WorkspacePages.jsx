import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, Archive, BookOpen, CheckCircle2, FileText, FolderOpen, GitBranch, History, ListChecks, Plus, Save, Settings2, ShieldCheck, Trash2 } from 'lucide-react'
import { api, bundleUrl, libraryFileUrl, resultUrl } from '../api.js'
import { Button, DataTable, Field, Loading, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, fmt } from '../ui.jsx'
import { CLASSIC_URL } from './CasePages.jsx'
import { BusinessMap } from './BusinessMapPage.jsx'

const flat = pack => (pack?.stages || []).flatMap(s => (s.steps || []).map(st => ({ stage: s.name || '', step: st.name || '', description: st.description || '' })))
function unflat(rows) {
  const out = []
  for (const r of rows.filter(r => r.step.trim())) {
    let s = out.find(x => x.name === r.stage.trim())
    if (!s) { s = { name: r.stage.trim(), steps: [] }; out.push(s) }
    s.steps.push({ name: r.step.trim(), description: r.description })
  }
  return out
}

const kb = n => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`
const when = v => String(v || '').slice(0, 16).replace('T', ' ')

/** Everything recorded for the process: each analysis with its reports, AI answers, the AI requests (JSON) sent,
 *  and the evidence; plus the library's own files. Read live from the analysis folders each time it opens. */
function ProcessRecords({ processKey, onOpen }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState('')
  useEffect(() => { setD(null); api.processRecords(processKey).then(setD).catch(e => setError(e.message)) }, [processKey])
  if (error) return <StatusBanner>{error}</StatusBanner>
  if (!d) return <Loading />
  const groups = files => files.reduce((m, f) => ((m[f.category] = m[f.category] || []).push(f), m), {})
  return <>
    <Panel title={`Everything recorded for ${d.name}`} subtitle="Every analysis linked to this process, with its reports, readout, evidence and the AI requests that were prepared, approved and sent. Event tables and uploaded raw files are not listed."
      action={<a className="btn outline" href={bundleUrl(processKey)}><Archive size={15} /> Download everything (ZIP)</a>}>
      <div className="mini-kpis four"><div><span>Analyses</span><strong>{d.analyses.length}</strong></div>
        <div><span>Result files</span><strong>{d.analyses.reduce((n, a) => n + (a.files || []).length, 0)}</strong></div>
        <div><span>AI requests sent</span><strong>{d.analyses.reduce((n, a) => n + (a.ai?.sent || 0), 0)}</strong></div>
        <div><span>Library files</span><strong>{d.library.length}</strong></div></div>
      {!d.analyses.length && <SummaryNote>No analysis is linked to this process yet. Start one with <b>New analysis</b> and choose this process.</SummaryNote>}
    </Panel>
    {d.analyses.map(a => { const g = groups(a.files || []); return <Panel key={a.job_id} title={a.source_name || a.job_id}
      subtitle={`${when(a.created_at)} · ${a.mode ? (a.mode === 'nocase' ? 'no case ID' : 'case ID') : 'not imported yet'} · ${(a.steps || []).filter(s => s.done).length}/${(a.steps || []).length} steps done · AI: ${a.ai.prepared} prepared, ${a.ai.approved} approved, ${a.ai.sent} sent`}
      action={onOpen && <button className="text-button" onClick={() => onOpen(a.job_id)}><FolderOpen size={14} /> Open this analysis</button>}>
      <span className="progress-dots">{(a.steps || []).map(s => <i key={s.key} className={s.done ? 'on' : ''} title={s.label} />)}</span>
      {a.exports?.length > 0 && <div className="record-group"><h4>Reports and exports</h4><div className="record-links">{a.exports.map(x => <a key={x.url} className="text-button" href={x.url}><FileText size={14} /> {x.label}</a>)}</div></div>}
      {Object.entries(g).map(([cat, fs]) => <div key={cat} className="record-group"><h4>{cat}</h4>
        <div className="table-wrap"><table><tbody>{fs.map(f => <tr key={f.name}><td>{f.label}</td><td><code>{f.name}</code></td><td>{kb(f.bytes)}</td><td>{when(f.modified)}</td>
          <td><a className="text-button" href={resultUrl(a.job_id, f.name)}><ArrowDownToLine size={14} /> Download</a></td></tr>)}</tbody></table></div></div>)}
      {!(a.files || []).length && <SummaryNote>Nothing recorded yet for this analysis.</SummaryNote>}
    </Panel> })}
    <Panel title="Process library files" subtitle="Stages and steps, step rules, business documents, saved versions and decisions">
      <DataTable rows={d.library} max={200} empty="No files yet." columns={[{ key: 'kind', label: 'Kind' }, { key: 'path', label: 'File', render: v => <a className="text-button" href={libraryFileUrl(processKey, v)}>{v}</a> }, { key: 'bytes', label: 'Size', render: kb }, { key: 'modified', label: 'Changed', render: when }]} />
    </Panel>
  </>
}

export function LibraryPage({ job, processKey = '', onProcess, onOpen }) {
  const [list, setList] = useState(null)
  const [key, setKeyState] = useState(processKey || job?.process || '')
  const setKey = k => { setKeyState(k); onProcess?.(k) }
  useEffect(() => { if (processKey && processKey !== key) setKeyState(processKey) }, [processKey])
  const [p, setP] = useState(null)
  const [rows, setRows] = useState([])
  const [tab, setTab] = useState('records')
  const [by, setBy] = useState(() => { try { return localStorage.getItem('pm_reviewer') || '' } catch { return '' } })
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  useEffect(() => { api.processes().then(l => { setList(l); if (!key && l.length) setKeyState(l[0].key) }).catch(e => setError(e.message)) }, [])
  const load = () => key && api.process(key).then(d => { setP(d); setRows(flat(d.pack)) }).catch(e => setError(e.message))
  useEffect(() => { load() }, [key])
  const act = async fn => { setError(''); setMsg(''); try { try { localStorage.setItem('pm_reviewer', by) } catch { /* ignore */ } await fn(); await load() } catch (e) { setError(e.message) } }
  if (!list) return <Loading />
  return <>
    <WorkspaceHeader kicker="WORKSPACE · PROCESS LIBRARY" title="Process library">Everything about each process in one place: its analyses with their reports, readouts, evidence and the AI requests sent; its stages and steps; the rules that recognise each step; and who approved what. Every analysis of the same process reuses them.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}{msg && <SummaryNote>{msg}</SummaryNote>}
    {!list.length ? <SummaryNote>The library is empty. A process is created when you start a new analysis.</SummaryNote> : <>
      <div className="inline-form"><Field label="Process"><select value={key} onChange={e => setKey(e.target.value)}>{list.map(x => <option key={x.key} value={x.key}>{x.name}</option>)}</select></Field>
        <Field label="Your name"><input value={by} onChange={e => setBy(e.target.value)} placeholder="for the audit trail" /></Field></div>
      {p && <>
        <div className="mini-kpis four"><div><span>Stages</span><strong>{(p.pack.stages || []).length}</strong></div><div><span>Steps</span><strong>{rows.length}</strong></div><div><span>Rules</span><strong>{p.rules.length}</strong></div><div><span>Status</span><strong className="small">{p.provenance.label}</strong></div></div>
        <Tabs tabs={[['records', Archive, 'Analyses and records'], ['map', GitBranch, 'Business process map'], ['steps', BookOpen, 'Stages and steps'], ['waiting', ShieldCheck, `Waiting for approval${p.proposed_pack ? ' (1)' : ''}`], ['rules', ListChecks, `Step rules (${p.rules.length})`], ['history', History, 'History']]} value={tab} onChange={setTab} />
        {tab === 'records' && <ProcessRecords processKey={key} onOpen={onOpen} />}
        {tab === 'map' && <BusinessMap processKey={key} defaultJob={job?.job_id} embedded />}
        {tab === 'steps' && <Panel title="Stages and steps, in business order" subtitle="Edit, add or remove rows, then save with your name. Every save keeps the previous version.">
          <div className="table-wrap"><table><thead><tr><th>Stage (Level 2)</th><th>Step (Level 3)</th><th>Description</th><th /></tr></thead><tbody>
            {rows.map((r, i) => <tr key={i}>{['stage', 'step', 'description'].map(k => <td key={k}><input value={r[k]} onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, [k]: e.target.value } : x))} /></td>)}
              <td><button className="icon-button" onClick={() => setRows(rs => rs.filter((_, j) => j !== i))} aria-label="Remove"><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>
          <div className="button-row"><Button variant="outline" onClick={() => setRows(rs => [...rs, { stage: rs.at(-1)?.stage || '', step: '', description: '' }])}><Plus size={15} /> Add step</Button>
            <Button onClick={() => act(async () => { await api.savePack(key, { stages: unflat(rows), by }); setMsg('Saved. The previous version is kept in the history.') })} disabled={!by.trim()}><Save size={15} /> Save changes</Button></div>
        </Panel>}
        {tab === 'waiting' && <Panel title="Proposed from the documents" subtitle="Check the stages and steps the AI proposed, then approve. You can edit them afterwards.">
          {!p.proposed_pack ? <SummaryNote>Nothing is waiting.</SummaryNote> : <>
            <DataTable rows={flat(p.proposed_pack)} max={80} columns={[{ key: 'stage', label: 'Stage' }, { key: 'step', label: 'Step' }, { key: 'description', label: 'Description' }]} />
            <Button onClick={() => act(async () => { await api.approvePack(key, { by }); setMsg('Approved and versioned.'); setTab('steps') })} disabled={!by.trim()}><CheckCircle2 size={15} /> Approve this process</Button></>}
        </Panel>}
        {tab === 'rules' && <Panel title="Step rules" subtitle="Accepted on Review steps; used by every analysis of this process">
          <DataTable rows={p.rules.map(r => ({ id: r.id, step: r.step, app: r.match?.app, screen: r.match?.screen, action: r.match?.action, status: r.status, version: r.version }))} max={300}
            columns={[{ key: 'id', label: 'Rule' }, { key: 'step', label: 'Step' }, { key: 'app', label: 'Application' }, { key: 'screen', label: 'Screen pattern' }, { key: 'action', label: 'Click pattern' }, { key: 'status', label: 'Status', render: v => <Tag tone={v === 'sme_confirmed' ? 'positive' : 'neutral'}>{v}</Tag> }, { key: 'version', label: 'Version' }]} />
        </Panel>}
        {tab === 'history' && <Panel title="Decisions"><DataTable rows={p.decisions} max={200} columns={[{ key: 'when', label: 'When', render: v => String(v || '').slice(0, 19).replace('T', ' ') }, { key: 'who', label: 'Who' }, { key: 'item', label: 'Item' }, { key: 'action', label: 'Action' }, { key: 'comment', label: 'Note' }]} /></Panel>}
      </>}
    </>}
  </>
}

export function AnalysesPage({ current, onOpen, onNew, onDeleted }) {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState('')
  const load = () => api.jobs().then(setRows).catch(e => setError(e.message))
  useEffect(() => { load() }, [])
  return <>
    <WorkspaceHeader kicker="WORKSPACE · ANALYSES" title="Analyses" actions={<Button onClick={onNew}><Plus size={15} /> New analysis</Button>}>Every file you imported, its workflow and how far it is.</WorkspaceHeader>
    {error && <StatusBanner>{error}</StatusBanner>}
    {!rows ? <Loading /> : <Panel><DataTable rows={rows} max={200} empty="No analyses yet." columns={[
      { key: 'source_name', label: 'File', render: (v, r) => <button className="text-button" onClick={() => onOpen(r.job_id)}><FolderOpen size={14} /> {v}</button> },
      { key: 'created_at', label: 'Created', render: v => String(v || '').slice(0, 16).replace('T', ' ') },
      { key: 'mode', label: 'Workflow', render: v => v ? <Tag>{v === 'nocase' ? 'no case ID' : 'case ID'}</Tag> : <Tag tone="warning">not imported</Tag> },
      { key: 'process_name', label: 'Process' }, { key: 'rows', label: 'Rows', render: v => fmt(v) },
      { key: 'steps', label: 'Progress', render: v => <span className="progress-dots">{(v || []).map(s => <i key={s.key} className={s.done ? 'on' : ''} title={s.label} />)}</span> },
      { key: 'job_id', label: '', render: v => <button className="icon-button" title="Move to backup (never deleted)" onClick={async () => { if (window.confirm('Move this analysis to the backup folder?')) { await api.removeJob(v); onDeleted(v); load() } }}><Trash2 size={15} /></button> }]} /></Panel>}
  </>
}

export function SettingsPage({ health }) {
  const ai = health?.ai || {}
  return <>
    <WorkspaceHeader kicker="WORKSPACE · SETTINGS" title="Settings">Read-only here on purpose: AI providers, routes and keys are set in <code>config/ai_providers.yaml</code>, <code>config/settings.yaml</code> and <code>.env</code> (or the classic app's Settings page). No key or model is ever typed into these pages.</WorkspaceHeader>
    <div className="two-panels">
      <Panel title="AI status"><div className="privacy-details">
        <div><span>AI available</span><strong>{ai.available ? 'yes' : 'no'}</strong></div><div><span>Reason</span><strong>{ai.reason || '–'}</strong></div>
        <div><span>Usable providers</span><strong>{(ai.providers || []).join(', ') || 'none'}</strong></div><div><span>Private providers (client data)</span><strong>{(ai.private_providers || []).join(', ') || 'none'}</strong></div></div>
        <SummaryNote>Client data only ever goes to providers marked <code>private: true</code> with their own address. Public data may use any provider allowed in the AI settings. Every request still needs approval on the page where it is prepared.</SummaryNote></Panel>
      <Panel title="Where things are"><div className="privacy-details">
        <div><span>Data folder</span><strong className="small">{health?.data_dir}</strong></div><div><span>API version</span><strong>{health?.version}</strong></div></div>
        <a className="btn outline" href={CLASSIC_URL} target="_blank" rel="noreferrer"><Settings2 size={15} /> Open the classic app (AI settings)</a></Panel>
    </div>
  </>
}
