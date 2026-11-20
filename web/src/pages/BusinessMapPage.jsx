import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, BookOpen, GitBranch } from 'lucide-react'
import { api } from '../api.js'
import FlowMap from '../components/FlowMap.jsx'
import StagePanel from '../components/StagePanel.jsx'
import { Button, DataTable, Field, Loading, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader, fmt, hours, pct } from '../ui.jsx'

/** The business process map: the approved process (stages, steps, order) and, when an analysis of it exists,
 *  the same process as observed in the data (hours, applications, real transitions). Not the app's own workflow. */
export function BusinessMap({ processKey, defaultJob, embedded = false, onNav }) {
  const [d, setD] = useState(null)
  const [view, setView] = useState('')
  const [obs, setObs] = useState(null)
  const [sel, setSel] = useState(null)
  const [dir, setDir] = useState('TB')
  const [error, setError] = useState('')
  useEffect(() => {
    setD(null); setObs(null); setSel(null); setError('')
    if (!processKey) return
    api.processMap(processKey).then(r => {
      setD(r)
      const pick = r.analyses.find(a => a.job_id === defaultJob) || r.analyses[0]
      setView(pick ? pick.job_id : 'documented')
    }).catch(e => setError(e.message))
  }, [processKey, defaultJob])
  useEffect(() => {
    setObs(null); setSel(null)
    if (view && view !== 'documented') api.map(view, 'step').then(setObs).catch(e => setError(e.message))
  }, [view])
  if (error) return <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>
  if (!processKey) return <SummaryNote>Choose a process.</SummaryNote>
  if (!d) return <Loading />
  const analysis = d.analyses.find(a => a.job_id === view)
  const nocase = analysis?.mode === 'nocase'
  const documentedNodes = d.nodes.map((n, i) => ({ ...n, index: i + 1 }))
  return <>
    <Panel title={`${d.name} · business process map`}
      subtitle={view === 'documented' ? `As documented: ${d.provenance.label}. Stages and steps in business order.` : `As observed in ${analysis?.source_name}: hours and applications per step, the business order (grey) and the transitions seen in the data (blue).`}
      action={<div className="graph-options"><label className="map-select-label">Show</label>
        <select value={view} onChange={e => setView(e.target.value)}>
          <option value="documented">As documented (approved process)</option>
          {d.analyses.map(a => <option key={a.job_id} value={a.job_id}>As observed: {a.source_name} · {String(a.created_at || '').slice(0, 10)}</option>)}
        </select>
        <select value={dir} onChange={e => setDir(e.target.value)}><option value="TB">Top to bottom</option><option value="LR">Left to right</option></select>
        <a className="text-button" href={`/api/processes/${encodeURIComponent(processKey)}/map.html?view=${encodeURIComponent(view)}&dir=${dir}`}><ArrowDownToLine size={14} /> HTML</a>
        <a className="text-button" href={`/api/processes/${encodeURIComponent(processKey)}/map.pdf?view=${encodeURIComponent(view)}&dir=${dir}`}><ArrowDownToLine size={14} /> PDF</a></div>}>
      {!d.nodes.length && <SummaryNote warn>This process has no approved steps yet. Propose them from the documents on Business context, or type them in Process library → Stages and steps.</SummaryNote>}
      {view === 'documented' ? (d.nodes.length > 0 && <FlowMap nodes={documentedNodes} edges={d.edges} direction={dir} appLines={0} groups />)
        : !obs ? <Loading>Loading the observed map…</Loading>
          : <><div className="map-view-status"><span className="map-view-badge"><GitBranch size={14} />{nocase ? 'NO CASE ID' : 'CASE ID'}</span>
              <span>{fmt(obs.nodes.filter(n => !n.missing).length)} steps seen · {fmt(obs.edges.filter(e => e.kind !== 'order').length)} observed transitions{obs.missing?.length ? ` · ${obs.missing.length} step(s) not seen` : ''}</span>
              {nocase && obs.unassigned_share != null && <span>{pct(1 - obs.unassigned_share)} of hours linked to a step</span>}</div>
            <FlowMap key={view} nodes={obs.nodes} edges={obs.edges} direction={dir} appLines={3} groups onSelect={id => { if (!(obs.nodes || []).find(n => n.id === id)?.missing) setSel(id) }} selected={sel} /></>}
      {view !== 'documented' && nocase && <p className="chart-caption">Select a step for its applications, Level 4 activities and screens. Empty dashed boxes are steps of the process not seen in the data.</p>}
      {embedded && d.analyses.length === 0 && <p className="input-help">No analysis of this process yet: import a file and choose this process on Business context to see it as observed.</p>}
    </Panel>
    {sel && view !== 'documented' && <StagePanel jobId={view} step={sel} onClose={() => setSel(null)} />}
    {view === 'documented' && d.nodes.length > 0 && <Panel title="Steps in business order">
      <DataTable rows={documentedNodes} max={200} columns={[{ key: 'index', label: '#' }, { key: 'group', label: 'Stage' }, { key: 'label', label: 'Step' }, { key: 'description', label: 'Description' }, { key: 'exception', label: 'Exception' }]} />
    </Panel>}
    {!embedded && onNav && view !== 'documented' && <div className="button-row"><Button variant="outline" onClick={() => onNav(nocase ? 'nc_map' : 'case_map')}>Open the full map tools (levels, sliders, matrix)</Button></div>}
  </>
}

export default function BusinessMapPage({ job, onNav }) {
  const [procs, setProcs] = useState(null)
  const [key, setKey] = useState(job?.process || '')
  useEffect(() => { api.processes().then(p => { setProcs(p); if (!key && p.length) setKey(p[0].key) }).catch(() => setProcs([])) }, [])
  return <>
    <WorkspaceHeader kicker="THE BUSINESS PROCESS · NOT THE APP" title="Business process map">
      The process you are analysing (for example the ICC month-end recharge): its stages and steps as documented and approved, and the same process as observed in the data, with hours and applications per step and the real order of work.
    </WorkspaceHeader>
    {!procs ? <Loading /> : !procs.length ? <SummaryNote>No process yet. Start a <button className="text-button" onClick={() => onNav('start')}>new analysis</button> and give it a process name; on Business context you add its documents and approve its steps.</SummaryNote> : <>
      <div className="inline-form"><Field label="Process"><select value={key} onChange={e => setKey(e.target.value)}>{procs.map(p => <option key={p.key} value={p.key}>{p.name} · {p.steps} steps</option>)}</select></Field>
        <Button variant="outline" onClick={() => onNav('library')}><BookOpen size={15} /> Edit in Process library</Button></div>
      <BusinessMap processKey={key} defaultJob={job?.job_id} onNav={onNav} />
    </>}
  </>
}
