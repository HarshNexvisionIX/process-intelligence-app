import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, Braces, BrainCircuit, Bot, Send, CheckCircle2, Flag, Gauge, ShieldCheck, Sparkles, Target, TriangleAlert } from 'lucide-react'
import { api, downloadJson, fileUrl } from '../api.js'
import AIPackages from '../components/AIPackages.jsx'
import { Button, DataTable, HorizontalBars, Loading, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, colors, fmt } from '../ui.jsx'

const OUTLINE = ['Objective and scope', 'Where work is concentrated', 'As-is process', 'Effort, repetition and hand-offs',
  'Interventions to test', 'To-be workflow with agents', 'Outcomes and scale path']

function Head({ n, title, sub }) {
  return <div className="ro-head"><span>{String(n).padStart(2, '0')} / 07</span><div><h3>{title}</h3>{sub && <p>{sub}</p>}</div></div>
}
function Evidence({ id, lookup }) {
  const e = lookup[id]
  return <div className="ro-evidence" title={e?.limitation}><b>{id}</b><span>{e ? `${e.topic}: ${e.observed_fact}` : 'not in the evidence'}</span></div>
}
function Claim({ c, lookup }) {
  return <div className="ro-claim"><strong>{c.headline}</strong><p>{c.interpretation}</p><Evidence id={c.evidence_id} lookup={lookup} /></div>
}

function SummaryJson({ jobId, context, payload }) {
  const [tab, setTab] = useState('context')
  if (!context) return null
  const shown = tab === 'context' ? context : payload
  return <Panel title="The summary JSON" subtitle="ProcessLens format: the aggregate summary (schema 1.1) and the transformation context made from it"
    action={<button className="text-button" onClick={() => downloadJson(tab === 'context' ? `transformation_context_${jobId}.json` : `ai_summary_${jobId}.json`, shown)}><ArrowDownToLine size={14} /> Save this JSON</button>}>
    <Tabs tabs={[['context', Send, 'Transformation context (sent)'], ['payload', Braces, 'Aggregate summary (schema 1.1)']]} value={tab} onChange={setTab} />
    <div className="json-label">{tab === 'context' ? 'EXACT JSON SENT TO THE AI' : 'AGGREGATE SUMMARY (THE SOURCE OF EVERY AI JSON)'}</div>
    <pre className="json-preview">{JSON.stringify(shown, null, 2)}</pre>
  </Panel>
}

