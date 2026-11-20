import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, ArrowRight, GitBranch, LoaderCircle, Sparkles } from 'lucide-react'
import { api, downloadCsv, downloadJson, fileUrl } from '../api.js'
import FlowMap from '../components/FlowMap.jsx'
import StagePanel from '../components/StagePanel.jsx'
import AIMapPanel from '../components/AIMapPanel.jsx'
import { Button, DataTable, HorizontalBars, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader, fmt, hours, pct, waitText } from '../ui.jsx'

export default function MapPage({ job, onNav }) {
  const nocase = job.mode === 'nocase'
  const [level, setLevel] = useState(nocase ? 'step' : 'step')
  const [step, setStep] = useState('')
  const [d, setD] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [dir, setDir] = useState('TB')
  const [apps, setApps] = useState(3)
  const [nodeCov, setNodeCov] = useState(100)
  const [pathCov, setPathCov] = useState(100)
  const [order, setOrder] = useState(true)
  const [selected, setSelected] = useState(null)
  useEffect(() => {
    setBusy(true); setError(''); setSelected(null)
    api.map(job.job_id, level, level === 'activity' ? step : null).then(r => { setD(r); if (level === 'activity' && !step) setStep(r.step || '') })
      .catch(e => setError(e.message)).finally(() => setBusy(false))
  }, [job.job_id, level, step])
  const total = (d?.nodes || []).reduce((s, n) => s + Number(n.hours || 0), 0)
  const observed = (d?.edges || []).filter(e => e.kind !== 'order')
  const mapQuery = new URLSearchParams({ level, dir, ...(level === 'activity' && step ? { step } : {}) }).toString()
  return <>
    <WorkspaceHeader kicker={nocase ? 'STEP 5 · PROCESS MAP' : 'STEP 6 · PROCESS MAP'} title="Process Map">
      {nocase ? 'Each box shows its hours and the applications used in it. Select a box to see every application, the Level 4 activities, how a run usually goes and the screens linked to it.'
        : 'The intended flow you confirmed on Review steps: the steps of your data (Level 3) in their stages (Level 2). Each box shows how many cases reach it; grey dashed arrows are the intended order, blue arrows what the cases really did. Select a box for its applications, Level 4 and waits.'}
    </WorkspaceHeader>
    {d && !nocase && <div className={'conformance-strip' + (d.confirmed ? '' : ' warn')}>
      <div><b>{d.order_source?.label || 'Order found in the data'}</b>{d.flow ? ` · flow confidence ${d.flow.band} (${d.flow.score})` : ''}</div>
      <ul>{(d.conformance || []).map((l, i) => <li key={i}>{l}</li>)}</ul>
      {!d.confirmed && <p>The activities are not confirmed against the approved steps yet. <button className="text-button" onClick={() => onNav('definition')}>Review steps</button></p>}
    </div>}
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {d && nocase && d.unassigned_share > 0.3 && <SummaryNote warn>{pct(d.unassigned_share)} of the hours ({hours(d.unassigned_hours)}) are not linked to a step yet, so the map shows only part of the work. Approve the AI task “Link screens to steps” on <button className="text-button" onClick={() => onNav('nc_overview')}>Overview and analysis</button> or assign units on <button className="text-button" onClick={() => onNav('nc_steps')}>Review steps</button>.</SummaryNote>}
    <Panel title={nocase ? 'Observed process map (no case ID)' : level === 'observed' ? 'Every step as observed (case ID)' : 'Intended flow, compared with the cases (case ID)'}
      subtitle={nocase ? `Business order: ${d?.order_source?.label || '–'} · blue arrows = transitions seen in at least ${d?.min_episodes ?? '–'} episodes by ${d?.min_actors ?? '–'}+ people` : level === 'observed' ? 'Directly-follows transitions between activities within each case' : `Intended order: ${d?.order_source?.label || '–'} · blue arrows = transitions seen in the cases`}
      action={<div className="graph-options">
        <label className="map-select-label">Map detail</label>
        <select value={level} onChange={e => { setLevel(e.target.value); setStep('') }} disabled={busy}>{(d?.levels || [{ value: 'step', label: 'Level 3 · Steps' }]).map(l => <option key={l.value} value={l.value}>{l.label}</option>)}</select>
        {level === 'activity' && nocase && <select value={step} onChange={e => setStep(e.target.value)}>{(d?.steps || []).map(s => <option key={s}>{s}</option>)}</select>}
        <a className="text-button" href={fileUrl(job.job_id, `map.html?${mapQuery}`)}><ArrowDownToLine size={14} /> HTML</a>
        <a className="text-button" href={fileUrl(job.job_id, `map.pdf?${mapQuery}`)}><ArrowDownToLine size={14} /> PDF</a>
      </div>}>
      <div className="map-view-status"><span className="map-view-badge"><GitBranch size={14} />{nocase ? 'NO CASE ID' : 'CASE ID'}</span>
        <span>{fmt(d?.nodes?.filter(n => !n.missing).length)} boxes · {fmt(observed.length)} observed transitions{d?.missing?.length ? ` · ${d.missing.length} step(s) not seen` : ''}</span>
        {busy && <span className="map-updating"><LoaderCircle size={14} className="spin" /> Updating…</span>}</div>
      <div className="business-graph-controls">
        <label>Boxes covering <strong>{nodeCov}%</strong> of {nocase ? 'hours' : 'events'}<input type="range" min="20" max="100" step="5" value={nodeCov} onChange={e => setNodeCov(Number(e.target.value))} /></label>
        <label>Transitions covering <strong>{pathCov}%</strong><input type="range" min="20" max="100" step="5" value={pathCov} onChange={e => setPathCov(Number(e.target.value))} /></label>
        <label>Direction<select value={dir} onChange={e => setDir(e.target.value)}><option value="TB">Top to bottom</option><option value="LR">Left to right</option></select></label>
        <label>Applications in each box<select value={apps} onChange={e => setApps(Number(e.target.value))}><option value={0}>None</option><option value={3}>Top 3</option><option value={6}>Top 6</option><option value={99}>All</option></select></label>
        {level !== 'activity' && level !== 'observed' && <label className="check-inline"><input type="checkbox" checked={order} onChange={e => setOrder(e.target.checked)} /> Show business order</label>}
      </div>
      {d && <FlowMap key={`${level}:${step}`} nodes={d.nodes} edges={d.edges} direction={dir} appLines={apps} nodeCoverage={nodeCov} pathCoverage={pathCov}
        showOrder={order} groups={level === 'step'} onSelect={level !== 'activity' && (nocase || level !== 'stage') ? (id => { if (!(d.nodes || []).find(n => n.id === id)?.boundary) setSelected(id) }) : null} selected={selected} />}
      {level !== 'activity' && (nocase || level !== 'stage') && <div className="stage-map-hint"><Sparkles size={15} /> Select a box to open its summary, the summary of each Level 4 activity inside it, and “Explain this step with AI” below.</div>}
      {d && nocase && level === 'step' && !observed.length && <SummaryNote>No blue arrows: a transition between two steps is drawn only when it is seen in at least {d.min_episodes} working episodes by {d.min_actors}+ people, and only {fmt(d.multi_step_episodes || 0)} episode(s) contain two different steps here. The grey business order is shown instead; linking more screens to steps (Review steps) can reveal more.</SummaryNote>}
      {d && !nocase && d.documented_missing?.length > 0 && <SummaryNote>Documented steps not found in this data: {d.documented_missing.map(m => <Tag key={m}>{m}</Tag>)} — the data may name them differently (match them on <button className="text-button" onClick={() => onNav('definition')}>Review steps</button>) or they are not recorded in this system.</SummaryNote>}
      {d?.note && <p className="chart-caption">{d.note}</p>}
      <p className="chart-caption">{nocase ? 'Grey dashed arrows: the business order from the approved process (documents or SME). Blue arrows: the order people really moved between steps inside a working episode, kept only when it holds across people and weeks; labels are counts. Steps in the process but not seen in the data are shown empty. Sliders hide small boxes and rare transitions; they never change the numbers.' : level === 'observed' ? 'Arrows are directly-follows transitions inside cases; labels are counts. Repeats of the same activity in a row are listed below the map.' : 'Grey dashed arrows: the intended order (Review steps). Blue arrows: transitions seen inside the cases, with counts. Steps taken out of the flow are listed below the map. Sliders hide small boxes and rare transitions; they never change the numbers.'}</p>
    </Panel>
    {selected && <StagePanel jobId={job.job_id} step={selected} onClose={() => setSelected(null)} />}
    {d && <div className="two-panels">
      <Panel title={nocase ? "Hours per box" : "Events per activity"} subtitle={nocase ? "Share of the hours on this map" : "Events in all cases"} action={<button className="text-button" onClick={() => downloadCsv(`map_${level}.csv`, d.nodes.map(({ apps: a, ...n }) => n))}><ArrowDownToLine size={14} /> CSV</button>}>
        {nocase ? <HorizontalBars rows={[...d.nodes].filter(n => !n.missing).sort((a, b) => (b.hours || 0) - (a.hours || 0))} nameKey="label" valueKey="hours" suffix=" h" max={14} format={v => fmt(v, 1)} />
          : <HorizontalBars rows={[...d.nodes].filter(n => !n.boundary).sort((a, b) => (b.events || 0) - (a.events || 0))} nameKey="label" valueKey="events" max={14} />}
        {total > 0 && <p className="input-help">{hours(total)} on this map{nocase && d.unassigned_hours ? ` · ${hours(d.unassigned_hours)} not linked to a step` : ''}.</p>}
      </Panel>
      <Panel title="Transitions" subtitle="Observed, most frequent first" action={<button className="text-button" onClick={() => downloadJson(`map_${level}.json`, d)}><ArrowDownToLine size={14} /> JSON</button>}>
        <DataTable rows={[...observed].sort((a, b) => (b.count || 0) - (a.count || 0))} max={14} empty="No transition passed the tests." columns={[{ key: 'source', label: 'From' }, { key: 'target', label: 'To' }, { key: 'count', label: 'Times' }, ...(nocase && level === 'step' ? [{ key: 'episodes', label: 'Episodes' }] : []), ...(!nocase ? [{ key: 'median_wait_h', label: 'Median wait', render: (v, r) => waitText(v, r.overlap_share) }] : [])]} />
        {!nocase && <p className="input-help">Median wait: from the end of one step to the start of the next, in the cases; “overlaps” when the next step usually starts before the previous one ends (work in parallel).</p>}
        {d.other_transitions?.length > 0 && <details className="plain-details"><summary>{d.other_transitions.length} other transition(s) below the thresholds</summary>
          <DataTable rows={d.other_transitions} max={30} columns={[{ key: 'source', label: 'From' }, { key: 'target', label: 'To' }, { key: 'count', label: 'Times' }, { key: 'episodes', label: 'Episodes' }]} /></details>}
      </Panel>
    </div>}
    {d?.matrix?.rows?.length > 0 && <Panel title="Step × application hours" subtitle="Where each step's time is spent, by application">
      <div className="table-wrap"><table><thead><tr><th>Step</th>{d.matrix.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{d.matrix.rows.map((r, i) => { const row = d.matrix.cells[i] || []; const mx = Math.max(0.0001, ...row.map(Number)); return <tr key={r}><td><b>{r}</b></td>
          {d.matrix.columns.map((c, j) => <td key={c} className="heat-cell" style={{ background: row[j] ? `rgba(21,94,239,${0.08 + 0.5 * row[j] / mx})` : undefined }}>{row[j] ? fmt(row[j], 1) : ''}</td>)}</tr> })}</tbody></table></div>
    </Panel>}
    {d?.self_loops?.length > 0 && <SummaryNote>Repeated in a row inside a case (not drawn as arrows): {d.self_loops.map(l => `${l.activity} ${fmt(l.count)} times in ${fmt(l.cases)} cases`).join(' · ')}</SummaryNote>}
    {d?.missing?.length > 0 && <SummaryNote>Steps of the process not seen in the data: {d.missing.map(m => <Tag key={m}>{m}</Tag>)} — {nocase ? 'they may happen outside the recorded applications, or no rule links their screens yet.' : 'no activity of the data is linked to them (Review steps), or they are not recorded in this system.'}</SummaryNote>}
    {!nocase && d?.other_steps?.length > 0 && <p className="input-help">Not in the intended flow (not drawn): {d.other_steps.join(', ')}. Choose “All steps as observed” to see them.</p>}
    <AIMapPanel job={job} />
    {!nocase && <div className="button-row"><Button onClick={() => onNav('report')}>Next: Report <ArrowRight size={15} /></Button></div>}
  </>
}
