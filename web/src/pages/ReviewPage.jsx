import React, { useEffect, useMemo, useState } from 'react'
import { ArrowDownUp, ArrowRight, CheckCircle2, GitBranch, Layers3, ListChecks, LoaderCircle, MessageSquareText, Play, Save, XCircle } from 'lucide-react'
import { api } from '../api.js'
import { Button, DataTable, Field, Loading, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, fmt } from '../ui.jsx'

export default function ReviewPage({ job, onJob, onNav }) {
  const [d, setD] = useState(null)
  const [tab, setTab] = useState('rules')
  const [by, setBy] = useState(() => { try { return localStorage.getItem('pm_reviewer') || '' } catch { return '' } })
  const [steps, setSteps] = useState({})
  const [picked, setPicked] = useState({})
  const [assign, setAssign] = useState({})
  const [answers, setAnswers] = useState({})
  const [newQ, setNewQ] = useState('')
  const [reason, setReason] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [filter, setFilter] = useState('')
  const load = () => api.review(job.job_id).then(r => { setD(r); setSteps(Object.fromEntries(r.proposed.map(p => [p.id, p.step]))); setPicked({}); setAssign({}) }).catch(e => setError(e.message))
  useEffect(() => { load() }, [job.job_id])
  const act = async (name, fn) => { setBusy(name); setError(''); setMsg(''); try { try { localStorage.setItem('pm_reviewer', by) } catch { /* ignore */ } await fn(); await load(); onJob(job.job_id) } catch (e) { setError(e.message) } finally { setBusy('') } }
  const units = useMemo(() => (d?.unassigned || []).filter(u => !filter || `${u.app} ${u.screen} ${u.click}`.toLowerCase().includes(filter.toLowerCase())), [d, filter])
  if (error && !d) return <StatusBanner>{error}</StatusBanner>
  if (!d) return <Loading />
  const sel = Object.keys(picked).filter(k => picked[k])
  const open = d.questions.filter(q => !q.answer).length
  const nAssign = Object.values(assign).filter(Boolean).length
  return <>
    <WorkspaceHeader kicker="STEP 4 · REVIEW STEPS · HUMAN DECISION" title="Review steps">Accept or correct how screens are linked to steps, give a step to the largest unlinked work, and answer the questions. Everything you accept is reused by every later analysis of this process.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {msg && <SummaryNote>{msg}</SummaryNote>}
    <div className="review-top">
      <div className="mini-kpis four"><div><span>Waiting rules</span><strong>{d.proposed.length}</strong></div><div><span>Confirmed rules</span><strong>{d.confirmed_rules}</strong></div>
        <div><span>Unlinked units</span><strong>{d.unassigned.length}</strong></div><div><span>Open questions</span><strong>{open}</strong></div></div>
      <Field label="Reviewed by"><input value={by} onChange={e => setBy(e.target.value)} placeholder="Your name" /></Field>
    </div>
    <RerunBar job={job} onNav={onNav} onDone={() => { load(); onJob(job.job_id) }} pending={d.proposed.length} />
    <Tabs tabs={[['rules', ListChecks, `Proposed rules (${d.proposed.length})`], ['units', ListChecks, `Unlinked work (${d.unassigned.length})`], ['activities', Layers3, 'Activities (Level 4)'], ['order', ArrowDownUp, 'Step order'], ['questions', MessageSquareText, `Questions (${open} open)`]]} value={tab} onChange={setTab} />
    {tab === 'rules' && <Panel title="Proposed rules" subtitle="Each rule links one application + screen (+ click) to a step. Change the step where it is wrong, then accept.">
      {!d.proposed.length ? <SummaryNote>No rules are waiting. Run the analysis with the approved AI task “Link screens to steps” to get proposals.</SummaryNote> : <>
        <div className="table-wrap"><table><thead><tr><th><input type="checkbox" checked={sel.length === d.proposed.length} onChange={e => setPicked(Object.fromEntries(d.proposed.map(p => [p.id, e.target.checked])))} /></th><th>Step</th><th>Application</th><th>Screen</th><th>Click</th><th>Events</th><th>Hours</th><th>Why</th></tr></thead>
          <tbody>{d.proposed.map(p => <tr key={p.id}><td><input type="checkbox" checked={!!picked[p.id]} onChange={e => setPicked(s => ({ ...s, [p.id]: e.target.checked }))} /></td>
            <td><select className={steps[p.id] !== p.step ? 'changed' : ''} value={steps[p.id] || ''} onChange={e => setSteps(s => ({ ...s, [p.id]: e.target.value }))}>{d.steps.map(s => <option key={s}>{s}</option>)}</select></td>
            <td>{p.app}</td><td>{p.screen}</td><td>{p.click}</td><td>{fmt(p.events)}</td><td>{fmt(p.hours, 2)}</td><td className="muted">{p.why}</td></tr>)}</tbody></table></div>
        <div className="button-row">
          <Button onClick={() => act('acceptAll', async () => { const r = await api.accept(job.job_id, { rules: d.proposed.map(p => ({ id: p.id, step: steps[p.id] })), by }); setMsg(`Accepted ${d.proposed.length} rule(s). Run the analysis again to apply them.`) })} disabled={!by.trim()} loading={busy === 'acceptAll'}><CheckCircle2 size={15} /> Accept all {d.proposed.length}, with your changes</Button>
          <Button variant="outline" onClick={() => act('acceptSel', async () => { await api.accept(job.job_id, { rules: sel.map(id => ({ id, step: steps[id] })), by }); setMsg(`Accepted ${sel.length} rule(s).`) })} disabled={!by.trim() || !sel.length}>Accept selected ({sel.length})</Button>
          <input className="inline-input" value={reason} onChange={e => setReason(e.target.value)} placeholder="Reason to reject" />
          <Button variant="outline" onClick={() => act('reject', async () => { await api.reject(job.job_id, { ids: sel, by, reason }); setMsg(`Rejected ${sel.length} rule(s).`) })} disabled={!by.trim() || !sel.length || !reason.trim()}><XCircle size={15} /> Reject selected</Button>
        </div></>}
    </Panel>}
    {tab === 'units' && <Panel title="Work not linked to a step" subtitle="Largest first. Pick the step for each unit you recognise; leave the rest empty (other work). Each choice becomes an approved rule.">
      {!d.unassigned.length ? <SummaryNote>Every unit has a step.</SummaryNote> : !d.steps.length ? <SummaryNote warn>The process has no steps yet: add them in Process library.</SummaryNote> : <>
        <input className="inline-input wide" value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter by application, screen or click" />
        <div className="table-wrap tall"><table><thead><tr><th>Application</th><th>Screen</th><th>Click</th><th>Events</th><th>Hours</th><th>Step</th></tr></thead>
          <tbody>{units.slice(0, 200).map(u => <tr key={u.unit_id}><td>{u.app}</td><td>{u.screen}</td><td>{u.click}</td><td>{fmt(u.events)}</td><td>{fmt(u.hours, 2)}</td>
            <td><select value={assign[u.unit_id] || ''} onChange={e => setAssign(s => ({ ...s, [u.unit_id]: e.target.value }))}><option value="">— other work —</option>{d.steps.map(s => <option key={s}>{s}</option>)}</select></td></tr>)}</tbody></table></div>
        <div className="button-row"><Button onClick={() => act('assign', async () => { const r = await api.assign(job.job_id, { items: Object.entries(assign).filter(([, v]) => v).map(([unit_id, step]) => ({ unit_id, step })), by }); setMsg(`Saved ${r.saved} rule(s). Run the analysis again to apply them.`) })} disabled={!by.trim() || !nAssign} loading={busy === 'assign'}><Save size={15} /> Save {nAssign} assignment(s)</Button>
          <Button variant="outline" onClick={() => onNav('nc_overview')}><Play size={15} /> Go to the analysis</Button></div></>}
    </Panel>}
    {tab === 'activities' && <ActivitiesTab job={job} by={by} />}
    {tab === 'order' && <OrderTab job={job} by={by} />}
    {tab === 'questions' && <Panel title="Questions for the SME" subtitle="From the analysis (weak evidence, order, rules) and from reviewers. Answers are kept in the process library.">
      {d.questions.map(q => <div key={q.key} className={'question' + (q.answer ? ' answered' : '')}>
        <div className="q-head"><Tag tone={q.answer ? 'positive' : 'warning'}>{q.answer ? 'answered' : 'open'}</Tag><Tag>{q.kind}</Tag><strong>{q.question}</strong></div>
        {q.answer ? <p className="q-answer">{q.answer} <span className="muted">— {q.by}</span></p> : <div className="inline-form">
          <input value={answers[q.key] || ''} onChange={e => setAnswers(s => ({ ...s, [q.key]: e.target.value }))} placeholder="Answer" />
          <Button variant="outline" disabled={!by.trim() || !(answers[q.key] || '').trim()} onClick={() => act('ans', () => api.answer(job.job_id, { key: q.key, question: q.question, answer: answers[q.key], by }))}>Save answer</Button></div>}
      </div>)}
      {!d.questions.length && <SummaryNote>No questions.</SummaryNote>}
      <div className="inline-form"><Field label="Add a question"><input value={newQ} onChange={e => setNewQ(e.target.value)} placeholder="e.g. Is the approval always by e-mail?" /></Field>
        <Button variant="outline" disabled={!newQ.trim()} onClick={() => act('q', async () => { await api.addQuestion(job.job_id, newQ); setNewQ('') })}>Add</Button></div>
    </Panel>}
  </>
}

