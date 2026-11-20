import React, { useEffect, useState } from 'react'
import { Layers3, X } from 'lucide-react'
import { api, downloadCsv } from '../api.js'
import StageAIPanel, { Level4Summaries } from './StageAIPanel.jsx'
import { DataTable, HorizontalBars, Loading, StatusBanner, SummaryNote, Tag, colors, fmt, hours, pct, waitText } from '../ui.jsx'

/** Everything about the selected step (or stage): computed locally from the data, no AI. */
export default function StagePanel({ jobId, step, onClose }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState('')
  const reload = () => api.stage(jobId, step).then(setD).catch(e => setError(e.message))
  useEffect(() => { setD(null); setError(''); reload() }, [jobId, step])
  if (error) return <StatusBanner onClose={onClose}>{error}</StatusBanner>
  if (!d) return <Loading>Loading the step…</Loading>
  if (d.kind === 'case_step') { const c = d.step
    return <section className="stage-inspector">
      <div className="stage-inspector-heading"><div><div className="stage-inspector-eyebrow"><Layers3 size={15} /> SELECTED ACTIVITY (CASE ID)</div><h3>{c.step}</h3>
        <p>Computed from the data on this computer. No AI is involved in these numbers.</p></div>
        <button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button></div>
      <div className="mini-kpis">
        <div><span>Cases</span><strong>{fmt(c.cases)}</strong></div><div><span>Share of cases</span><strong>{pct(c.case_share)}</strong></div>
        <div><span>Events</span><strong>{fmt(c.events)}</strong></div><div><span>Hours</span><strong>{c.hours != null ? hours(c.hours) : '–'}</strong></div>
        <div><span>People</span><strong>{fmt(c.people)}</strong></div><div><span>Median time</span><strong>{c.median_processing_h != null ? `${fmt(c.median_processing_h * 60, 1)} min` : '–'}</strong></div>
        <div><span>Repeated in cases</span><strong>{c.rework ? pct(c.rework.share) : '0%'}</strong></div><div><span>Applications</span><strong>{fmt((c.apps || []).length)}</strong></div>
      </div>
      <Level4Summaries level4={d.level4} explanation={d.explanation} />
      <StageAIPanel jobId={jobId} step={step} onDone={reload} />
      <div className="stage-inspector-layout">
        <div><h4>Comes before it (most frequent)</h4>
          <DataTable rows={c.before} max={4} empty="Nothing before: it starts the case." columns={[{ key: 'from', label: 'From' }, { key: 'count', label: 'Times' }, { key: 'median_wait_h', label: 'Median wait', render: (v, r) => waitText(v, r.overlap_share) }]} /></div>
        <div><h4>Comes after it (most frequent)</h4>
          <DataTable rows={c.after} max={4} empty="Nothing after: it ends the case." columns={[{ key: 'to', label: 'To' }, { key: 'count', label: 'Times' }, { key: 'median_wait_h', label: 'Median wait', render: (v, r) => waitText(v, r.overlap_share) }]} /></div>
      </div>
      <div className="stage-inspector-layout">
        <div><h4>Applications in this activity</h4><HorizontalBars rows={c.apps} nameKey="app" valueKey="events" max={6} /></div>
        <div>{c.rework && <SummaryNote>{`Repeated in ${pct(c.rework.share)} of its cases (${fmt(c.rework.extra_occurrences)} extra occurrences).`}{d.level4?.source === 'clicks' ? ' With click-level data every click is one occurrence, so repeats count clicks, not redone work.' : ''}</SummaryNote>}</div>
      </div>
    </section> }
  if (d.kind === 'stage') return <section className="stage-inspector">
    <div className="stage-inspector-heading"><div><div className="stage-inspector-eyebrow"><Layers3 size={15} /> SELECTED STAGE (LEVEL 2)</div><h3>{step}</h3>
      <p>{hours(d.stage.hours)} · {fmt(d.stage.events)} events · {d.stage.actors} people</p></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button></div>
    <DataTable rows={d.steps} columns={[{ key: 'step', label: 'Step (Level 3)' }, { key: 'hours', label: 'Hours', digits: 1 }, { key: 'share_of_hours', label: 'Share', render: v => pct(v) }, { key: 'actors', label: 'People' }, { key: 'runs', label: 'Runs' }]} />
  </section>
  const s = d.step, ins = d.inside || {}
  return <section className="stage-inspector">
    <div className="stage-inspector-heading">
      <div><div className="stage-inspector-eyebrow"><Layers3 size={15} /> SELECTED STEP (LEVEL 3){s.stage ? ` · STAGE: ${s.stage}` : ''}</div><h3>{s.step}</h3>
        <p>Computed from the data on this computer. No AI is involved in these numbers.</p></div>
      <button className="icon-button" onClick={onClose} aria-label="Close"><X size={18} /></button>
    </div>
    <div className="mini-kpis">
      <div><span>Hours</span><strong>{hours(s.hours)}</strong></div><div><span>Share of hours</span><strong>{pct(s.share_of_hours)}</strong></div>
      <div><span>Events</span><strong>{fmt(s.events)}</strong></div><div><span>People</span><strong>{fmt(s.actors)}</strong></div>
      <div><span>Runs</span><strong>{fmt(s.runs)}</strong></div><div><span>Median run</span><strong>{fmt((s.median_run_s || 0) / 60, 1)} min</strong></div>
      <div><span>Repeated runs</span><strong>{pct(s.repetition_share)}</strong></div><div><span>App switches / run</span><strong>{fmt(s.switches_per_run, 1)}</strong></div>
    </div>
    <Level4Summaries level4={d.level4} explanation={d.explanation} />
    <StageAIPanel jobId={jobId} step={step} onDone={reload} />
    <div className="stage-inspector-layout">
      <div>
        <h4>Applications and hours in this step</h4>
        <HorizontalBars rows={d.apps} nameKey="app" valueKey="hours" suffix=" h" max={12} format={v => fmt(v, 2)} />
        {s.hours_by_source && <p className="input-help">How the hours were linked to this step: {Object.entries(s.hours_by_source).map(([k, v]) => `${k} ${fmt(v, 1)} h`).join(' · ')}</p>}
      </div>
      <div>
        <h4>Level 4 · activities inside the step</h4>
        <DataTable rows={(ins.time_split || []).length ? ins.time_split : d.activities} max={10}
          columns={(ins.time_split || []).length ? [{ key: 'activity', label: 'Activity' }, { key: 'hours', label: 'Hours', digits: 2 }, { key: 'share', label: 'Share', render: v => pct(v) }, { key: 'median_per_run_s', label: 'Median / run (s)', digits: 1 }, { key: 'work_type', label: 'Type', render: v => <Tag>{v}</Tag> }]
            : [{ key: 'activity', label: 'Activity' }, { key: 'hours', label: 'Hours', digits: 2 }, { key: 'events', label: 'Events' }]} empty="No Level 4 activities for this step yet." />
      </div>
    </div>
    {ins.patterns?.length > 0 && <div className="stage-inspector-layout">
      <div><h4>How a run usually goes</h4>
        <DataTable rows={ins.patterns} max={6} columns={[{ key: 'pattern', label: 'Pattern (→ order, ⇄ back and forth)' }, { key: 'runs', label: 'Runs' }, { key: 'share', label: 'Share', render: v => pct(v) }]} />
        <p className="input-help">Starts with <b>{ins.start_activity || '–'}</b>, ends with <b>{ins.end_activity || '–'}</b>. Back-and-forth in {pct(ins.back_and_forth_share)} of runs; rework in {pct(ins.rework_share)} ({ins.rework_rule}).</p></div>
      <div><h4>Signals to look at</h4>
        <ul className="insight-list">
          {(ins.automation_candidates || []).map((a, i) => <li key={i}>Repetitive activity: <b>{a.activity}</b> ({fmt(a.events)} events, in {pct(a.pattern_share)} of the usual pattern).</li>)}
          {ins.variation?.flagged && <li>Run time differs a lot between people: {(ins.variation.reasons || []).join('; ')}</li>}
          {!ins.variation?.flagged && <li>Run time is similar across people (variation {fmt(ins.variation?.cv, 2)}).</li>}
          <li>Median {fmt(ins.median_apps_per_run, 1)} application(s) per run, {fmt(ins.switches_per_run, 1)} switches per run.</li>
        </ul>
        <SummaryNote warn>Signals point to where to look; they do not prove waste or an automation case. Check them with the SME.</SummaryNote></div>
    </div>}
    <h4>Screens and clicks linked to this step (largest first)</h4>
    <DataTable rows={d.screens} max={15} onExport={() => downloadCsv(`screens_${s.step}.csv`, d.screens)} columns={[{ key: 'app', label: 'Application' }, { key: 'screen', label: 'Screen' }, { key: 'click', label: 'Click' },
      { key: 'events', label: 'Events' }, { key: 'hours', label: 'Hours', digits: 2 }, { key: 'source', label: 'Linked by', render: v => <Tag tone={v === 'rule' ? 'positive' : 'warning'}>{v === 'rule' ? 'approved rule' : v === 'ai' ? 'AI proposal' : v}</Tag> }]} />
  </section>
}
