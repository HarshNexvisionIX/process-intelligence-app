import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, ListTree, LockKeyhole, Sparkles } from 'lucide-react'
import { api, downloadJson } from '../api.js'
import AIPackages from './AIPackages.jsx'
import { Button, SummaryNote, StatusBanner, Tag, fmt, hours, pct } from '../ui.jsx'

/** Level 4 summaries of one step: a measured sentence for each Level 4 activity (always, no AI), plus the AI's
 *  business summary of it once "Explain this step with AI" has run. */
export function Level4Summaries({ level4, explanation }) {
  const acts = level4?.activities || []
  const ai = Object.fromEntries(((explanation?.report?.level4_summaries) || []).map(x => [x.activity, x]))
  return <div className="l4-summaries">
    <h4><ListTree size={15} /> Level 4 · summary of each activity inside this step</h4>
    {level4?.note && <p className="input-help">{level4.note}</p>}
    {!acts.length && !level4?.note && <p className="input-help">No Level 4 activities for this step yet.</p>}
    <div className="l4-grid">
      {acts.map((a, i) => <article key={a.activity + i} className="l4-card">
        <header><b>{a.activity}</b>
          <span>{a.hours != null ? hours(a.hours) : ''}{a.share != null ? ` · ${pct(a.share)}` : ''}{a.events != null ? ` · ${fmt(a.events)} ${level4.source === 'clicks' ? 'times' : 'events'}` : ''}</span></header>
        <p className="l4-measured"><Tag>Measured</Tag> {a.summary}</p>
        {ai[a.activity] && <p className="l4-ai"><Tag tone="warning">AI</Tag> {ai[a.activity].summary}
          {ai[a.activity].evidence_patterns && <small> Evidence: {ai[a.activity].evidence_patterns}</small>}</p>}
      </article>)}
    </div>
    {acts.length > 0 && !explanation && <p className="input-help">The AI business summary of each activity appears here after “Explain this step with AI” below.</p>}
  </div>
}

/** ProcessLens "Explain this stage with AI": one request about one step, approved before it is sent. */
export default function StageAIPanel({ jobId, step, onDone }) {
  const [budget, setBudget] = useState('compact')
  const [d, setD] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [pkKey, setPkKey] = useState(0)
  const load = () => api.stageAi(jobId, step, budget).then(setD).catch(e => setError(e.message))
  useEffect(() => { setD(null); setError(''); setMsg(''); load() }, [jobId, step, budget])
  const act = async (name, fn) => { setBusy(name); setError(''); setMsg(''); try { await fn() } catch (e) { setError(e.message) } finally { setBusy('') } }
  const prepare = () => act('prepare', async () => { const r = await api.stageAiPrepare(jobId, { step, budget }); setMsg(r.prepared ? 'Prepared. Open the request below, read it and approve it with your name; nothing was sent.' : `Not prepared: ${(r.blocked || []).map(b => b.why).join('; ')}`); setPkKey(k => k + 1) })
  const generate = () => act('generate', async () => { await api.stageAiGenerate(jobId, { step, budget }); await load(); setPkKey(k => k + 1); setMsg('Explanation written.'); onDone?.() })
  const r = d?.saved?.report
  return <details className="stage-ai" open={!!r}>
    <summary><Sparkles size={15} /> Explain this step with AI {r ? <Tag tone="positive">done</Tag> : <Tag>optional</Tag>}</summary>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {msg && <SummaryNote>{msg}</SummaryNote>}
    {r && <div className="stage-ai-result">
      <div className="legend-inline"><Tag tone="warning">Interpretation, not measured</Tag><Tag>Confidence: {r.confidence}</Tag>
        <span className="input-help">{d.saved.provider} / {d.saved.model} · {d.saved.budget}</span>
        <button className="text-button" onClick={() => downloadJson(`step_explanation_${step}.json`, d.saved)}><ArrowDownToLine size={14} /> JSON</button></div>
      <p className="summary-prose">{r.stage_business_summary}</p>
      {(r.key_work_patterns || []).length > 0 && <><h4>Key work patterns</h4><ul className="insight-list">{r.key_work_patterns.map((k, i) => <li key={i}><b>{k.title}</b> — {k.business_meaning}{k.application_roles ? ` (${k.application_roles})` : ''}{k.evidence_patterns ? <small> Evidence: {k.evidence_patterns}</small> : null}</li>)}</ul></>}
      {(r.proposed_level4_steps || []).length > 0 && <><h4>Proposed Level 4 sub-steps <Tag tone="warning">hypothesis</Tag></h4><ul className="insight-list">{r.proposed_level4_steps.map((k, i) => <li key={i}><b>{k.name}</b> — {k.rationale}{k.supporting_evidence ? <small> Evidence: {k.supporting_evidence}</small> : null}</li>)}</ul></>}
      <div className="two-panels">
        <div><h4>Possible friction points</h4><ul className="insight-list">{(r.possible_friction_points || []).map((x, i) => <li key={i}>{x}</li>)}</ul></div>
        <div><h4>Questions for the SME</h4><ul className="insight-list">{(r.questions_for_sme || []).map((x, i) => <li key={i}>{x}</li>)}</ul></div>
      </div>
      {r.evidence_limitations && <SummaryNote warn>{r.evidence_limitations}</SummaryNote>}
    </div>}
    {d && <div className="stage-ai-request">
      <div className="graph-options"><label className="field-label">Summary profile</label>
        <select value={budget} onChange={e => setBudget(e.target.value)} disabled={!!busy}><option value="compact">Compact · fewer patterns (recommended)</option><option value="detailed">Detailed · more patterns</option></select>
        <span className="input-help">≈ {fmt(d.approx_input_tokens)} JSON tokens · 1 request when approved · no raw rows</span></div>
      <details className="plain-details"><summary>EXACT OUTGOING SUMMARY (JSON)</summary><pre className="json-preview">{JSON.stringify(d.payload, null, 2)}</pre></details>
      <div className="fingerprint"><LockKeyhole size={13} /> SHA-256 of the request: <code>{String(d.fingerprint || '').slice(0, 20)}…</code></div>
      <div className="button-row"><Button variant="outline" onClick={prepare} loading={busy === 'prepare'}><Sparkles size={15} /> Prepare this request</Button>
        <Button onClick={generate} loading={busy === 'generate'}><Sparkles size={15} /> Generate explanation</Button></div>
      <AIPackages jobId={jobId} kind="stage" refreshKey={pkKey} compact />
    </div>}
  </details>
}