export default function ReadoutPage({ job, onNav }) {
  const [d, setD] = useState(null)
  const [map, setMap] = useState(null)
  const [ov, setOv] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [pkKey, setPkKey] = useState(0)
  const load = () => api.readout(job.job_id).then(setD).catch(e => setError(e.message))
  useEffect(() => {
    load()
    api.map(job.job_id, 'step').then(setMap).catch(() => setMap(null))
    if (job.mode === 'nocase') api.overview(job.job_id).then(setOv).catch(() => setOv(null))
  }, [job.job_id])
  const act = async (name, fn) => { setBusy(name); setError(''); setMsg(''); try { await fn() } catch (e) { setError(e.message) } finally { setBusy('') } }
  const prepare = () => act('prepare', async () => { const r = await api.readoutPrepare(job.job_id); setMsg(r.prepared ? 'The request is prepared below. Nothing was sent.' : `Not prepared: ${(r.blocked || []).map(b => b.why).join('; ')}`); setPkKey(k => k + 1) })
  const generate = () => act('generate', async () => { await api.readoutGenerate(job.job_id); await load(); setPkKey(k => k + 1); setMsg('Readout written.') })
  if (!d) return error ? <StatusBanner>{error}</StatusBanner> : <Loading />
  const lookup = Object.fromEntries((d.evidence?.evidence_catalog || []).map(e => [e.id, e]))
  const r = d.readout?.report
  const scopeIds = ['E01', 'E02', 'E03', 'E06'].filter(x => lookup[x])
  const flowId = Object.values(lookup).find(e => /transitions/i.test(e.topic))?.id
  const nocase = job.mode === 'nocase'
  const caseHours = n => (n.apps || []).reduce((t, a) => t + Number(a.hours || 0), 0)
  const steps = nocase ? (ov?.steps || []).filter(s => s.step !== 'Unassigned')
    : (map?.nodes || []).filter(n => !n.missing).map(n => ({ step: n.label, hours: caseHours(n) })).filter(s => s.hours > 0)
  return <>
    <WorkspaceHeader kicker="03 / AI TRANSFORMATION READOUT" title="AI Analyst"
      actions={r && <div className="button-row"><button className="btn outline" onClick={() => downloadJson(`ai_readout_${job.job_id}.json`, d.readout)}><ArrowDownToLine size={15} /> JSON</button>
        <a className="btn outline" href={fileUrl(job.job_id, 'readout.html')}><ArrowDownToLine size={15} /> Readout HTML</a>
        <a className="btn" href={fileUrl(job.job_id, 'readout.pdf')}><ArrowDownToLine size={15} /> Readout PDF</a></div>}>
      A stakeholder-ready, seven-section transformation storyline, built exactly like ProcessLens: the app computes an aggregate summary, turns it into the evidence catalog E01–E11 (plus this app's step evidence), and the AI writes only the interpretation. Every claim cites one evidence ID; the numbers shown are the app's, never the AI's.
    </WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {msg && <SummaryNote>{msg}</SummaryNote>}
    {!d.ready ? <SummaryNote warn>The readout is written from the report's checked facts. Confirm <button className="text-button" onClick={() => onNav('context')}>Business context</button> and generate the <button className="text-button" onClick={() => onNav(job.mode === 'nocase' ? 'nc_report' : 'report')}>Report</button> first.</SummaryNote> : <>
      <div className="privacy-banner"><div className="privacy-icon"><ShieldCheck size={24} /></div><div><h3>Evidence before AI</h3>
        <p>Review the exact restricted JSON below. Source participants, case IDs, application names, titles and activity text are excluded. The model makes one request; the PDF is generated locally without a second model call.</p></div>
        <Tag tone={r ? (d.stale ? 'warning' : 'positive') : 'neutral'}>{r ? (d.stale ? 'evidence changed' : 'readout ready') : 'not written yet'}</Tag></div>
      <div className="run-cards">
        <div className="run-card"><div className="run-num">1</div><div><h3>Prepare the request</h3><p>Builds the request from the evidence and checks it with the privacy guard. Nothing is sent.</p>
          <Button variant="outline" onClick={prepare} loading={busy === 'prepare'}><Sparkles size={15} /> Prepare</Button></div></div>
        <div className="run-card"><div className="run-num">2</div><div><h3>Review and approve</h3><p>Read the exact JSON and its SHA-256 below, then approve it with your name.</p><a className="text-button" href="#ro-request">Open the request</a></div></div>
        <div className="run-card"><div className="run-num">3</div><div><h3>Write the readout</h3><p>One AI call. The answer is checked: every claim must cite evidence and contain no numbers, else one retry.</p>
          <Button onClick={generate} loading={busy === 'generate'}><BrainCircuit size={15} /> Write the readout</Button></div></div>
      </div>
      {d.stale && <SummaryNote warn>The evidence changed after this readout was written (new run, new rules or a new report). Prepare, approve and write it again to update it.</SummaryNote>}

      {r ? <div className="ro-report">
        <section className="ro-cover"><Head n={1} title={OUTLINE[0]} /><h2>{r.headline}</h2><p>{r.transformation_objective}</p><p className="ro-exec">{r.executive_message}</p>
          <div className="ro-kpis">{scopeIds.map(x => <div key={x}><b>{lookup[x].observed_fact}</b><span>{lookup[x].topic}</span><i>{x}</i></div>)}</div>
          <p className="ro-meta">Written by {d.readout.provider} / {d.readout.model} on {String(d.readout.generated_at).slice(0, 16).replace('T', ' ')} UTC from {Object.keys(lookup).length} evidence items.</p></section>
        <div className="ro-grid">
          <section className="ro-section"><Head n={2} title={OUTLINE[1]} sub="Hours by step, measured" />
            {steps.length > 0 && <HorizontalBars rows={[...steps].sort((a, b) => b.hours - a.hours)} nameKey="step" valueKey="hours" suffix=" h" max={8} format={v => fmt(v, 1)} />}
            {(r.findings || []).map((c, i) => <Claim key={i} c={c} lookup={lookup} />)}</section>
          <section className="ro-section"><Head n={3} title={OUTLINE[2]} sub={nocase ? "Measured map: business order and observed transitions" : "Approved process: the cases that reach each step"} />
            {map && <div className="ro-strip">{[...new Set(map.nodes.map(n => n.group || 'Steps'))].map(g => <div key={g} className="ro-stage"><span>{g}</span>
              <div>{map.nodes.filter(n => (n.group || 'Steps') === g).map(n => <div key={n.id} className={'ro-step' + (n.missing ? ' missing' : '')}><b>{n.label}</b><small>{n.missing ? 'not seen yet' : nocase ? `${fmt(n.hours, 1)} h` : `${fmt(n.cases)} cases`}</small></div>)}</div></div>)}</div>}
            {flowId && <Evidence id={flowId} lookup={lookup} />}</section>
        </div>
        <section className="ro-section"><Head n={4} title={OUTLINE[3]} sub="Where effort and friction concentrate" />
          <div className="ro-cols">{(r.risks || []).map((c, i) => <div key={i} className="ro-risk"><TriangleAlert size={16} /><Claim c={c} lookup={lookup} /></div>)}</div></section>
        <section className="ro-section"><Head n={5} title={OUTLINE[4]} sub="Hypotheses: each with a KPI and a validation gate" />
          <div className="ro-interventions">{(r.interventions || []).map((x, i) => <div key={i} className="ro-int">
            <div className="ro-int-top"><Tag tone={x.priority === 'High' ? 'warning' : x.priority === 'Medium' ? 'neutral' : 'positive'}>{x.priority}</Tag><h4>{x.title}</h4></div>
            <div className="ro-int-grid"><div><span>Likely cause</span><p>{x.proposed_cause}</p></div><div><span>Change to test</span><p>{x.proposed_change}</p></div>
              <div><span>KPI to test</span><p>{x.kpi_to_test}</p></div><div><span>Validation gate</span><p>{x.validation_gate}</p></div></div>
            <Evidence id={x.evidence_id} lookup={lookup} /></div>)}</div></section>
        <section className="ro-section"><Head n={6} title={OUTLINE[5]} sub="Agents act; people approve what matters" />
          <div className="ro-orch"><Bot size={18} /><p><b>Orchestrator:</b> {r.orchestrator_role}</p></div>
          <div className="ro-agents">{(r.agents || []).map((a, i) => <div key={i} className="ro-agent"><div className="agent-id">A{i + 1}</div><h4>{a.title}</h4><p>{a.role}</p>
            <ul>{(a.actions || []).map((t, j) => <li key={j}>{t}</li>)}</ul><div className="ro-human"><CheckCircle2 size={14} /> {a.human_control}</div></div>)}</div></section>
        <section className="ro-section"><Head n={7} title={OUTLINE[6]} sub="Measure a baseline, pilot, then scale" />
          <div className="ro-outcomes">{(r.outcomes || []).map((o, i) => <div key={i} className="ro-outcome"><Target size={16} /><h4>{o.title}</h4><Tag>{o.target_direction}</Tag>
            <p><b>KPI:</b> {o.kpi}</p><p><b>Measure:</b> {o.how_to_measure}</p><p><b>Scale gate:</b> {o.scale_gate}</p></div>)}</div>
          {(r.decisions || []).length > 0 && <div className="ro-decisions"><h4><Flag size={15} /> Decisions needed</h4><ol>{r.decisions.map((x, i) => <li key={i}>{x}</li>)}</ol></div>}</section>
        <SummaryNote warn>Causes and changes are hypotheses from aggregate evidence. Hours are observed effort, not savings. Validate with the SME and a measured pilot before acting.</SummaryNote>
      </div> : <Panel title="The seven sections" subtitle="What the readout will contain once written"><div className="readout-outline">{OUTLINE.map((t, i) => <span key={t}>{String(i + 1).padStart(2, '0')} / {t}</span>)}</div></Panel>}

      <Panel title="Evidence catalog" subtitle={`${(d.evidence?.evidence_catalog || []).length} entries · E01–E11 as in ProcessLens, E12 and later from this app's step analysis`}>
        <DataTable rows={d.evidence?.evidence_catalog || []} max={200} columns={[{ key: 'id', label: 'ID' }, { key: 'topic', label: 'Topic' }, { key: 'observed_fact', label: 'Observed fact' }, { key: 'limitation', label: 'Limitation' }]} />
      </Panel>
      <SummaryJson jobId={job.job_id} context={d.evidence?.context} payload={d.evidence?.summary_payload} />
      <div id="ro-request"><Panel title="The request (approve before it is sent)"><AIPackages jobId={job.job_id} kind="readout" refreshKey={pkKey} compact /></Panel></div>
    </>}
  </>
}
