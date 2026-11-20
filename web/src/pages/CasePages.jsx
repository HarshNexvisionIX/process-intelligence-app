import React, { useEffect, useState } from 'react'
import { Activity, AlarmClock, ArrowDownToLine, ArrowRight, BarChart3, CheckCircle2, GitBranch, Link2, ListChecks, Play, RefreshCcw, Save, ShieldCheck, TrendingUp, Users } from 'lucide-react'
import { CartesianGrid, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'
import { api, fileUrl } from '../api.js'
import { Button, ChartWrap, DataTable, Field, HorizontalBars, Loading, Metric, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, colors, dur, fmt, pct, tooltipStyle } from '../ui.jsx'

export const CLASSIC_URL = `${window.location.protocol}//${window.location.hostname}:8501`

export function QualityPage({ job, onJob, onNav }) {
  const [q, setQ] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ran = job.steps?.find(s => s.key === 'quality')?.done
  useEffect(() => { if (ran) api.quality(job.job_id).then(setQ).catch(() => setQ(null)); else setQ(null) }, [job.job_id, ran])
  const run = async () => { setBusy(true); setError(''); try { setQ(await api.runQuality(job.job_id)); onJob(job.job_id) } catch (e) { setError(e.message) } finally { setBusy(false) } }
  return <>
    <WorkspaceHeader kicker="STEP 3 · DATA QUALITY" title="Data quality" actions={<Button onClick={run} loading={busy}><ShieldCheck size={15} /> Run quality checks</Button>}>
      12 checks on every event. The score and band limit what the report may claim.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {!q ? <SummaryNote>Run the quality checks.</SummaryNote> : <>
      <div className="metrics-grid three"><div className="metric"><div className="metric-label">Feasibility score</div><div className="metric-value">{q.feasibility} / 100</div></div>
        <div className="metric"><div className="metric-label">Band</div><div className="metric-value">{q.band}</div></div>
        <div className="metric"><div className="metric-label">Analysable events</div><div className="metric-value">{Number(q.stats?.clean_rows || 0).toLocaleString()}</div><div className="metric-foot">{Number(q.stats?.cases || 0).toLocaleString()} cases</div></div></div>
      {q.dimensions && <Panel title="Score by dimension"><HorizontalBars rows={Object.entries(q.dimensions).map(([k, v]) => ({ k: k.replaceAll('_', ' '), v }))} nameKey="k" valueKey="v" max={10} /></Panel>}
      <Panel title="Checks"><DataTable rows={q.checks || []} max={30} columns={[{ key: 'status', label: '', render: v => <Tag tone={v === 'pass' ? 'positive' : 'warning'}>{v}</Tag> }, { key: 'title', label: 'Check', render: (v, r) => v || r.key }, { key: 'message', label: 'Result', render: (v, r) => v || r.detail || '' }]} /></Panel>
      {(q.fixes || []).length > 0 && <Panel title="Fix these first"><ol className="insight-list">{q.fixes.map((f, i) => <li key={i}><b>{f.check}</b>: {f.problem} → {f.fix}</li>)}</ol></Panel>}
      <Button onClick={() => onNav('analysis')}>Next: Process analysis</Button>
    </>}
  </>
}

export function CaseAnalysisPage({ job, onJob, onNav }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [k, setK] = useState(0)
  const done = job.steps?.find(s => s.key === 'analysis')?.done
  const run = async () => { setBusy(true); setError(''); try { await api.runAnalysis(job.job_id); setK(x => x + 1); onJob(job.job_id) } catch (e) { setError(e.message) } finally { setBusy(false) } }
  return <>
    <WorkspaceHeader kicker="STEP 4 · PROCESS ANALYSIS" title="Process analysis" actions={<Button onClick={run} loading={busy}><Play size={15} /> {done ? 'Run again' : 'Run analysis'}</Button>}>
      Cases, durations, variants, waiting times, rework, people and applications, computed from every event. Next you link the activities to the approved steps and confirm the order; the Process Map then shows the approved process.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {done ? <><CaseResults key={'r' + k} job={job} />
      <div className="button-row"><Button onClick={() => onNav('definition')}>Next: Review steps <ArrowRight size={15} /></Button></div></>
      : <SummaryNote>Run the analysis. It needs the Business context confirmed and the quality checks run.</SummaryNote>}
  </>
}

const VERDICT = { same: ['positive', '✓ same as documented'], similar: ['warning', '? similar — please check'], you: ['positive', '✓ confirmed by you'], none: ['', '✗ not in the documents'] }

/** Case ID, step 5. The data's own levels (Level 2 stage, Level 3 step) are the process as recorded; the
 *  documented steps (Business context) check them. Then the intended flow: the order the cases should follow,
 *  prefilled from the documents and the data, corrected and confirmed here. */
export function CaseStepsPage({ job, onJob, onNav }) {
  const [d, setD] = useState(null)
  const [links, setLinks] = useState({})
  const [rows, setRows] = useState([])
  const [by, setBy] = useState('')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [tab, setTab] = useState('compare')
  const apply = r => { setD(r); setLinks(Object.fromEntries(r.activities.map(a => [a.activity, a.step || '']))); setRows(r.flow.map(x => ({ ...x }))) }
  useEffect(() => { api.caseSteps(job.job_id).then(apply).catch(e => setError(e.message)) }, [job.job_id])
  if (error && !d) return <><WorkspaceHeader kicker="STEP 5 · REVIEW STEPS" title="Review steps" /><StatusBanner>{error}</StatusBanner></>
  if (!d) return <Loading />
  const run = async (name, fn, ok) => { setBusy(name); setError(''); setMsg(''); try { apply(await fn()); setMsg(ok); onJob(job.job_id) } catch (e) { setError(e.message) } finally { setBusy('') } }
  const saveLinks = () => run('links', () => api.saveCaseSteps(job.job_id, { links, by }), 'Comparison confirmed. The intended flow below uses these matches for its order.')
  const saveFlow = () => run('flow', () => api.saveCaseFlow(job.job_id, { rows: rows.map(({ stage, step, in_flow }) => ({ stage, step, in_flow })), by }), 'Intended flow confirmed. The Process Map, the comparison, the report and the AI now use it.')
  const move = (i, k) => setRows(rs => { const n = [...rs]; const j = i + k; if (j < 0 || j >= n.length) return rs; [n[i], n[j]] = [n[j], n[i]]; return n })
  const set = (i, key, v) => setRows(rs => rs.map((r, j) => j === i ? { ...r, [key]: v } : r))
  const ref = d.reference
  const docs = d.steps.length > 0
  const verdictOf = a => { const l = links[a.activity] || ''; if (!l) return 'none'; if (a.source === 'saved' && l === a.step && a.verdict !== 'same') return 'you'; if (l === a.suggested) return a.verdict === 'none' ? 'you' : a.verdict; return 'you' }
  const count = v => d.activities.filter(a => verdictOf(a) === v).length
  const groups = d.activities.reduce((m, a) => ((m[a.stage || '—'] = m[a.stage || '—'] || []).push(a), m), {})
  const flowStages = rows.filter(r => r.in_flow).reduce((m, r) => { const k = r.stage || '—'; if (!m.length || m[m.length - 1].name !== k) m.push({ name: k, steps: [] }); m[m.length - 1].steps.push(r.step); return m }, [])
  const nameBox = label => <Field label={label}><input value={by} onChange={e => setBy(e.target.value)} placeholder="e.g. Harsh" /></Field>
  return <>
    <WorkspaceHeader kicker="STEP 5 · REVIEW STEPS" title="Review steps">The steps come from your data{d.has_stages ? ' (Level 3, grouped by their Level 2 stage)' : ''}{d.level1?.length ? ` — Level 1: ${d.level1.join(', ')}` : ''}. Check each one against the documented steps, then correct and confirm the intended flow: the order the cases should follow. The Process Map, the report and the AI use the confirmed flow.</WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}{msg && <SummaryNote>{msg}</SummaryNote>}
    <div className="metrics-grid four compact">
      <div className="metric"><div className="metric-label">Steps in the data (Level 3)</div><div className="metric-value">{d.activities.length}</div><div className="metric-foot">{d.has_stages ? `in ${Object.keys(groups).length} Level 2 stages` : 'no Level 2 column'}</div></div>
      <div className="metric"><div className="metric-label">Checked against the documents</div><div className="metric-value small">{docs ? `${count('same') + count('you')} ✓ · ${count('similar')} ? · ${count('none')} ✗` : 'no documents'}</div><div className="metric-foot">{docs ? `${d.missing.length} documented step(s) not found in the data` : 'add them on Business context'}</div></div>
      <div className="metric"><div className="metric-label">Cases in the intended order</div><div className="metric-value">{ref?.in_order_share != null ? pct(ref.in_order_share) : '–'}</div><div className="metric-foot">{ref?.pair_order_share != null ? `${pct(ref.pair_order_share)} of step pairs in order` : 'run the analysis'}</div></div>
      <div className="metric"><div className="metric-label">Intended flow</div><div className="metric-value small">{d.flow_source === 'confirmed' ? 'Confirmed' : 'Not confirmed yet'}</div><div className="metric-foot">{d.flow_source === 'confirmed' ? `by ${d.flow_by} · ${String(d.flow_at || '').slice(0, 10)}` : d.flow_source === 'prefilled' ? 'prefilled from your documents and the data' : 'prefilled from the data'}</div></div></div>
    <Tabs tabs={[['compare', Link2, '1 · Your steps vs the documents'], ['flow', ListChecks, '2 · Intended flow'], ['follow', GitBranch, '3 · How the cases follow it']]} value={tab} onChange={setTab} />
    {tab === 'compare' && <Panel title="Is each step of the data the same as a documented step?" subtitle="Suggestions only: ✓ the same name, ? a very similar name or mostly the same words (please check), ✗ nothing similar in the documents. Accept, or pick the right documented step, then confirm. A step that is not in the documents stays in the process; it is only marked as undocumented.">
      {!docs && <SummaryNote warn>No documented steps yet: add the SOP on <button className="text-button" onClick={() => onNav('context')}>Business context</button> and approve its stages and steps to check the data against them.</SummaryNote>}
      {docs && <><div className="table-wrap"><table className="links-table"><thead><tr><th>{d.has_stages ? 'Level 2 (data)' : 'Stage'}</th><th>Level 3 step (data)</th><th>Cases</th><th>Documented step</th><th>Check</th></tr></thead>
        <tbody>{Object.entries(groups).map(([stg, acts]) => acts.map((a, i) => { const v = verdictOf(a); return <tr key={a.activity} className={v === 'similar' ? 'todo' : ''}>
          <td>{i === 0 ? <b>{stg}</b> : ''}</td><td><b>{a.activity}</b></td><td>{fmt(a.cases)}</td>
          <td><select value={links[a.activity] || ''} onChange={e => setLinks(l => ({ ...l, [a.activity]: e.target.value }))}><option value="">— Not in the documents —</option>
            {d.steps.map(s => <option key={s.step} value={s.step}>{s.position}. {s.step}{s.stage ? ` (${s.stage})` : ''}</option>)}</select></td>
          <td><Tag tone={VERDICT[v][0]}>{VERDICT[v][1]}</Tag></td></tr> }))}</tbody></table></div>
        {d.missing.length > 0 && <SummaryNote warn>Documented steps not found in the data: {d.missing.map(m => <Tag key={m.step}>{m.step}</Tag>)} Either the data names them differently (pick them above), or they are not recorded in this system.</SummaryNote>}
        <div className="save-config">{nameBox('Your name (confirms the comparison)')}<Button onClick={saveLinks} loading={busy === 'links'} disabled={!by.trim()}><CheckCircle2 size={15} /> Confirm comparison</Button>
          <Button variant="outline" onClick={() => setTab('flow')}>Next: intended flow <ArrowRight size={15} /></Button></div></>}
    </Panel>}
    {tab === 'flow' && <Panel title="Intended flow: the order the cases should follow" subtitle={d.flow_source === 'confirmed' ? `Confirmed by ${d.flow_by}. Change and confirm again at any time.` : `Prefilled ${d.flow_source === 'prefilled' ? 'in the documented order where a step matches a documented step, other steps at their usual place in the cases' : 'from the order found in the data'}, each under its ${d.has_stages ? 'Level 2 stage from the data' : 'stage'}. Move steps, rename or change a stage, take a step out of the flow; then confirm.`}>
      <div className="flow-preview">{flowStages.map((st, i) => <div key={i} className="flow-stage"><div className="flow-stage-name">{st.name}</div><div className="flow-steps">{st.steps.map((x, j) => <span key={x} className="flow-chip">{x}{j < st.steps.length - 1 && <ArrowRight size={12} />}</span>)}</div></div>)}</div>
      <div className="table-wrap"><table className="links-table flow-table"><thead><tr><th>#</th><th>Stage (Level 2)</th><th>Step (Level 3)</th><th>Cases</th><th>Documented as</th><th>In the flow</th><th>Move</th></tr></thead>
        <tbody>{rows.map((r, i) => <tr key={r.step} className={r.in_flow ? '' : 'muted-row'}><td>{i + 1}</td>
          <td><input value={r.stage || ''} onChange={e => set(i, 'stage', e.target.value)} /></td><td><b>{r.step}</b>{r.new && <Tag tone="warning">new in this file</Tag>}</td><td>{fmt(r.cases)}</td>
          <td>{links[r.step] ? links[r.step] : <span className="input-help">not in the documents</span>}</td>
          <td><input type="checkbox" checked={!!r.in_flow} onChange={e => set(i, 'in_flow', e.target.checked)} /></td>
          <td className="move-cell"><button className="icon-button" aria-label="Up" onClick={() => move(i, -1)} disabled={i === 0}>↑</button><button className="icon-button" aria-label="Down" onClick={() => move(i, 1)} disabled={i === rows.length - 1}>↓</button></td></tr>)}</tbody></table></div>
      <div className="save-config">{nameBox('Your name (confirms the intended flow)')}<Button onClick={saveFlow} loading={busy === 'flow'} disabled={!by.trim()}><Save size={15} /> Confirm intended flow</Button></div>
    </Panel>}
    {tab === 'follow' && <Panel title="How the cases follow the intended flow" subtitle={ref ? `Flow used: ${ref.source_badge} · flow confidence ${ref.flow?.band} (${ref.flow?.score})` : 'Run the analysis first'}>
      {!ref ? <SummaryNote>Run the analysis on Process analysis first.</SummaryNote> : <>
        <ul className="insight-list">{(ref.conformance || []).map((l, i) => <li key={i}>{l}</li>)}</ul>
        {ref.earlier_than_intended?.length > 0 && <><h4 className="sub-h">Done earlier than intended</h4><DataTable rows={ref.earlier_than_intended} columns={[{ key: 'step', label: 'Step' }, { key: 'before', label: 'Done before (intended earlier)' }, { key: 'cases', label: 'Cases' }]} /></>}
        {ref.back_and_forth?.length > 0 && <><h4 className="sub-h">Back and forth</h4><DataTable rows={ref.back_and_forth} columns={[{ key: 'a', label: 'Step' }, { key: 'b', label: 'Step' }, { key: 'count', label: 'Times' }, { key: 'cases', label: 'Cases' }]} /></>}
        <h4 className="sub-h">Cases reaching each step</h4>
        <DataTable rows={ref.steps} max={60} columns={[{ key: 'position', label: '#' }, { key: 'stage', label: 'Stage' }, { key: 'step', label: 'Step' }, { key: 'cases_reached', label: 'Cases' }, { key: 'share_reached', label: 'Share', render: v => pct(v) }]} />
        {ref.other_steps?.names?.length > 0 && <p className="input-help">Not in the intended flow: {ref.other_steps.names.join(', ')} · in {pct(ref.other_steps.share)} of cases.</p>}</>}
    </Panel>}
    <div className="button-row"><Button onClick={() => onNav('case_map')} disabled={!ref}>Next: Process Map <ArrowRight size={15} /></Button></div>
  </>
}