function ActivitiesTab({ job, by }) {
  const [d, setD] = useState(null)
  const [rows, setRows] = useState([])
  const [pick, setPick] = useState({})
  const [reason, setReason] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const load = () => api.activities(job.job_id).then(r => { setD(r); setRows(r.proposed); setPick({}) }).catch(e => setError(e.message))
  useEffect(() => { load() }, [job.job_id])
  if (!d) return error ? <StatusBanner>{error}</StatusBanner> : <Loading />
  const sel = rows.filter(r => pick[r.id])
  const act = async fn => { setError(''); setMsg(''); try { await fn(); await load() } catch (e) { setError(e.message) } }
  return <Panel title="Level 4 activities" subtitle="Groups of clicks inside each step, proposed by the AI (when approved) or by rules. Rename or describe them, then accept; rejected ones are not proposed again.">
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}{msg && <SummaryNote>{msg}</SummaryNote>}
    <h4 className="sub-h">Waiting for review ({rows.length})</h4>
    {!rows.length ? <SummaryNote>No activity groups are waiting. They appear after a run with the approved task “Name activities inside each step”.</SummaryNote> : <>
      <div className="table-wrap"><table><thead><tr><th><input type="checkbox" checked={sel.length === rows.length} onChange={e => setPick(Object.fromEntries(rows.map(r => [r.id, e.target.checked])))} /></th><th>Step</th><th>Activity</th><th>Description</th><th>Rules</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={r.id}><td><input type="checkbox" checked={!!pick[r.id]} onChange={e => setPick(s => ({ ...s, [r.id]: e.target.checked }))} /></td><td>{r.step}</td>
          <td><input value={r.activity} onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, activity: e.target.value } : x))} /></td>
          <td><input value={r.description} onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, description: e.target.value } : x))} /></td>
          <td className="muted">{(r.rules || []).length}</td></tr>)}</tbody></table></div>
      <div className="button-row"><Button onClick={() => act(async () => { await api.acceptActivities(job.job_id, { items: sel, by }); setMsg(`Accepted ${sel.length} group(s).`) })} disabled={!by.trim() || !sel.length}><CheckCircle2 size={15} /> Accept selected ({sel.length})</Button>
        <input className="inline-input" value={reason} onChange={e => setReason(e.target.value)} placeholder="Reason to reject" />
        <Button variant="outline" onClick={() => act(async () => { await api.rejectActivities(job.job_id, { ids: sel.map(r => r.id), by, reason }); setMsg(`Rejected ${sel.length} group(s).`) })} disabled={!by.trim() || !sel.length || !reason.trim()}><XCircle size={15} /> Reject selected</Button></div></>}
    <h4 className="sub-h">Approved groups ({d.approved.length})</h4>
    <DataTable rows={d.approved} empty="None approved yet." columns={[{ key: 'step', label: 'Step' }, { key: 'activity', label: 'Activity' }, { key: 'status', label: 'Status' }]} />
    <h4 className="sub-h">In use in this analysis ({d.in_use.length})</h4>
    <DataTable rows={d.in_use} max={60} columns={[{ key: 'step', label: 'Step' }, { key: 'activity', label: 'Activity' }, { key: 'hours', label: 'Hours', digits: 2 }, { key: 'events', label: 'Events' }, { key: 'actors', label: 'People' }, { key: 'source', label: 'Source' }, { key: 'status', label: 'Status' }]} />
  </Panel>
}

