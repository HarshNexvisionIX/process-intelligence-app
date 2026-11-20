import React, { useEffect, useRef, useState } from 'react'
import { ArrowRight, BookOpen, CheckCircle2, FileUp, Plus, Send, Sparkles, Trash2 } from 'lucide-react'
import { api } from '../api.js'
import AIPackages from '../components/AIPackages.jsx'
import { Button, DataTable, Field, Loading, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader } from '../ui.jsx'

export default function ContextPage({ job, onJob, onNav }) {
  const [ctx, setCtx] = useState(null)
  const [procs, setProcs] = useState([])
  const [pick, setPick] = useState('')
  const [newName, setNewName] = useState('')
  const [kind, setKind] = useState('sop')
  const [files, setFiles] = useState([])
  const [dated, setDated] = useState('')
  const [roles, setRoles] = useState('')
  const [walk, setWalk] = useState(false)
  const [by, setBy] = useState(() => { try { return localStorage.getItem('pm_reviewer') || '' } catch { return '' } })
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [pkKey, setPkKey] = useState(0)
  const fileInput = useRef(null)
  const load = () => Promise.all([api.context(job.job_id), api.processes()]).then(([c, p]) => { setCtx(c); setProcs(p); setPick(c.process || (p[0]?.key ?? '__new__')) }).catch(e => setError(e.message))
  useEffect(() => { load() }, [job.job_id])
  const act = async (name, fn) => { setBusy(name); setError(''); setMsg(''); try { await fn() } catch (e) { setError(e.message) } finally { setBusy('') } }
  if (!ctx) return <Loading />
  const remember = () => { try { localStorage.setItem('pm_reviewer', by) } catch { /* ignore */ } }
  const usePick = () => act('process', async () => { await api.setProcess(job.job_id, pick === '__new__' ? { new_name: newName } : { key: pick }); setNewName(''); await load(); onJob(job.job_id) })
  const upload = () => act('upload', async () => { const r = await api.addDocuments(job.job_id, files, kind, dated, roles, walk); setFiles([]); if (fileInput.current) fileInput.current.value = ''; setMsg(`Added: ${r.added.join(', ') || 'nothing'}${r.messages.length ? ' · ' + r.messages.join(' · ') : ''}`); await load() })
  const prepare = () => act('prepare', async () => { const r = await api.docsPrepare(job.job_id); setMsg(r.prepared ? `Prepared ${r.prepared} request(s) for review${r.masked ? ' · masked before sending: ' + Object.entries(r.masked).map(([k, v]) => `${v} ${k}`).join(', ') : ''}. Nothing was sent.` : `Nothing prepared: ${r.reason}`); setPkKey(k => k + 1) })
  const send = () => act('send', async () => { const r = await api.docsSend(job.job_id); setMsg(r.ok ? `The AI proposed ${r.stages} stages and ${r.steps} steps: check them in section 3 and approve.` : `No proposal: ${r.reason}`); setPkKey(k => k + 1); await load() })
  const confirm = noDocs => act('confirm', async () => { remember(); await api.confirmContext(job.job_id, { by, no_documents_reason: noDocs ? reason : null }); await load(); onJob(job.job_id) })
  const reopen = () => act('reopen', async () => { await api.reopenContext(job.job_id); await load(); onJob(job.job_id) })
  const active = ctx.documents.filter(d => d.status === 'in use')
  const kindInfo = ctx.kinds.find(k => k.key === kind)
  return <>
    <WorkspaceHeader kicker="STEP 2 · BUSINESS CONTEXT · REQUIRED" title="Business context">Which process is this, and what does the business say about it? Every claim in the documents is checked against the data. Nothing comes from built-in templates.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {msg && <SummaryNote>{msg}</SummaryNote>}
    <Panel title="1 · Which process is this?" subtitle="Analyses of the same process share its documents, steps, rules and review decisions.">
      <div className="inline-form">
        <Field label="Process"><select value={pick} onChange={e => setPick(e.target.value)}>{procs.map(p => <option key={p.key} value={p.key}>{p.name} · {p.steps} steps · {p.rules} rules</option>)}<option value="__new__">New process…</option></select></Field>
        {pick === '__new__' && <Field label="Process name"><input value={newName} onChange={e => setNewName(e.target.value)} placeholder="e.g. ICC month-end recharge" /></Field>}
        <Button onClick={usePick} loading={busy === 'process'} disabled={(pick === '__new__' && !newName.trim()) || pick === ctx.process}>Use this process</Button>
      </div>
      {ctx.process && <p className="input-help">This analysis belongs to <b>{ctx.process_name}</b>.</p>}
    </Panel>
    {ctx.process && <>
      <Panel title="2 · Business documents" subtitle="SOPs, transcripts, decks, recordings, notes, process models, policy tables.">
        <div className="inline-form wrap">
          <Field label="Kind of document"><select value={kind} onChange={e => setKind(e.target.value)}>{ctx.kinds.map(k => <option key={k.key} value={k.key}>{k.label}</option>)}</select></Field>
          <Field label={`Files (${(kindInfo?.extensions || []).join(' ')})`}><input ref={fileInput} type="file" multiple accept={(kindInfo?.extensions || []).join(',')} onChange={e => setFiles(Array.from(e.target.files || []))} /></Field>
          <Field label="Dated / recorded on"><input type="date" value={dated} onChange={e => setDated(e.target.value)} /></Field>
          {['transcript', 'video'].includes(kind) && <Field label="Speaker roles, in order"><input value={roles} onChange={e => setRoles(e.target.value)} placeholder="e.g. ICC lead, SME" /></Field>}
          <Button onClick={upload} loading={busy === 'upload'} disabled={!files.length}><FileUp size={15} /> Add {files.length > 1 ? 'documents' : 'document'}</Button>
        </div>
        {['transcript', 'video'].includes(kind) && <label className="checkbox-row"><input type="checkbox" checked={walk} onChange={e => setWalk(e.target.checked)} /><span>Shows the work being done (walkthrough)</span></label>}
        <DataTable rows={ctx.documents} empty="No documents yet for this process." columns={[{ key: 'id', label: 'ID' }, { key: 'title', label: 'Document' }, { key: 'kind', label: 'Kind' }, { key: 'dated', label: 'Dated' }, { key: 'status', label: 'Status', render: (v, r) => <Tag tone={v === 'in use' ? 'positive' : 'neutral'}>{v}{r.stale ? ' · may be outdated' : ''}</Tag> }]} />
      </Panel>
      {<StagesSteps job={job} ctx={ctx} hasDocs={active.length > 0} by={by} setBy={setBy} pkKey={pkKey}
        prepare={prepare} send={send} busy={busy} onChanged={async () => { await load(); onJob(job.job_id) }} />}
      <Panel title="4 · Confirm" subtitle="Required before the report. The report says which of the two it rests on.">
        {ctx.done ? <div className="done-strip"><CheckCircle2 size={18} /><span>{ctx.business_context.status === 'documents'
          ? `Confirmed by ${ctx.business_context.by} on ${String(ctx.business_context.at).slice(0, 10)}: ${ctx.business_context.documents} document(s).`
          : `Recorded by ${ctx.business_context.by}: no documents (${ctx.business_context.reason}). The report says the process is described from the data only.`}</span>
          <Button variant="outline" onClick={reopen}>Change</Button><Button onClick={() => onNav(ctx.mode === 'nocase' ? 'nc_overview' : 'quality')} disabled={ctx.mode === 'nocase' && !(ctx.pack?.approved || []).length}>Next <ArrowRight size={15} /></Button></div>
          : <>
            <div className="inline-form"><Field label="Reviewed by"><input value={by} onChange={e => setBy(e.target.value)} placeholder="Your name" /></Field>
              <Button onClick={() => confirm(false)} disabled={!by.trim() || !active.length} loading={busy === 'confirm'}><CheckCircle2 size={15} /> Confirm business context</Button></div>
            <details className="plain-details"><summary>No documents are available for this process</summary>
              <div className="inline-form"><Field label="Why not?"><input value={reason} onChange={e => setReason(e.target.value)} placeholder="e.g. no SOP exists; SME interview planned" /></Field>
                <Button variant="outline" onClick={() => confirm(true)} disabled={!by.trim() || !reason.trim()}>Record: no documents</Button></div></details>
          </>}
      </Panel>
    </>}
  </>
}

