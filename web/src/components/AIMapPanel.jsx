import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, BarChart3, LockKeyhole, ShieldCheck, Sparkles, Zap } from 'lucide-react'
import { api, downloadJson } from '../api.js'
import AIPackages from './AIPackages.jsx'
import FlowMap from './FlowMap.jsx'
import { Button, Panel, SectionHeader, StatusBanner, SummaryNote, Tag, fmt } from '../ui.jsx'

/** ProcessLens "Enhance the map with AI": one request from a compact aggregate summary, approved before it is sent. */
export default function AIMapPanel({ job }) {
  const nocase = job.mode === 'nocase'
  const [scope, setScope] = useState(nocase ? 'navigation' : 'case')
  const [budget, setBudget] = useState('compact')
  const [d, setD] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [msg, setMsg] = useState('')
  const [pkKey, setPkKey] = useState(0)
  const load = () => api.aiMap(job.job_id, scope, budget).then(setD).catch(e => setError(e.message))
  useEffect(() => { load() }, [job.job_id, scope, budget])
  const act = async (name, fn) => { setBusy(name); setError(''); setMsg(''); try { await fn() } catch (e) { setError(e.message) } finally { setBusy('') } }
  const prepare = () => act('prepare', async () => { const r = await api.aiMapPrepare(job.job_id, { scope, budget }); setMsg(r.prepared ? 'Prepared. Review and approve the request below; nothing was sent.' : `Not prepared: ${(r.blocked || []).map(b => b.why).join('; ')}`); setPkKey(k => k + 1) })
  const generate = () => act('generate', async () => { await api.aiMapGenerate(job.job_id, { scope, budget }); await load(); setPkKey(k => k + 1); setMsg('Conceptual map written.') })
  if (!d) return error ? <StatusBanner>{error}</StatusBanner> : null
  if (!d.ready) return null
  const p = d.payload || {}
  const report = d.saved?.report
  const steps = (report?.proposed_steps || []).map((s, i) => ({ id: String(s.id), label: s.label, description: s.explanation, documented: true, index: i + 1 }))
  const edges = (report?.proposed_edges || []).map(e => ({ source: String(e.source), target: String(e.target), kind: 'order' }))
  return <>
    <SectionHeader eyebrow="PRIVACY & TOKEN CONTROL" title="Enhance the map with AI" subtitle="A single request based on compact aggregate nodes and transitions, not raw event rows, random event samples or multiple batch calls." />
    <div className="cost-explainer">
      <div><strong><ShieldCheck size={17} /> Privacy-reviewed first</strong><p>Only fixed semantic categories, step IDs with their approved names, and summarized transition counts leave the app.</p></div>
      <div><strong><Zap size={17} /> One request, not many batches</strong><p>All rows are analyzed locally; only the most informative high-frequency aggregate paths are sent. Rare paths may be excluded.</p></div>
      <div><strong><BarChart3 size={17} /> Token-aware</strong><p>Compact is the default. Estimates count JSON characters, not the full request or output.</p></div>
    </div>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {msg && <SummaryNote>{msg}</SummaryNote>}
    <div className="two-panels ai-layout">
      <Panel title="1. Choose summary size and inspect" subtitle="Both options come from the same privacy-controlled aggregate summary.">
        <label className="field-label">Evidence scope</label>
        <select value={scope} onChange={e => setScope(e.target.value)} disabled={!!busy}>
          <option value="case" disabled={nocase}>Observed cases (Case ID available)</option>
          <option value="navigation">{nocase ? 'No-case steps and navigation (hypothesis only)' : 'No-case navigation (hypothesis only)'}</option>
          <option value="combined">Both evidence types (larger)</option></select>
        <label className="field-label">Summary profile</label>
        <select value={budget} onChange={e => setBudget(e.target.value)} disabled={!!busy}>
          <option value="compact">Compact · lower token use (recommended)</option><option value="detailed">Detailed · more frequent paths</option></select>
        <div className="privacy-details"><div><span>Approx. JSON tokens</span><strong>{fmt(d.approx_input_tokens)}</strong></div><div><span>Network requests</span><strong>1 when approved</strong></div>
          <div><span>Raw event rows</span><strong>Excluded</strong></div><div><span>Graph nodes (case / navigation)</span><strong>{p.case_flow?.nodes?.length || 0} / {p.no_case_navigation?.nodes?.length || 0}</strong></div></div>
        <div className="json-label">EXACT OUTGOING SUMMARY</div><pre className="json-preview">{JSON.stringify(p, null, 2)}</pre>
        <div className="fingerprint"><LockKeyhole size={13} /> SHA-256 of the request: <code>{String(d.fingerprint || '').slice(0, 20)}…</code></div>
        <button className="text-button" onClick={() => downloadJson(`process_map_${scope}_${budget}_summary.json`, p)}><ArrowDownToLine size={14} /> Save this JSON</button>
      </Panel>
      <div className="ai-side"><Panel title="2. Prepare, approve and generate" subtitle="Only the selected summary is sent, to the providers allowed for this data.">
        <div className="button-row"><Button variant="outline" onClick={prepare} loading={busy === 'prepare'}><Sparkles size={15} /> Prepare this summary</Button>
          <Button onClick={generate} loading={busy === 'generate'}><Sparkles size={15} /> Generate conceptual map</Button></div>
        <p className="input-help">Prepare, then open the request below, read it and approve it with your name. No automatic request occurs.</p>
        <AIPackages jobId={job.job_id} kind="map" refreshKey={pkKey} compact />
      </Panel>
        <Panel title="What the model can infer" subtitle="Interpretation, not replacement of evidence"><ul className="insight-list"><li>Likely generic stage sequence and possible loops</li><li>Possible relationships between frequent transitions</li><li>What needs SME confirmation</li><li>Limitations from missing case IDs and anonymized labels</li></ul></Panel></div>
    </div>
    {report && <div className="ai-report"><div className="report-heading"><Sparkles size={20} /><h2>AI-enhanced conceptual map</h2>
      <button className="text-button" onClick={() => downloadJson('ai_conceptual_process_map.json', report)}><ArrowDownToLine size={15} /> JSON</button></div>
      <Panel title={report.headline || 'Suggested process hypothesis'} subtitle={`${d.saved.scope} · ${d.saved.budget} · ${d.saved.provider} / ${d.saved.model}`}>
        <p className="summary-prose">{report.explanation}</p>
        <div className="legend-inline"><Tag tone="warning">Not an observed case flow</Tag><Tag>Confidence: {report.confidence}</Tag></div>
        <FlowMap nodes={steps} edges={edges} direction="LR" appLines={0} groups={false} height={420} />
      </Panel>
      <div className="two-panels"><Panel title="Evidence used"><ul className="insight-list">{(report.evidence || []).map((x, i) => <li key={i}>{x}</li>)}</ul></Panel>
        <Panel title="Limits and SME validation"><ul className="insight-list">{(report.limitations || []).map((x, i) => <li key={i}>{x}</li>)}</ul></Panel></div>
    </div>}
  </>
}
