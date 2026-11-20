import React, { useEffect, useRef, useState } from 'react'
import { Activity, ArrowDownToLine, ArrowRight, BrainCircuit, CalendarDays, Clock3, Database, GitBranch, Layers3, Link2, ListChecks, Play, Users } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fileUrl } from '../api.js'
import AIPackages from '../components/AIPackages.jsx'
import { Button, ChartWrap, DataTable, Loading, Metric, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, colors, fmt, hours, pct, tooltipStyle } from '../ui.jsx'
import { downloadCsv } from '../api.js'

export function ScopeStrip({ o, basis, scope }) {
  if (!o) return null
  const items = [['People', fmt(o.actors)], ['Applications', fmt(o.apps)], ['Events', fmt(o.events)], ['Active hours', hours(o.hours)], ['Linked to a step', pct(1 - (o.unassigned_share ?? 1))]]
  return <section className="scope-strip"><div className="scope-strip-heading">Scope of analysis</div>
    <div className="scope-strip-grid">{items.map(([l, v]) => <div className="scope-strip-cell" key={l}><strong>{v}</strong><span>{l}</span></div>)}</div>
    <div className="scope-strip-foot">{scope ? `${String(scope.first_event).slice(0, 10)} to ${String(scope.last_event).slice(0, 10)} · ${fmt(scope.in_scope)} of ${fmt(scope.rows_in_file)} rows in scope · ` : ''}Time basis: {basis?.time || '–'} · no case ID: arrows are navigation inside working episodes, not proven hand-offs.</div>
  </section>
}

