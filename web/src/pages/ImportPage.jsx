import React, { useEffect, useRef, useState } from 'react'
import { AppWindow, ArrowDownToLine, ArrowRight, CheckCircle2, Database, FileUp, GitBranch, Save, ShieldCheck, Sparkles, Upload, Workflow } from 'lucide-react'
import { api } from '../api.js'
import { Button, DataTable, Field, Loading, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader, fmt } from '../ui.jsx'

const TZ = ['UTC', 'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Dubai', 'Africa/Johannesburg', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney']
const NC_MAIN = ['ts', 'ts_end', 'actor', 'app', 'screen', 'action', 'text', 'active_s', 'case_id']
const NC_MORE = ['wait_s', 'l1', 'l2', 'l3', 'l4', 'l5', 'run_id', 'session_id']

/** Step 0 of every analysis: which process is this data about? An existing process, or a new one with a unique name. */
export function ProcessChooser({ value, onChange, disabled, onTaken }) {
  const [list, setList] = useState(null)
  useEffect(() => { api.processes().then(l => { setList(l); if (!value.process && !value.new_process && l.length) onChange({ process: l[0].key, new_process: '' }) }).catch(() => setList([])) }, [])
  const isNew = value.process === '__new__' || (list && !list.length)
  const taken = isNew && value.new_process.trim() && (list || []).find(p => norm(p.name) === norm(value.new_process) || p.key === norm(value.new_process))
  useEffect(() => { onTaken?.(!!taken) }, [!!taken])
  return <div className="process-chooser">
    <div className="workspace-kicker">1 · WHICH PROCESS IS THIS DATA ABOUT? (REQUIRED)</div>
    <div className="inline-form">
      <Field label="Process"><select value={isNew ? '__new__' : value.process} disabled={disabled} onChange={e => onChange({ process: e.target.value, new_process: '' })}>
        {(list || []).map(p => <option key={p.key} value={p.key}>{p.name} · {p.steps} steps</option>)}<option value="__new__">+ New process…</option></select></Field>
      {isNew && <Field label="New process name (unique)"><input value={value.new_process} disabled={disabled} onChange={e => onChange({ process: '__new__', new_process: e.target.value })} placeholder="e.g. ICC month-end recharge" /></Field>}
    </div>
    {taken && <StatusBanner>A process named “{taken.name}” already exists. Choose it from the list, or enter a different name.</StatusBanner>}
    <p className="input-help">Every step of this analysis, its reports, readout, evidence and the AI requests are recorded under this process in the Process library.</p>
  </div>
}
const norm = t => String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
export const processReady = (v, list) => v.process && v.process !== '__new__' ? true : !!norm(v.new_process)
const procBody = v => v.process && v.process !== '__new__' ? { process: v.process } : { new_process: v.new_process.trim() }

export function StartScreen({ onUploaded }) {
  const [proc, setProc] = useState({ process: '', new_process: '' })
  const [taken, setTaken] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [cls, setCls] = useState('client')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const input = useRef(null)
  const [path, setPath] = useState('')
  async function fromPath() {
    setBusy(true); setError('')
    try { if (taken) return; if (!processReady(proc)) throw new Error('Choose the process first (or enter a new process name).'); onUploaded(await api.fromPath(path, cls, procBody(proc))) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  async function handle(files) {
    const list = Array.from(files || [])
    if (!list.length) return
    setBusy(true); setError('')
    try { if (taken) return; if (!processReady(proc)) throw new Error('Choose the process first (or enter a new process name).'); const r = await api.upload(list, cls, procBody(proc)); onUploaded(r) } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return <div className="start-screen">
    <div className="hero-eyebrow"><span /> PROCESS MINING · WITH OR WITHOUT A CASE ID</div>
    <h1>From desktop events to<br /><em>a checked process map.</em></h1>
    <p>Upload an event export (Parquet, CSV or Excel). The app finds out whether it has a real case ID, maps the columns with you, and every number in the report is checked twice. AI only helps where you approve exactly what it receives.</p>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    <ProcessChooser value={proc} onChange={setProc} disabled={busy} onTaken={setTaken} />
    <div className="workspace-kicker">2 · WHAT KIND OF DATA IS IT?</div>
    <div className="class-choice">
      {[['client', 'Client data', 'AI only through private providers (or none)'], ['public', 'Public / test data', 'Any AI provider allowed in Settings']].map(([v, l, h]) =>
        <label key={v} className={'class-card' + (cls === v ? ' active' : '')}><input type="radio" name="cls" checked={cls === v} onChange={() => setCls(v)} /><div><strong>{l}</strong><small>{h}</small></div></label>)}
    </div>
    <div className={'upload-box ' + (dragging ? 'dragging' : '')} onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)}
      onDrop={e => { e.preventDefault(); setDragging(false); handle(e.dataTransfer.files) }}>
      <div className="upload-icon"><FileUp size={27} /></div><h3>3 · Drop event files to start</h3>
      <span>One file, or several files of the same layout · .parquet · .csv · .xlsx</span>
      <input ref={input} type="file" accept=".parquet,.csv,.xlsx,.xls,.tsv,.txt" multiple hidden onChange={e => handle(e.target.files)} />
      <Button onClick={() => input.current.click()} loading={busy}><FileUp size={16} /> Choose files</Button>
    </div>
    <div className="demo-divider"><span>OR READ FILES WHERE THEY ARE</span></div>
    <div className="path-box"><input value={path} onChange={e => setPath(e.target.value)} placeholder="D:\\exports\\events.parquet  or  D:\\exports  or  D:\\exports\\events_*.parquet" />
      <Button variant="outline" onClick={fromPath} disabled={!path.trim()} loading={busy}><FileUp size={16} /> Use files on this computer</Button></div>
    <p className="input-help">Best for files over ~1 GB: one file, a folder (all files read as one log) or a pattern. The data is read in place and never copied or changed.</p>
    <div className="feature-trio">
      <div><ShieldCheck size={20} /><strong>Local processing</strong><span>Raw events stay in your data folder. Users and case IDs are stored as salted hashes.</span></div>
      <div><GitBranch size={20} /><strong>Two workflows</strong><span>Case-ID process mining, or clickstream mapping when there is no case ID.</span></div>
      <div><Sparkles size={20} /><strong>Approve-before-send AI</strong><span>Every AI request is shown as JSON with a SHA-256 before it may be sent.</span></div>
    </div>
  </div>
}

function DetectionBanner({ det, workflow, setWorkflow }) {
  return <div className={'detect-banner ' + (det.workflow === 'nocase' ? 'nocase' : 'case')}>
    <div className="detect-icon">{det.workflow === 'nocase' ? <Workflow size={22} /> : <GitBranch size={22} />}</div>
    <div><div className="workspace-kicker">AUTOMATIC CHECK</div><h3>{det.workflow === 'nocase' ? 'No case ID found: clickstream mapping' : `Case ID found${det.column ? `: ${det.column}` : ''}`}</h3><p>{det.message}</p></div>
    <div className="pill-tabs small"><button className={workflow === 'case' ? 'active' : ''} onClick={() => setWorkflow('case')}><GitBranch size={15} /> Case ID</button><button className={workflow === 'nocase' ? 'active' : ''} onClick={() => setWorkflow('nocase')}><Workflow size={15} /> No case ID</button></div>
  </div>
}

/** Real names for MASKED application values (hashes, IDs, numbers). Shown only when the chosen application column
 *  has masked values. Names go into one shared map (all datasets) and are applied when you convert. */
function AppNamesPanel({ job, column, title, user, hintLabel }) {
  const [d, setD] = useState(null)
  const [rows, setRows] = useState([])
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [preview, setPreview] = useState(null)
  const fileRef = useRef(null)
  const load = (first = false) => api.appNames(job.job_id, column, title, user).then(r => { setD(r); setRows(r.masked.map(x => ({ ...x }))); if (first) setOpen(r.summary.mapped < r.summary.masked) }).catch(e => setError(e.message))
  useEffect(() => { setD(null); setMsg(''); setPreview(null); if (column) load(true) }, [job.job_id, column, title, user])
  if (!column || !d || !d.summary.masked) return null           // nothing masked: nothing to ask
  const s = d.summary
  const left = rows.filter(r => !String(r.real_name || '').trim()).length
  const set = (i, k, v) => setRows(rs => rs.map((r, j) => j === i ? { ...r, [k]: v } : r))
  const save = async () => { setBusy(true); setError(''); try { const c = await api.saveAppNames(rows); setMsg(`Saved: ${c.added} added, ${c.updated} updated${c.removed ? `, ${c.removed} removed` : ''}. The names are used when you convert${job.mode ? ' (convert again to apply them to this analysis)' : ''}.`); await load() } catch (e) { setError(e.message) } finally { setBusy(false) } }
  const pick = async f => { if (!f) return; setError(''); try { setPreview({ file: f, ...(await api.importAppNames(f, false)) }) } catch (e) { setError(e.message) } }
  const confirm = async () => { setBusy(true); try { await api.importAppNames(preview.file, true); setPreview(null); setMsg('Import saved.'); await load() } catch (e) { setError(e.message) } finally { setBusy(false) } }
  return <div className={"app-names" + (s.mapped < s.masked ? "" : " named")}>
    <div className="app-names-head"><AppWindow size={17} /><div><b>{s.mapped < s.masked ? 'Application names needed' : 'Application names'}</b>
      <span>{s.masked} of {s.applications} application values in “{column}” are masked (hashes, IDs or numbers){s.mapped ? `; ${s.mapped} already named` : ''}. {left ? `${Math.round(100 * s.unnamed_share)}% of events would show as “Unnamed app 1, 2…” until named.` : 'All of them have a name.'}</span></div>
      <button className="text-button" onClick={() => setOpen(o => !o)}>{open ? 'Hide' : left ? 'Name them' : 'Edit names'}</button></div>
    {open && <>
      <p className="input-help">Give each masked value its real name once. Names are kept in one shared map for every dataset and applied when you convert. The masked values and window titles are only shown here; they never reach the converted data, reports or the AI.</p>
      <div className="table-wrap"><table className="app-names-table"><thead><tr><th>Recorded value</th><th>Events</th><th>Users</th><th>{hintLabel || 'Example window titles'}</th><th>Real name</th><th>Category</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={r.raw_value} className={String(r.real_name || '').trim() ? '' : 'todo'}>
          <td title={r.raw_value}><code>{r.raw_value.slice(0, 10)}…</code></td><td>{fmt(r.events)}</td><td>{r.users != null ? fmt(r.users) : '–'}</td>
          <td className="examples">{r.examples || '–'}</td>
          <td><input value={r.real_name || ''} placeholder="e.g. Workday" onChange={e => set(i, 'real_name', e.target.value)} /></td>
          <td><select value={r.category || ''} onChange={e => set(i, 'category', e.target.value)}><option value="">—</option>{[...new Set([...d.categories, r.category].filter(Boolean))].map(c => <option key={c}>{c}</option>)}</select></td></tr>)}</tbody></table></div>
      {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
      {msg && <SummaryNote>{msg}</SummaryNote>}
      {preview && <SummaryNote>This file would add {preview.added}, update {preview.updated} and leave {preview.unchanged} unchanged; {preview.ignored} rows ignored (no value or no name). <Button onClick={confirm} loading={busy}>Confirm import</Button> <button className="text-button" onClick={() => setPreview(null)}>Cancel</button></SummaryNote>}
      <div className="button-row"><Button onClick={save} loading={busy}><Save size={15} /> Save names</Button>
        <a className="btn outline" href="/api/apps/map.csv"><ArrowDownToLine size={15} /> Download map (CSV)</a>
        <Button variant="outline" onClick={() => fileRef.current?.click()}><Upload size={15} /> Import map (CSV / Excel)</Button>
        <input ref={fileRef} type="file" accept=".csv,.xlsx" hidden onChange={e => { pick(e.target.files[0]); e.target.value = '' }} /></div>
    </>}
  </div>
}

function NocaseForm({ prof, job, onDone }) {
  const sug = prof.nocase.suggested || {}
  const [cols, setCols] = useState(() => Object.fromEntries(prof.nocase.standard_columns.map(k => [k, sug[k] || ''])))
  const [fmtTs, setFmtTs] = useState('detect')
  const [tz, setTz] = useState('UTC')
  const [cls, setCls] = useState(job.dataset_class || 'client')
  const [more, setMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const names = prof.columns.map(c => c.name)
  async function convert() {
    setBusy(true); setError('')
    try { const r = await api.convertNocase(job.job_id, { columns: cols, ts_format: fmtTs, timezone: tz, dataset_class: cls }); onDone(r) }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const sel = k => <Field key={k} label={`${k}${k === 'ts' ? ' *' : ''}`} help={prof.nocase.help[k]}>
    <select value={cols[k] || ''} onChange={e => setCols(s => ({ ...s, [k]: e.target.value }))}><option value="">— none —</option>{names.map(n => <option key={n}>{n}</option>)}</select>
    {sug[k] && cols[k] === sug[k] && <Tag tone="positive">suggested</Tag>}</Field>
  return <Panel title="Map the columns (no case ID)" subtitle="Only time is required. Map what the export has; suggestions come from the column names and contents. You are the final decision.">
    <div className="mapping-grid">{NC_MAIN.map(sel)}</div>
    <button className="text-button" onClick={() => setMore(m => !m)}>{more ? 'Hide' : 'Show'} more columns (levels, wait time, run and session IDs)</button>
    {more && <div className="mapping-grid" style={{ marginTop: 12 }}>{NC_MORE.map(sel)}</div>}
    <AppNamesPanel job={job} column={cols.app} title={cols.screen} user={cols.actor} hintLabel="Example screens" />
    <div className="mapping-grid" style={{ marginTop: 18 }}>
      <Field label="Time format"><select value={fmtTs} onChange={e => setFmtTs(e.target.value)}>{prof.nocase.time_formats.map(f => <option key={f}>{f}</option>)}</select></Field>
      <Field label="Timezone of times without an offset"><select value={tz} onChange={e => setTz(e.target.value)}>{TZ.map(t => <option key={t}>{t}</option>)}</select></Field>
      <Field label="Dataset class"><select value={cls} onChange={e => setCls(e.target.value)}><option value="client">client · private AI providers only</option><option value="public">public · any allowed provider</option></select></Field>
    </div>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    <div className="save-config"><SummaryNote>Converting writes the standard events, cleans the screen names and builds the units (application + screen + click). Users are stored as salted hashes.</SummaryNote>
      <Button onClick={convert} loading={busy} disabled={!cols.ts}><Database size={16} /> Convert</Button></div>
  </Panel>
}

function CaseForm({ prof, job, onDone }) {
  const s = prof.case.suggested
  const [map, setMap] = useState(() => Object.fromEntries(prof.case.roles.map(r => [r, s[r]?.column || ''])))
  const [tz, setTz] = useState('UTC')
  // Click-level rows (an Action / Level 4 column): one Level 3 step is many rows in a row -> merge them into one run
  const [opts, setOpts] = useState({ drop_missing_case: false, drop_missing_activity: false, merge_repeats: !!s.action?.column })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [checks, setChecks] = useState(null)
  const names = prof.columns.map(c => c.name)
  const levels = s.activity?.reasons?.some(r => /process levels/.test(r))
  async function convert() {
    setBusy(true); setError('')
    try { const r = await api.convertCase(job.job_id, { mapping: map, timezone: tz, ...opts }); setChecks(r.checks); onDone(r) }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  return <Panel title="Map the columns (case ID)" subtitle="Case ID, activity and timestamp are required. Each suggestion shows its confidence. For a derived case ID (from text, sessions, a records export) use the classic app's Import page.">
    {levels && <SummaryNote>This file already has the process levels: <b>Level 1</b> {map.level1 ? `“${map.level1}”` : ''} is the process, <b>Level 2</b> “{map.stage}” the stage and <b>Level 3</b> “{map.activity}” the step (Activity){map.action ? <>; “{map.action}” holds the clicks inside each step (<b>Level 4</b>), so repeated rows of one step are merged into one step run</> : ''}. The map and the comparison with your documents use these levels.</SummaryNote>}
    <div className="mapping-grid">{prof.case.roles.map(r => <Field key={r} label={prof.case.labels[r] + (prof.case.required.includes(r) ? ' *' : '')}
      help={s[r]?.column && map[r] === s[r].column ? `${Math.round(100 * s[r].confidence)}% · ${(s[r].reasons || []).join('; ')}` : null}>
      <select value={map[r] || ''} onChange={e => setMap(m => ({ ...m, [r]: e.target.value }))}><option value="">— none —</option>{names.map(n => <option key={n}>{n}</option>)}</select></Field>)}</div>
    <AppNamesPanel job={job} column={map.application} title={map.window_title || map.activity} user={map.resource} hintLabel={map.window_title ? 'Example window titles' : 'Example activities'} />
    <div className="mapping-grid" style={{ marginTop: 16 }}>
      <Field label="Timezone the times were recorded in"><select value={tz} onChange={e => setTz(e.target.value)}>{TZ.map(t => <option key={t}>{t}</option>)}</select></Field>
    </div>
    {[['drop_missing_case', 'Leave out rows without a case ID (unrelated activity, not lost data)'], ['drop_missing_activity', 'Leave out rows without an activity'], ['merge_repeats', 'Merge the same step repeated in a row in a case']].map(([k, l]) =>
      <label className="checkbox-row" key={k}><input type="checkbox" checked={opts[k]} onChange={e => setOpts(o => ({ ...o, [k]: e.target.checked }))} /><span>{l}</span></label>)}
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {checks && <DataTable rows={checks} columns={[{ key: 'ok', label: '', render: v => v ? '✓' : '!' }, { key: 'name', label: 'Check' }, { key: 'message', label: 'Result' }]} />}
    <div className="save-config"><SummaryNote>Validate and convert checks the mapping on every row, then writes the standard event log. Case IDs are stored as salted hashes.</SummaryNote>
      <Button onClick={convert} loading={busy} disabled={!map.case_id || !map.activity || !map.timestamp}><Database size={16} /> Validate and convert</Button></div>
  </Panel>
}

export default function ImportPage({ job, onJob, onNav, fresh }) {
  const [prof, setProf] = useState(fresh?.profile || null)
  const [error, setError] = useState('')
  const [workflow, setWorkflow] = useState(null)
  const [done, setDone] = useState(null)
  useEffect(() => {
    setDone(null)
    if (!job) return
    if (fresh?.profile && fresh.job.job_id === job.job_id) { setProf(fresh.profile); return }
    setProf(null); api.profile(job.job_id).then(setProf).catch(e => setError(e.message))
  }, [job?.job_id])
  useEffect(() => { if (prof) setWorkflow(job?.mode || prof.detection.workflow || 'case') }, [prof])
  if (!job) return null
  const imported = job.steps?.[0]?.done
  return <>
    <WorkspaceHeader kicker="STEP 1 · IMPORT DATA" title="Import data">Check what is in the file, confirm the workflow the app detected, map the columns and convert.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {(imported || done) && <div className="done-strip"><CheckCircle2 size={18} /><span>Imported as <b>{job.mode === 'nocase' ? 'no case ID (clickstream)' : 'case ID'}</b>{done?.scope ? `: ${fmt(done.scope.in_scope)} of ${fmt(done.scope.rows_in_file)} rows in scope` : ''}{done?.result ? `: ${fmt(done.result.events)} events, ${fmt(done.result.cases)} cases` : ''}. You can map again below to replace it.</span>
      <Button onClick={() => onNav('context')}>Next: Business context <ArrowRight size={15} /></Button></div>}
    {!prof ? <Loading>Reading and profiling the file…</Loading> : <>
      <DetectionBanner det={prof.detection} workflow={workflow} setWorkflow={setWorkflow} />
      <div className="metrics-grid four compact"><div className="metric"><div className="metric-label">Rows</div><div className="metric-value">{fmt(prof.rows)}</div></div>
        <div className="metric"><div className="metric-label">Columns</div><div className="metric-value">{prof.columns.length}</div></div>
        <div className="metric"><div className="metric-label">File</div><div className="metric-value small">{job.source_name}</div></div>
        <div className="metric"><div className="metric-label">Dataset class</div><div className="metric-value small">{job.dataset_class}</div></div></div>
      <Panel title="What is in the file" subtitle="Type, missing values, distinct values and examples per column">
        <DataTable rows={prof.columns} max={60} columns={[{ key: 'name', label: 'Column' }, { key: 'type', label: 'Type' }, { key: 'missing_pct', label: 'Missing %', digits: 1 }, { key: 'distinct', label: 'Distinct' }, { key: 'date_format', label: 'Date format' }, { key: 'examples', label: 'Examples', render: v => (v || []).join(' · ') }]} />
      </Panel>
      {workflow === 'nocase' ? <NocaseForm prof={prof} job={job} onDone={r => { setDone(r); onJob(job.job_id) }} /> : <CaseForm prof={prof} job={job} onDone={r => { setDone(r); onJob(job.job_id) }} />}
    </>}
  </>
}
