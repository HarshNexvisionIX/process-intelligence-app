import React, { useEffect, useState } from 'react'
import { ArrowDownToLine, BrainCircuit, FileText, Quote, RefreshCcw, Sparkles } from 'lucide-react'
import { api, fileUrl } from '../api.js'
import AIPackages from '../components/AIPackages.jsx'
import { Button, DataTable, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader } from '../ui.jsx'

/** The report's summary and what it rests on: checked facts only (no AI), or worded by the AI from the facts. */
function Summary({ s }) {
  if (!s?.available) return null
  return <Panel title="Summary" subtitle={`Report summary · ${s.source}`}
    action={<Tag tone={s.ai ? 'positive' : 'neutral'}>{s.ai ? 'AI wording, numbers from the facts' : 'no AI'}</Tag>}>
    {!s.ai && s.reason && <p className="input-help">AI not used: {s.reason}</p>}
    {s.mode === 'nocase' ? <>
      {s.overview && <div className="summary-lead"><Quote size={16} /><p>{s.overview}</p></div>}
      {s.steps?.length > 0 && <><h4 className="sub-h">Per step</h4><ul className="insight-list">{s.steps.map(x => <li key={x.step}>{x.text}</li>)}</ul></>}
      {s.inside?.length > 0 && <><h4 className="sub-h">Inside each step</h4><ul className="insight-list">{s.inside.map(x => <li key={x.step}>{x.text}</li>)}</ul></>}
      {s.checks && <p className="input-help">{s.checks.passed} checks passed, {s.checks.failed} failed · generated {s.generated}</p>}
    </> : <>
      {(s.summary || []).length > 0 && <div className="summary-lead"><Quote size={16} /><div>{s.summary.map((t, i) => <p key={i}>{t}</p>)}</div></div>}
      <div className="two-panels in-panel">
        {(s.findings || []).length > 0 && <div><h4 className="sub-h">Findings</h4><ul className="insight-list">{s.findings.map((t, i) => <li key={i}>{t}</li>)}</ul></div>}
        {(s.recommendations || []).length > 0 && <div><h4 className="sub-h">Recommendations</h4><ul className="insight-list">{s.recommendations.map((t, i) => <li key={i}>{t}</li>)}</ul></div>}
      </div>
    </>}
  </Panel>
}

export default function ReportPage({ job, onJob, onNav }) {
  const nocase = job.mode === 'nocase'
  const reportDone = job.steps?.at(-1)?.done
  const ctxDone = job.steps?.find(s => s.key === 'context')?.done
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [res, setRes] = useState(null)
  const [sum, setSum] = useState(null)
  const [stamp, setStamp] = useState(Date.now())
  const [pkKey, setPkKey] = useState(0)
  const loadSummary = () => api.summary(job.job_id).then(setSum).catch(() => setSum(null))
  useEffect(() => { loadSummary() }, [job.job_id])
  async function gen(useAi) {
    setBusy(useAi ? 'ai' : 'plain'); setError('')
    try { const r = await api.reportGenerate(job.job_id, useAi); setRes(r); setStamp(Date.now()); setPkKey(k => k + 1); loadSummary(); onJob(job.job_id) }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }
  return <>
    <WorkspaceHeader kicker={job.mode === "nocase" ? "STEP 6 · REPORT" : "STEP 7 · REPORT"} title="Report"
      actions={reportDone && <div className="button-row">
        <a className="btn outline" href={fileUrl(job.job_id, 'report.html')} download={`report_${job.job_id}.html`}><ArrowDownToLine size={15} /> HTML</a>
        <a className="btn outline" href={fileUrl(job.job_id, 'report.xlsx')}><ArrowDownToLine size={15} /> Excel</a>
        <a className="btn outline" href={fileUrl(job.job_id, 'facts.json')}><ArrowDownToLine size={15} /> Facts JSON</a>
        <a className="btn outline" href={fileUrl(job.job_id, 'stakeholder.html')}><ArrowDownToLine size={15} /> Stakeholder HTML</a>
        <a className="btn" href={fileUrl(job.job_id, 'report.pdf')}><ArrowDownToLine size={15} /> Stakeholder PDF</a></div>}>
      Every number in the report is a checked fact. The AI may only word the summary from the facts; the app inserts and checks every number.
    </WorkspaceHeader>
    {error && <StatusBanner onClose={() => setError('')}>{error}</StatusBanner>}
    {!ctxDone && <StatusBanner type="warning">Business context is required before the report. <button className="text-button" onClick={() => onNav('context')}>Go to Business context</button></StatusBanner>}
    <div className="run-cards">
      <div className="run-card"><div className="run-num">1</div><div><h3>Generate from the checked facts</h3><p>No AI: the summary is written from the facts. Also prepares the summary request for review (nothing is sent).</p>
        <Button onClick={() => gen(false)} loading={busy === 'plain'} disabled={!ctxDone}><RefreshCcw size={15} /> Generate report</Button></div></div>
      <div className="run-card"><div className="run-num">2</div><div><h3>Review the summary request</h3><p>Fact IDs, labels and values only; never raw data or names.</p><a className="text-button" href="#report-ai">Open the request</a></div></div>
      <div className="run-card"><div className="run-num">3</div><div><h3>Generate with AI wording</h3><p>Only when the request is approved. Any problem in the AI text → the fact-only summary is kept.</p>
        <Button onClick={() => gen(true)} loading={busy === 'ai'} disabled={!ctxDone}><BrainCircuit size={15} /> Generate with approved AI</Button></div></div>
    </div>
    {reportDone && <Summary s={sum} />}
    {res?.checks && <Panel title="Checks behind the numbers" subtitle={`${res.checks.passed} passed · ${res.checks.failed} failed`}>
      <DataTable rows={res.checks.checks} max={40} columns={[{ key: 'passed', label: '', render: v => <Tag tone={v ? 'positive' : 'warning'}>{v ? 'pass' : 'fail'}</Tag> }, { key: 'check', label: 'Check' }, { key: 'detail', label: 'Detail' }]} />
    </Panel>}
    {reportDone && <div className="done-strip"><Sparkles size={18} /><span>Next: turn these checked facts into a seven-section stakeholder story on <b>AI Analyst</b> (approve-before-send).</span><Button onClick={() => onNav('readout')}>Open AI Analyst</Button></div>}
    {reportDone ? <Panel title="Report preview" subtitle="The same file as the HTML download" action={<a className="text-button" href={fileUrl(job.job_id, 'report.html')} target="_blank" rel="noreferrer"><FileText size={14} /> Open in a new tab</a>}>
      <iframe title="report" className="report-frame" src={`${fileUrl(job.job_id, 'report.html')}?t=${stamp}`} />
    </Panel> : <SummaryNote>No report yet.</SummaryNote>}
    <div id="report-ai"><Panel title="AI request for the summary wording"><AIPackages jobId={job.job_id} kind="report" refreshKey={pkKey} compact /></Panel></div>
  </>
}