/** No case ID: the stages and steps of the process, approved here before the analysis. The AI may propose them from
 *  the documents (approve-before-send); the reviewer edits the table and approves it. */
function StagesSteps({ job, ctx, hasDocs, by, setBy, pkKey, prepare, send, busy, onChanged }) {
  const pk = ctx.pack || {}
  const source = pk.proposed ? 'proposed' : 'approved'
  const [rows, setRows] = useState([])
  const [editing, setEditing] = useState(false)
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { setRows((pk.proposed || pk.approved || []).map(r => ({ ...r }))); setEditing(Boolean(pk.proposed) || !(pk.approved || []).length) }, [ctx])
  const approved = (pk.approved || []).length > 0
  const set = (i, k, v) => setRows(rs => rs.map((r, j) => j === i ? { ...r, [k]: v } : r))
  async function approve() {
    setSaving(true); setErr('')
    const stages = []
    for (const r of rows.filter(r => r.step.trim())) {
      let st = stages.at(-1)
      if (!st || st.name !== r.stage.trim()) { st = { name: r.stage.trim(), steps: [] }; stages.push(st) }
      st.steps.push({ name: r.step.trim(), description: r.description || '' })
    }
    try { try { localStorage.setItem('pm_reviewer', by) } catch { /* ignore */ }
      await api.approvePack(ctx.process, { by, stages }); setEditing(false); await onChanged() }
    catch (e) { setErr(e.message) } finally { setSaving(false) }
  }
  return <Panel title={`3 · Stages and steps (${ctx.mode === 'nocase' ? 'required before the analysis' : 'the intended flow'})`}
    subtitle={ctx.mode === 'nocase' ? 'The analysis links every screen to one of these steps, and the map draws them in this order. Propose them from the documents with AI, or type them from the SOP; then approve.'
      : 'On Review steps every activity of the data is linked to one of these steps, and every case is compared with this order. Propose them from the documents with AI, or type them from the SOP; then approve.'}>
    {approved && !editing ? <>
      <div className="done-strip"><CheckCircle2 size={18} /><span>{pk.approved.length} steps approved ({pk.provenance?.label}).</span><Button variant="outline" onClick={() => setEditing(true)}>Edit</Button></div>
      <DataTable rows={pk.approved.map((r, i) => ({ n: i + 1, ...r }))} max={100} columns={[{ key: 'n', label: '#' }, { key: 'stage', label: 'Stage (Level 2)' }, { key: 'step', label: 'Step (Level 3)' }, { key: 'description', label: 'Description' }]} />
    </> : <>
      {hasDocs && <div className="ai-steps-box">
        <div className="button-row"><Button variant="outline" onClick={prepare} loading={busy === 'prepare'}><Sparkles size={15} /> 1. Prepare the AI request</Button>
          <a className="text-button" href="#docs-request">2. Review and approve it below</a>
          <Button variant="outline" onClick={send} loading={busy === 'send'}><Send size={15} /> 3. Send: AI proposes the steps</Button></div>
        <p className="input-help">The AI reads the masked document text only. Its proposal fills the table below for you to check and edit.</p></div>}
      {source === 'proposed' && <SummaryNote>The AI proposed these stages and steps from the documents. Check every row, edit what is wrong, then approve.</SummaryNote>}
      {!rows.length && <SummaryNote warn>No stages and steps yet. {hasDocs ? 'Let the AI propose them (above), or type' : 'Type'} them from the SOP: one row per step, in business order.</SummaryNote>}
      <div className="table-wrap"><table><thead><tr><th>#</th><th>Stage (Level 2)</th><th>Step (Level 3)</th><th>Description</th><th /></tr></thead><tbody>
        {rows.map((r, i) => <tr key={i}><td>{i + 1}</td>{['stage', 'step', 'description'].map(k => <td key={k}><input value={r[k] || ''} onChange={e => set(i, k, e.target.value)} /></td>)}
          <td><button className="icon-button" onClick={() => setRows(rs => rs.filter((_, j) => j !== i))} aria-label="Remove"><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>
      <div className="button-row"><Button variant="outline" onClick={() => setRows(rs => [...rs, { stage: rs.at(-1)?.stage || '', step: '', description: '' }])}><Plus size={15} /> Add step</Button>
        <input className="inline-input" value={by} onChange={e => setBy(e.target.value)} placeholder="Approved by (your name)" />
        <Button onClick={approve} loading={saving} disabled={!by.trim() || !rows.some(r => r.step.trim())}><CheckCircle2 size={15} /> Approve stages and steps</Button>
        {approved && <Button variant="outline" onClick={() => { setEditing(false); setRows(pk.approved) }}>Cancel</Button>}</div>
      {err && <StatusBanner onClose={() => setErr('')}>{err}</StatusBanner>}
      {hasDocs && <div id="docs-request"><AIPackages jobId={job.job_id} kind="docs" refreshKey={pkKey} compact /></div>}
    </>}
  </Panel>
}