export default function AnalysisPage({ job, onJob, onNav }) {
  const [ov, setOv] = useState(null)
  const [run, setRun] = useState(null)
  const [error, setError] = useState('')
  const [pkKey, setPkKey] = useState(0)
  const timer = useRef(null)
  const load = () => api.overview(job.job_id).then(setOv).catch(e => setError(e.message))
  const poll = () => api.runStatus(job.job_id).then(r => {
    setRun(r)
    if (r.running) { timer.current = setTimeout(poll, 1500) } else { load(); setPkKey(k => k + 1); onJob(job.job_id) }
  }).catch(e => setError(e.message))
  useEffect(() => { load(); poll(); return () => clearTimeout(timer.current) }, [job.job_id])
  async function start(useAi) {
    setError('')
    try { await api.run(job.job_id, useAi); setRun({ running: true, message: 'Starting…' }); clearTimeout(timer.current); timer.current = setTimeout(poll, 800) }
    catch (e) { setError(e.message) }
  }
  const o = ov?.overall
  const last = ov?.last_run
  const ready = Boolean(job.steps?.find(x => x.key === 'context')?.done)
  return <>
    <WorkspaceHeader kicker="STEP 3 · OVERVIEW AND ANALYSIS" title="Overview and analysis"
      actions={o && <a className="btn outline" href={fileUrl(job.job_id, 'analysis.html')}><ArrowDownToLine size={15} /> Analysis HTML</a>}>Run the analysis engine. First without AI: it links screens to steps with your approved rules and prepares every AI request for review. Then, if you approve them, again with AI.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {!ready && <StatusBanner type="warning">Finish <b>Business context</b> first: choose the process and approve its stages and steps. The analysis links every screen to one of those steps. <button className="text-button" onClick={() => onNav('context')}>Go to Business context</button></StatusBanner>}
    <div className="run-cards">
      <div className="run-card"><div className="run-num">1</div><div><h3>Analyse without AI</h3><p>Approved rules only. Builds every AI request the run would make and keeps them for your review (nothing is sent).</p>
        <Button onClick={() => start(false)} disabled={run?.running || !ready}><Play size={15} /> Run without AI</Button></div></div>
      <div className="run-card"><div className="run-num">2</div><div><h3>Review the AI requests</h3><p>Open each task below: its purpose, the exact JSON, the size and the SHA-256. Approve with your name.</p>
        <a className="text-button" href="#ai-packages">Go to the requests <ArrowRight size={14} /></a></div></div>
      <div className="run-card"><div className="run-num">3</div><div><h3>Analyse with approved AI</h3><p>Only approved tasks are sent; each request sent is logged. Rules are used wherever the AI is not approved or not available.</p>
        <Button onClick={() => start(true)} disabled={run?.running || !ready}><BrainCircuit size={15} /> Run with approved AI</Button></div></div>
    </div>
    {run?.running && <Loading>{run.message || 'Running the analysis…'}{run.elsewhere ? ' (started in the other window)' : ''}</Loading>}
    {run && !run.running && run.ok === false && <StatusBanner>The run stopped: {run.message}</StatusBanner>}
    {!ov ? <Loading /> : !o ? <SummaryNote>No results yet. Run the analysis.</SummaryNote> : <>
      <ScopeStrip o={o} basis={ov.basis} scope={ov.scope} />
      <div className="metrics-grid">
        <Metric label="Active hours" value={hours(o.hours)} icon={Clock3} foot={`Time basis: ${ov.basis?.time}`} />
        <Metric label="Linked to a step" value={pct(1 - (o.unassigned_share ?? 1))} icon={Link2} foot={`${hours(o.unassigned_hours)} not linked yet`} tint={o.unassigned_share > 0.3 ? 'orange' : 'blue'} />
        <Metric label="People" value={fmt(o.actors)} icon={Users} foot={`${fmt(o.episodes)} working episodes`} tint="violet" />
        <Metric label="Step runs" value={fmt(o.step_runs)} icon={Activity} foot={`${fmt(o.switches)} application switches`} tint="orange" />
      </div>
      {o.unassigned_share > 0.3 && <SummaryNote warn>{pct(o.unassigned_share)} of the hours are not linked to a step yet. Approve the AI task “Link screens to steps” and run again, or give steps to the largest units on <button className="text-button" onClick={() => onNav('nc_steps')}>Review steps</button>.</SummaryNote>}
      <div className="two-panels wide-left">
        <Panel title="Hours by step" subtitle="Approved steps plus Unassigned; open the Process map for the order">
          <ChartWrap height={Math.max(220, 28 * (ov.steps || []).length)}><BarChart layout="vertical" data={ov.steps || []} margin={{ top: 5, right: 20, left: 10, bottom: 0 }}>
            <CartesianGrid horizontal={false} stroke="#e5ecf7" /><XAxis type="number" tick={{ fontSize: 11 }} /><YAxis type="category" dataKey="step" width={190} tick={{ fontSize: 11 }} />
            <Tooltip contentStyle={tooltipStyle} formatter={v => `${fmt(v, 2)} h`} /><Bar dataKey="hours" fill={colors.primary} radius={[0, 4, 4, 0]} /></BarChart></ChartWrap>
        </Panel>
        <Panel title="Last run: what each stage did" subtitle={last ? `${String(last.at).slice(0, 16).replace('T', ' ')} UTC · ${last.with_ai ? 'with approved AI' : 'without AI'}` : ''}>
          <DataTable rows={last?.stages || []} max={20} columns={[{ key: 'stage', label: 'Stage' }, { key: 'status', label: 'Status', render: v => <Tag tone={v === 'ran' ? 'positive' : 'warning'}>{v}</Tag> }, { key: 'message', label: 'Result', render: (v, r) => <>{v || '–'}{r.ai_note ? <div className="input-help">{r.ai_note}</div> : null}</> }]} />
          {last?.blocked?.length > 0 && <SummaryNote warn>{last.blocked.length} request(s) were stopped by the privacy guard and not sent: {last.blocked.map(b => b.why).join(' · ')}</SummaryNote>}
        </Panel>
      </div>
      <DataDetails ov={ov} />
      <div className="button-row"><Button onClick={() => onNav('nc_steps')}><ArrowRight size={15} /> Next: Review steps</Button><Button variant="outline" onClick={() => onNav('nc_map')}><GitBranch size={15} /> Process Map</Button><Button variant="outline" onClick={() => onNav('insights')}><CalendarDays size={15} /> Data Insights</Button></div>
    </>}
    <div id="ai-packages"><Panel title="AI requests for this analysis" subtitle="Approve-before-send: purpose, exact JSON, size, SHA-256, where it would go.">
      <AIPackages jobId={job.job_id} kind="analysis" refreshKey={pkKey} />
    </Panel></div>
  </>
}