export function OrderTab({ job, by, onSaved }) {
  const [d, setD] = useState(null)
  const [rows, setRows] = useState([])
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const load = () => api.order(job.job_id).then(r => { setD(r); setRows(r.rows) }).catch(e => setError(e.message))
  useEffect(() => { load() }, [job.job_id])
  if (!d) return error ? <StatusBanner>{error}</StatusBanner> : <Loading />
  const move = (i, k) => setRows(rs => { const a = [...rs]; const j = i + k; if (j < 0 || j >= a.length) return a; [a[i], a[j]] = [a[j], a[i]]; return a.map((x, n) => ({ ...x, position: n + 1 })) })
  return <Panel title="Business order of the steps" subtitle={`Status: ${d.provenance.label}. The documented order is knowledge until an SME confirms it; recorded event order never invents cases.`}>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}{msg && <SummaryNote>{msg}</SummaryNote>}
    {!rows.length ? <SummaryNote>The process has no steps yet: add them in Process library.</SummaryNote> : <>
      <div className="table-wrap"><table><thead><tr><th>#</th><th>Stage</th><th>Step</th><th>Exception (optional)</th><th>Move</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={r.step + i}><td>{i + 1}</td>
          <td><input value={r.stage} onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, stage: e.target.value } : x))} /></td><td><b>{r.step}</b></td>
          <td><input value={r.exception || ''} onChange={e => setRows(rs => rs.map((x, j) => j === i ? { ...x, exception: e.target.value } : x))} placeholder="e.g. skipped when not needed" /></td>
          <td><button className="icon-button" onClick={() => move(i, -1)} aria-label="Up">↑</button> <button className="icon-button" onClick={() => move(i, 1)} aria-label="Down">↓</button></td></tr>)}</tbody></table></div>
      <Button onClick={async () => { setError(''); try { await api.saveOrder(job.job_id, { rows: rows.map((r, i) => ({ ...r, position: i + 1 })), by }); setMsg(job.mode === 'nocase' ? 'Business order saved as SME-confirmed. Run the analysis again to use it.' : 'Business order saved as SME-confirmed. The map and the comparison below use it now.'); load(); onSaved?.() } catch (e) { setError(e.message) } }} disabled={!by.trim()}><Save size={15} /> Save SME-confirmed order</Button></>}
    <h4 className="sub-h">Order edges and exceptions</h4>
    <DataTable rows={d.edges} empty="No separately recorded order edges yet. The step order above is the order shown on the map." columns={Object.keys(d.edges[0] || { src: 1 }).map(k => ({ key: k, label: k }))} />
  </Panel>
}

