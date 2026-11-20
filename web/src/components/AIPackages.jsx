import React, { useEffect, useMemo, useState } from 'react'
import { ArrowDownToLine, CheckCircle2, FileJson2, LockKeyhole, ShieldCheck, ShieldOff, Undo2 } from 'lucide-react'
import { api, downloadJson } from '../api.js'
import { Button, DataTable, Field, Panel, StatusBanner, SummaryNote, Tag, fmt } from '../ui.jsx'

/** Approve-before-send for one kind of AI work (docs | analysis | report).
 *  Shows every prepared request as JSON with its SHA-256, what it is for, what it never contains and where it would go.
 *  Nothing is sent from here: approval only allows the next run to send these requests. */
export default function AIPackages({ jobId, kind, refreshKey = 0, onChange, compact = false }) {
  const [data, setData] = useState(null)
  const [task, setTask] = useState(null)
  const [index, setIndex] = useState(0)
  const [by, setBy] = useState(() => { try { return localStorage.getItem('pm_reviewer') || '' } catch { return '' } })
  const [ok, setOk] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = () => api.aiPackages(jobId, kind).then(d => { setData(d); setError('') }).catch(e => setError(e.message))
  useEffect(() => { load() }, [jobId, kind, refreshKey])
  const tasks = data?.tasks || []
  useEffect(() => { if (tasks.length && !tasks.some(t => t.task === task)) { setTask(tasks[0].task); setIndex(0) } }, [data])
  useEffect(() => { setOk(false); setIndex(0) }, [task])
  const t = tasks.find(x => x.task === task)
  const pk = useMemo(() => (data?.packages || []).filter(p => p.task === task), [data, task])
  const current = pk[Math.min(index, Math.max(0, pk.length - 1))]
  async function approve() {
    setBusy(true); setError('')
    try { try { localStorage.setItem('pm_reviewer', by) } catch { /* private window */ }
      await api.aiApprove(jobId, kind, { task, task_fingerprint: t.task_fingerprint, by }); await load(); onChange?.() }
    catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  async function revoke() { await api.aiRevoke(jobId, kind, task); await load(); onChange?.() }
  if (!data) return error ? <StatusBanner>{error}</StatusBanner> : null
  const ai = data.ai || {}
  if (!tasks.length) return <SummaryNote>No AI request is prepared yet{kind === 'analysis' ? ': run the analysis without AI first — it builds every request it would make and keeps them here for review.' : '.'}</SummaryNote>
  return <div className="ai-packages">
    {!compact && <div className="privacy-banner"><div className="privacy-icon">{ai.available ? <ShieldCheck size={24} /> : <ShieldOff size={24} />}</div>
      <div><h3>Review before anything is sent</h3><p>Each request below was built and checked by the privacy guard exactly as it would be sent, then kept. Approve a task to let the next run send its requests.
        AI providers and keys are set in Settings / .env only; {ai.available ? `available now: ${(data.ai.providers || []).join(', ') || 'none'}.` : `AI is not available now (${ai.reason || 'switched off'}): approved packages are sent once it is.`}</p></div>
      <Tag tone={tasks.every(x => x.approved) ? 'positive' : 'warning'}>{tasks.filter(x => x.approved).length} of {tasks.length} approved</Tag></div>}
    <div className="two-panels ai-layout">
      <Panel title="Requests prepared" subtitle={`Prepared ${String(data.prepared_at || '').slice(0, 16).replace('T', ' ')} UTC · one row per AI task`}>
        <div className="task-list">{tasks.map(x => <button key={x.task} className={'task-row' + (x.task === task ? ' active' : '')} onClick={() => setTask(x.task)}>
          <div><strong>{x.title}</strong><small>{x.requests} request(s) · ~{fmt(x.approx_tokens)} tokens</small></div>
          {x.approved ? <Tag tone="positive"><CheckCircle2 size={12} /> approved</Tag> : <Tag tone="warning">waiting</Tag>}
        </button>)}</div>
        {t && <div className="task-detail">
          <div className="kv"><span>Purpose</span><p>{t.purpose}</p></div>
          <div className="kv"><span>What is in it</span><p>{t.includes}</p></div>
          <div className="kv"><span>Never in it</span><p>{t.never}</p></div>
          <div className="kv"><span>How the answer is used</span><p>{t.uses_answer}</p></div>
          <div className="privacy-details"><div><span>Requests</span><strong>{t.requests}</strong></div><div><span>Approx. tokens (input)</span><strong>{fmt(t.approx_tokens)}</strong></div>
            <div><span>Would go to</span><strong>{(t.would_go_to || []).join(', ') || 'no usable provider yet'}</strong></div><div><span>Dataset class</span><strong>{current?.dataset_class}</strong></div></div>
        </div>}
      </Panel>
      <Panel title="Exact JSON of the request" subtitle={pk.length > 1 ? `Request ${index + 1} of ${pk.length}` : 'The messages exactly as they would be sent'}
        action={<div className="row-actions">{pk.length > 1 && <><button className="text-button" disabled={index === 0} onClick={() => setIndex(i => i - 1)}>Previous</button><button className="text-button" disabled={index >= pk.length - 1} onClick={() => setIndex(i => i + 1)}>Next</button></>}
          <button className="text-button" onClick={() => downloadJson(`ai_${kind}_${task}_${index + 1}.json`, current?.messages)}><ArrowDownToLine size={14} /> JSON</button></div>}>
        {current && <>
          {current.parts.map((p, i) => <div key={i} className="msg-part"><div className="json-label"><FileJson2 size={12} /> {String(p.role).toUpperCase()}</div>
            {p.text && <pre className="json-preview text">{p.text}</pre>}
            {p.json && <pre className="json-preview">{JSON.stringify(p.json, null, 2)}</pre>}</div>)}
          <div className="fingerprint"><LockKeyhole size={13} /> SHA-256 of this request: <code>{current.fingerprint.slice(0, 24)}…</code></div>
          <div className="fingerprint"><LockKeyhole size={13} /> SHA-256 of the task (all its requests): <code>{t?.task_fingerprint?.slice(0, 24)}…</code></div>
        </>}
        <div className="approve-box">
          {t?.approved ? <>
            <SummaryNote>Approved by <b>{t.approved_by}</b> on {String(t.approved_at).slice(0, 16).replace('T', ' ')} UTC. If the requests change (new data, new steps), the approval no longer matches and must be given again.</SummaryNote>
            <Button variant="outline" onClick={revoke}><Undo2 size={15} /> Withdraw approval</Button>
          </> : <>
            <Field label="Approved by"><input value={by} onChange={e => setBy(e.target.value)} placeholder="Your name" /></Field>
            <label className="checkbox-row"><input type="checkbox" checked={ok} onChange={e => setOk(e.target.checked)} /><span>I reviewed the {t?.requests} request(s) of “{t?.title}” and approve sending them to the AI provider configured in Settings, under this dataset's class ({current?.dataset_class}: {current?.dataset_class === 'client' ? 'private providers only' : 'any allowed provider'}).</span></label>
            <Button onClick={approve} disabled={!ok || !by.trim()} loading={busy}><ShieldCheck size={16} /> Approve this task</Button>
          </>}
          {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
        </div>
      </Panel>
    </div>
    {!compact && data.audit?.length > 0 && <Panel title="Sent to the AI (audit log)" subtitle="Every request actually sent, newest last">
      <DataTable rows={data.audit} max={50} columns={[{ key: 'at', label: 'When (UTC)', render: v => String(v).slice(0, 19).replace('T', ' ') }, { key: 'task', label: 'Task' },
        { key: 'matched', label: 'Approval', render: v => <Tag tone={String(v).startsWith('exact') ? 'positive' : 'warning'}>{v}</Tag> }, { key: 'approved_by', label: 'Approved by' },
        { key: 'provider', label: 'Provider' }, { key: 'model', label: 'Model' }, { key: 'result', label: 'Result' }, { key: 'fingerprint', label: 'SHA-256', render: v => <code>{String(v).slice(0, 12)}…</code> }]} />
    </Panel>}
  </div>
}