const H = h => dur(h)
function Unavailable({ sec }) { return <SummaryNote warn>{sec?.reason || 'Not available for this data.'}</SummaryNote> }

export function CaseResults({ job }) {
  const [d, setD] = useState(null)
  const [tab, setTab] = useState('kpis')
  const [error, setError] = useState('')
  useEffect(() => { api.caseResults(job.job_id).then(setD).catch(e => setError(e.message)) }, [job.job_id])
  if (error) return <StatusBanner>{error}</StatusBanner>
  if (!d) return null
  const S = d.sections, k = S.kpis.data || {}
  const fc = S.flow_confidence.data || {}
  return <Panel title="Analysis results" subtitle={`Data quality band: ${d.band} · ${d.band_label || ''}`}
    action={<a className="text-button" href={fileUrl(job.job_id, 'case-results.html')}><ArrowDownToLine size={14} /> All results as HTML</a>}>
    <Tabs tabs={[['kpis', BarChart3, 'Key numbers'], ['variants', GitBranch, 'Variants'], ['bottlenecks', AlarmClock, 'Waiting'], ['rework', RefreshCcw, 'Rework'],
      ['people', Users, 'People & hand-offs'], ['apps', Activity, 'Applications & switching'], ['trends', TrendingUp, 'Trends']]} value={tab} onChange={setTab} />
    {tab === 'kpis' && (S.kpis.available ? <>
      <div className="metrics-grid"><Metric label="Cases" value={fmt(k.cases)} icon={GitBranch} foot={`${fmt(k.events)} events · ${fmt(k.activities)} activities`} />
        <Metric label="Median case duration" value={H(k.median_h)} icon={AlarmClock} tint="blue" foot={`90% within ${H(k.p90_h)}`} />
        <Metric label="Events per case (median)" value={fmt(k.events_per_case_median)} icon={Activity} tint="orange" foot={`${fmt(k.resources)} people / resources`} />
        <Metric label="Flow confidence" value={fc.band ? `${fc.band} (${fc.score})` : '–'} icon={ShieldCheck} tint="violet" foot={(fc.reasons || []).join('; ')} /></div>
      <p className="input-help">Period {String(k.first_event).slice(0, 10)} to {String(k.last_event).slice(0, 10)} · durations are {k.has_end_times ? 'start to end' : 'first to last event (no end times)'}. {fc.explain}</p></> : <Unavailable sec={S.kpis} />)}
    {tab === 'variants' && (S.variants.available ? <DataTable rows={(S.variants.data.variants || []).map(v => ({ ...v, path: (v.path || []).join(' → ') }))} max={25}
      columns={[{ key: 'rank', label: '#' }, { key: 'cases', label: 'Cases' }, { key: 'share', label: 'Share', render: v => pct(v, 1) }, { key: 'cumulative_share', label: 'Cumulative', render: v => pct(v) }, { key: 'median_duration_h', label: 'Median duration', render: H }, { key: 'path', label: 'Path' }]} /> : <Unavailable sec={S.variants} />)}
    {tab === 'bottlenecks' && (S.bottlenecks.available ? <DataTable rows={[...(S.bottlenecks.data.transitions || [])].sort((a, b) => (b.median_wait_h || 0) - (a.median_wait_h || 0))} max={25}
      columns={[{ key: 'src', label: 'From' }, { key: 'dst', label: 'To' }, { key: 'cases', label: 'Cases' }, { key: 'median_wait_h', label: 'Median wait', render: H }, { key: 'p90_wait_h', label: '90% wait', render: H }, { key: 'low_support', label: '', render: v => v ? <Tag tone="warning">few cases</Tag> : '' }]} /> : <Unavailable sec={S.bottlenecks} />)}
    {tab === 'rework' && (S.rework.available ? <>
      <DataTable rows={S.rework.data.activities || []} columns={[{ key: 'activity', label: 'Activity done again' }, { key: 'cases', label: 'Cases' }, { key: 'share', label: 'Share of its cases', render: v => pct(v) }, { key: 'extra_occurrences', label: 'Extra times' }]} />
      <p className="input-help">Same activity twice in a row: {fmt(S.rework.data.self_loops)} times. Back-and-forth pairs: {(S.rework.data.ping_pong || []).length}.</p>
      {(S.rework.data.ping_pong || []).length > 0 && <DataTable rows={S.rework.data.ping_pong} columns={Object.keys(S.rework.data.ping_pong[0]).map(c => ({ key: c, label: c.replaceAll('_', ' ') }))} />}</> : <Unavailable sec={S.rework} />)}
    {tab === 'people' && (S.resources.available ? <div className="two-panels in-panel">
      <div><h4 className="sub-h">People / resources ({fmt(S.resources.data.total_resources)})</h4><DataTable rows={S.resources.data.resources || []} max={15} columns={[{ key: 'resource', label: 'Resource' }, { key: 'events', label: 'Events' }, { key: 'cases', label: 'Cases' }]} /></div>
      <div><h4 className="sub-h">Hand-offs ({fmt(S.resources.data.total_handovers)})</h4><DataTable rows={S.resources.data.handovers || []} max={15} columns={Object.keys((S.resources.data.handovers || [])[0] || { from: 1 }).map(c => ({ key: c, label: c.replaceAll('_', ' ') }))} /></div></div> : <Unavailable sec={S.resources} />)}
    {tab === 'apps' && (S.applications.available ? <>
      <HorizontalBars rows={S.applications.data.applications || []} nameKey="application" valueKey="events" max={10} />
      {S.app_switching.available && <>
        <div className="mini-kpis four" style={{ marginTop: 16 }}><div><span>Switches</span><strong>{fmt(S.app_switching.data.kpis.total)}</strong></div><div><span>Per case (median)</span><strong>{fmt(S.app_switching.data.kpis.per_case_median, 1)}</strong></div>
          <div><span>Per 100 events</span><strong>{fmt(S.app_switching.data.kpis.per_100_events, 1)}</strong></div><div><span>Cases with a switch</span><strong>{pct(S.app_switching.data.kpis.share_cases_with_switch)}</strong></div></div>
        <DataTable rows={S.app_switching.data.pairs || []} max={15} columns={[{ key: 'from', label: 'From' }, { key: 'to', label: 'To' }, { key: 'count', label: 'Switches' }, { key: 'cases', label: 'Cases' }, { key: 'median_gap', label: 'Median gap' }]} /></>}</> : <Unavailable sec={S.applications} />)}
    {tab === 'trends' && (S.trends.available ? <ChartWrap height={280}><LineChart data={S.trends.data.months || []} margin={{ top: 10, right: 15, left: -10, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5ecf7" />
      <XAxis dataKey="month" tick={{ fontSize: 11 }} /><YAxis yAxisId="d" tick={{ fontSize: 11 }} /><YAxis yAxisId="c" orientation="right" tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} />
      <Line yAxisId="d" type="monotone" dataKey="median_duration_h" name="Median duration (h)" stroke={colors.primary} strokeWidth={2.4} /><Line yAxisId="c" type="monotone" dataKey="cases" name="Cases" stroke={colors.secondary} strokeWidth={2} /></LineChart></ChartWrap> : <Unavailable sec={S.trends} />)}
  </Panel>
}