function DataDetails({ ov }) {
  const [tab, setTab] = useState('supports')
  const caps = ov.capabilities || {}
  const plan = ov.evidence_plan || {}
  const sc = ov.evidence_scores || {}
  const score = sig => (sc[sig] || (sc.scores || {})[sig] || {})
  return <Panel title="Data details" subtitle="What the data supports, how the steps are recognised, and the units the engine works with">
    <Tabs tabs={[['supports', Database, 'What this data supports'], ['plan', ListChecks, 'Evidence plan and scores'], ['units', Layers3, `Units (${fmt((ov.units || []).length)})`]]} value={tab} onChange={setTab} />
    {tab === 'supports' && <>
      <DataTable rows={Object.entries(caps.supports || {}).map(([k, v]) => ({ analysis: k.replaceAll('_', ' '), possible: v.possible, why: v.reason }))} max={40}
        columns={[{ key: 'analysis', label: 'Analysis' }, { key: 'possible', label: 'Possible', render: v => <Tag tone={v ? 'positive' : 'warning'}>{v ? 'yes' : 'no'}</Tag> }, { key: 'why', label: 'Why' }]} />
      <div className="two-panels in-panel">
        <div><h4 className="sub-h">Fill rate of the standard columns</h4>
          <DataTable rows={Object.entries(caps.fill || {}).filter(([, v]) => v.rows).map(([k, v]) => ({ column: k, rows: v.rows, filled: v.share }))} max={30}
            columns={[{ key: 'column', label: 'Column' }, { key: 'rows', label: 'Rows' }, { key: 'filled', label: 'Filled', render: v => pct(v, 1) }]} /></div>
        <div><h4 className="sub-h">ID shapes (letters → A, digits → 9; values never stored)</h4>
          <DataTable rows={(caps.id_shapes || {}).overall || []} max={12} empty="No ID-like tokens in the text, screen or click."
            columns={[{ key: 'shape', label: 'Shape' }, { key: 'events', label: 'Events' }, { key: 'share', label: 'Events %', render: v => pct(v, 1) }, { key: 'distinct', label: 'Distinct' }]} />
          {caps.case_id && <SummaryNote>Mapped case ID: <b>{String(caps.case_id.verdict).replaceAll('_', ' ')}</b> — {(caps.case_id.reasons || []).join('; ')}</SummaryNote>}
          {(caps.counters || []).map(c => <p key={c.column} className="input-help"><code>{c.column}</code>: {c.reason}</p>)}</div>
      </div>
      {caps.time && <p className="input-help">Active time: {String(caps.time.active_time)} · end times: {caps.time.ts_end ? 'yes' : 'no'} · median gap between one person's events: {caps.time.median_gap_per_actor_s == null ? '–' : `${fmt(caps.time.median_gap_per_actor_s, 0)} s`} · generic clicks: {pct((caps.action || {}).generic_share, 1)}</p>}
    </>}
    {tab === 'plan' && <>
      <p className="input-help">Evidence plan source: {plan.source || 'not recorded'}</p>
      <DataTable rows={(plan.plan || []).map(p => ({ ...p, score: score(p.signal).score, coverage: score(p.signal).coverage, consistency: score(p.signal).consistency, note: score(p.signal).reason }))} max={40}
        columns={[{ key: 'use', label: 'Use' }, { key: 'signal', label: 'Signal' }, { key: 'why', label: 'Why' }, { key: 'score', label: 'Measured score', render: v => v == null ? '–' : fmt(v, 2) },
          { key: 'coverage', label: 'Coverage', render: v => pct(v) }, { key: 'consistency', label: 'Consistency', render: v => pct(v) }, { key: 'note', label: 'Note' }]} />
      {(plan.questions_for_sme || []).length > 0 && <SummaryNote>{plan.questions_for_sme.length} question(s) for the SME are on Review steps → Questions.</SummaryNote>}
    </>}
    {tab === 'units' && <DataTable rows={ov.units || []} max={300} onExport={() => downloadCsv('units.csv', ov.units)}
      columns={[{ key: 'app', label: 'Application' }, { key: 'screen', label: 'Screen' }, { key: 'click', label: 'Click' }, { key: 'events', label: 'Events' }, { key: 'actors', label: 'People' },
        { key: 'hours', label: 'Hours', digits: 2 }, { key: 'is_generic', label: 'Generic', render: v => v ? 'yes' : '' }, { key: 'step', label: 'Step' }, { key: 'source', label: 'Linked by' }]} />}
  </Panel>
}