/** Accepted rules are used from the next run: run again here, then read the map. */
function RerunBar({ job, onNav, onDone, pending }) {
  const [state, setState] = useState(null)
  const [error, setError] = useState('')
  async function rerun() {
    setError('')
    try {
      await api.run(job.job_id, false)
      setState('running')
      const poll = async () => {
        const r = await api.runStatus(job.job_id)
        if (r.running) { setTimeout(poll, 1500); return }
        setState(r.ok === false ? 'failed' : 'done'); if (r.ok === false) setError(r.message); onDone()
      }
      setTimeout(poll, 1200)
    } catch (e) { setError(e.message) }
  }
  return <div className="rerun-bar">
    <div><strong>After accepting, run the analysis again</strong><span>{pending ? `${pending} proposed rule(s) still waiting: accept or reject them first. ` : ''}The next run uses every accepted rule, order and activity (no AI is sent), so the map shows the reviewed process.</span></div>
    <Button variant="outline" onClick={rerun} disabled={state === 'running'}>{state === 'running' ? <><LoaderCircle size={15} className="spin" /> Running…</> : <><Play size={15} /> Run again with accepted rules</>}</Button>
    <Button onClick={() => onNav('nc_map')}><GitBranch size={15} /> Next: Process Map</Button>
    {state === 'done' && <span className="ok-note"><CheckCircle2 size={14} /> Analysis updated</span>}
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
  </div>
}
