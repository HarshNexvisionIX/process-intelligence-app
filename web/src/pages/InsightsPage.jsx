import React, { useEffect, useState } from 'react'
import { Activity, ArrowDownToLine, CalendarDays, Clock3, Grid2X2, LayoutDashboard, RefreshCcw, ClipboardCopy, ShieldCheck, Users } from 'lucide-react'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, Tooltip, XAxis, YAxis } from 'recharts'
import { api, downloadCsv, fileUrl } from '../api.js'
import { ChartWrap, DataTable, HorizontalBars, Loading, Metric, Panel, StatusBanner, SummaryNote, Tabs, Tag, WorkspaceHeader, colors, fmt, shortDate, tooltipStyle } from '../ui.jsx'

const DAYS = ['', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function Overview({ r }) {
  const o = r.overview, p = r.participation
  return <>
    <div className="metrics-grid">
      <Metric label="Events" value={fmt(o.events)} icon={Activity} foot={`${fmt(o.days)} days with events`} />
      <Metric label="People" value={fmt(o.participants)} icon={Users} foot={`${p.regular} regular · ${p.occasional} occasional`} tint="blue" />
      <Metric label="Hours" value={`${fmt(o.hours, 1)} h`} icon={Clock3} foot="Active time (see note)" tint="orange" />
      <Metric label="Applications" value={fmt(o.apps)} icon={Grid2X2} foot={`${shortDate(o.first_date)} – ${shortDate(o.last_date)}`} tint="violet" />
    </div>
    <div className="two-panels wide-left">
      <Panel title="People active per day" subtitle="Unique people with events each day">
        <ChartWrap><AreaChart data={p.daily} margin={{ top: 12, right: 15, left: -26, bottom: 0 }}><defs><linearGradient id="ppl" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#155eef" stopOpacity={0.28} /><stop offset="100%" stopColor="#155eef" stopOpacity={0} /></linearGradient></defs>
          <CartesianGrid vertical={false} stroke="#e5ecf7" /><XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 11 }} minTickGap={20} /><YAxis allowDecimals={false} tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} />
          <Area type="monotone" dataKey="participants" name="People" stroke={colors.primary} fill="url(#ppl)" strokeWidth={2.5} /></AreaChart></ChartWrap>
      </Panel>
      <Panel title="Most-used applications" subtitle="By hours"><HorizontalBars rows={r.applications.by_app} nameKey="application" valueKey="hours" suffix=" h" max={8} format={v => fmt(v, 1)} /></Panel>
    </div>
    <div className="two-panels">
      <Panel title="Hours by hour of day" subtitle="When the work happens (UTC)"><ChartWrap height={230}><BarChart data={r.time.hour_of_day} margin={{ top: 8, right: 10, left: -25, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5ecf7" /><XAxis dataKey="hour" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} formatter={v => `${fmt(v, 2)} h`} /><Bar dataKey="hours" fill={colors.blue} radius={[4, 4, 0, 0]} /></BarChart></ChartWrap></Panel>
      <Panel title="Hours by weekday"><ChartWrap height={230}><BarChart data={r.time.weekday.map(x => ({ ...x, day: DAYS[x.weekday] }))} margin={{ top: 8, right: 10, left: -25, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5ecf7" /><XAxis dataKey="day" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} formatter={v => `${fmt(v, 2)} h`} /><Bar dataKey="hours" fill={colors.secondary} radius={[4, 4, 0, 0]} /></BarChart></ChartWrap></Panel>
    </div>
  </>
}

function Participants({ r }) {
  const p = r.participation
  const dates = Array.from(new Set(p.daily.map(x => x.date))).slice(-30)
  const seen = new Set(p.attendance.map(x => x.participant + '|' + x.date))
  return <>
    <div className="metrics-grid three"><Metric label="Regular people" value={fmt(p.regular)} icon={Users} foot={`Active on ${p.min_active_days}+ days`} /><Metric label="Occasional people" value={fmt(p.occasional)} icon={Users} tint="orange" foot="Fewer active days" /><Metric label="Busiest day" value={fmt(Math.max(0, ...p.daily.map(x => x.participants)))} icon={CalendarDays} tint="blue" foot="People on one day" /></div>
    <div className="two-panels"><Panel title="People active by day"><ChartWrap><LineChart data={p.daily} margin={{ top: 12, right: 10, left: -25, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e9eff4" /><XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 11 }} minTickGap={24} /><YAxis tick={{ fontSize: 11 }} allowDecimals={false} /><Tooltip contentStyle={tooltipStyle} /><Line type="monotone" dataKey="participants" stroke={colors.primary} strokeWidth={2.6} dot={{ r: 2.5 }} /></LineChart></ChartWrap></Panel>
      <Panel title="Active days per person"><HorizontalBars rows={[...p.people].sort((a, b) => b.active_days - a.active_days)} nameKey="participant" valueKey="active_days" color={colors.blue} max={12} /></Panel></div>
    <Panel title="Attendance" subtitle="People × the last 30 dates with events">
      <div className="heatmap-container"><div className="heat-row"><span className="heat-id" />{dates.map(d => <span className="heat-day" key={d} title={d}>{String(d).slice(8)}</span>)}</div>
        {p.people.slice(0, 20).map(row => <div className="heat-row" key={row.participant}><span className="heat-id">{row.participant}</span>{dates.map(d => <span key={d} className={'heat-tile ' + (seen.has(row.participant + '|' + d) ? 'active' : '')} title={`${row.participant} · ${d}`} />)}</div>)}
        <div className="heat-legend"><span className="heat-tile" /> no events <span className="heat-tile active" /> events</div></div>
    </Panel>
    <Panel title="People" subtitle="Shown as P01, P02… (never names or IDs)" action={<button className="text-button" onClick={() => downloadCsv('people.csv', p.people)}>Export CSV</button>}>
      <DataTable rows={p.people} max={40} columns={[{ key: 'participant', label: 'Person' }, { key: 'active_days', label: 'Active days' }, { key: 'events', label: 'Events' }, { key: 'hours', label: 'Hours', digits: 1 }, { key: 'attendance', label: 'Attendance', render: v => <Tag tone={v === 'Regular' ? 'positive' : 'warning'}>{v}</Tag> }, { key: 'first_seen', label: 'First' }, { key: 'last_seen', label: 'Last' }]} />
      <SummaryNote>{p.devices_note}</SummaryNote>
    </Panel>
  </>
}

function Applications({ r }) {
  const apps = r.applications.by_app
  const top = apps.slice(0, 6).map(a => a.application)
  const byDate = new Map()
  for (const x of r.applications.daily) { const c = byDate.get(x.date) || { date: x.date }; const k = top.includes(x.application) ? x.application : 'Other'; c[k] = (c[k] || 0) + x.hours; byDate.set(x.date, c) }
  const series = [...top, 'Other']; const pal = ['#155eef', '#417ee8', '#78a5ed', '#a6c7ff', '#1d4ed8', '#93c5fd', '#c3d0e3']
  return <>
    <Panel title="Hours per application per day" subtitle="Top 6 applications, the rest as Other">
      <ChartWrap height={320}><AreaChart data={[...byDate.values()]} margin={{ top: 13, right: 15, left: -19, bottom: 0 }}><CartesianGrid stroke="#e5ecf7" vertical={false} /><XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 11 }} minTickGap={30} /><YAxis tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} formatter={x => `${fmt(x, 2)} h`} />
        {series.map((s, i) => <Area key={s} type="monotone" dataKey={s} stackId="1" fill={pal[i]} fillOpacity={0.8} stroke={pal[i]} />)}</AreaChart></ChartWrap>
      <div className="legend-inline">{series.map((s, i) => <span key={s}><i style={{ background: pal[i] }} />{s}</span>)}</div>
    </Panel>
    <Panel title="Applications" action={<button className="text-button" onClick={() => downloadCsv('applications.csv', apps)}>Export CSV</button>}>
      <DataTable rows={apps} max={40} columns={[{ key: 'application', label: 'Application' }, { key: 'participants', label: 'People' }, { key: 'events', label: 'Events' }, { key: 'hours', label: 'Hours', digits: 2 }]} />
    </Panel>
  </>
}

function Interactions({ r }) {
  const x = r.interactions
  return <>
    <div className="metrics-grid three"><Metric label="Application switches" value={fmt(x.switches)} icon={RefreshCcw} foot="Consecutive events in different apps (same person, same day)" /><Metric label="Copy / paste events" value={fmt(x.copy_paste_events)} icon={ClipboardCopy} foot="Keyword match on the click name" tint="orange" /><Metric label="Distinct clicks (top)" value={fmt(x.top_actions.length)} icon={Activity} tint="blue" foot="Most frequent click names" /></div>
    <div className="two-panels"><Panel title="Switches per day"><ChartWrap><BarChart data={x.switch_daily} margin={{ top: 12, right: 12, left: -25, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5ecf7" /><XAxis dataKey="date" tickFormatter={shortDate} tick={{ fontSize: 11 }} minTickGap={22} /><YAxis tick={{ fontSize: 11 }} /><Tooltip contentStyle={tooltipStyle} /><Bar dataKey="switches" fill={colors.primary} radius={[4, 4, 0, 0]} /></BarChart></ChartWrap></Panel>
      <Panel title="Most frequent application switches" subtitle="Navigation, not case hand-offs"><DataTable rows={x.switch_flows} max={12} columns={[{ key: 'from_application', label: 'From' }, { key: 'to_application', label: 'To' }, { key: 'switches', label: 'Switches' }]} /></Panel></div>
    <div className="two-panels"><Panel title="Copy / paste signals"><DataTable rows={x.copy_paste} max={15} empty="No copy or paste clicks found." columns={[{ key: 'application', label: 'Application' }, { key: 'action', label: 'Click' }, { key: 'events', label: 'Events' }]} /></Panel>
      <Panel title="Most frequent clicks"><HorizontalBars rows={x.top_actions} nameKey="action" valueKey="events" color={colors.blue} max={10} /></Panel></div>
  </>
}

function Quality({ q }) {
  if (!q) return <SummaryNote>No quality information yet.</SummaryNote>
  if (q.mode === 'case') return <>
    <div className="metrics-grid three"><Metric label="Feasibility score" value={q.feasibility == null ? '–' : `${q.feasibility} / 100`} icon={ShieldCheck} foot="From the 12 quality checks" />
      <Metric label="Band" value={q.band || '–'} icon={ShieldCheck} tint="blue" foot="Limits what the report may claim" />
      <Metric label="Rows left out on import" value={fmt(q.summary?.rows_left_out || 0)} icon={Activity} tint="orange" foot={`of ${fmt(q.summary?.rows_in_file)} rows in the file`} /></div>
    {q.dimensions && <Panel title="Score by dimension"><HorizontalBars rows={Object.entries(q.dimensions).map(([k, v]) => ({ k: k.replaceAll('_', ' '), v }))} nameKey="k" valueKey="v" max={10} /></Panel>}
    <Panel title="Checks"><DataTable rows={q.checks} max={30} columns={[{ key: 'status', label: '', render: v => <Tag tone={v === 'pass' ? 'positive' : 'warning'}>{v}</Tag> }, { key: 'title', label: 'Check', render: (v, r) => v || r.key }, { key: 'message', label: 'Result', render: (v, r) => v || r.detail || '' }]} /></Panel>
  </>
  return <>
    <div className="metrics-grid"><Metric label="Rows in the file" value={fmt(q.rows_in_file)} icon={Activity} foot="Before any filter" />
      <Metric label="Rows in scope" value={fmt(q.in_scope)} icon={ShieldCheck} tint="blue" foot={`${fmt(q.left_out_by_filter)} left out by filters`} />
      <Metric label="Unreadable times" value={fmt(q.unreadable_time)} icon={Clock3} tint="orange" foot="Rows dropped: time could not be read" />
      <Metric label="Checks passed" value={`${fmt(q.checks_passed)} / ${fmt((q.checks_passed || 0) + (q.checks_failed || 0))}`} icon={ShieldCheck} tint="violet" foot="Each number computed twice" /></div>
    <Panel title="How the data was read"><div className="data-quality-grid">
      <div><span>Time basis</span><strong>{q.time_basis}</strong></div><div><span>Period</span><strong>{String(q.first_event).slice(0, 10)} to {String(q.last_event).slice(0, 10)}</strong></div>
      <div><span>Columns mapped</span><strong>{(q.mapped || []).join(', ')}</strong></div><div><span>Hours in generic clicks</span><strong>{fmt(q.generic_hours, 1)} h</strong></div>
      <div><span>Hours not linked to a step</span><strong>{q.unassigned_share == null ? '–' : `${fmt(100 * q.unassigned_share, 1)}%`}</strong></div>
      <div><span>Case ID column</span><strong>{q.capabilities?.case_id ? String(q.capabilities.case_id.verdict).replaceAll('_', ' ') : 'none mapped'}</strong></div></div></Panel>
    <Panel title="Checks behind the numbers" subtitle="From the last report"><DataTable rows={q.checks} max={40} empty="Generate the report to run the checks." columns={[{ key: 'passed', label: '', render: v => <Tag tone={v ? 'positive' : 'warning'}>{v ? 'pass' : 'fail'}</Tag> }, { key: 'check', label: 'Check' }, { key: 'detail', label: 'Detail' }]} /></Panel>
  </>
}

export default function InsightsPage({ job }) {
  const [r, setR] = useState(null)
  const [tab, setTab] = useState('overview')
  const [error, setError] = useState('')
  const [days, setDays] = useState(5)
  useEffect(() => { api.insights(job.job_id, days).then(setR).catch(e => setError(e.message)) }, [job.job_id, days])
  return <>
    <WorkspaceHeader kicker="DETERMINISTIC ANALYTICS · NO AI" title="Data Insights"
      actions={r?.available && <a className="btn outline" href={fileUrl(job.job_id, `insights.html?min_active_days=${days}`)}><ArrowDownToLine size={15} /> Insights HTML</a>}>People, applications, time of day, switching and copy / paste, computed from every converted event.</WorkspaceHeader>
    {error && <StatusBanner>{error}</StatusBanner>}
    {!r ? !error && <Loading /> : !r.available ? <SummaryNote>{r.reason}</SummaryNote> : <>
      <Tabs tabs={[['overview', LayoutDashboard, 'Overview'], ['people', Users, 'People'], ['apps', Grid2X2, 'Applications'], ['inter', Activity, 'Interactions'], ['quality', ShieldCheck, 'Data quality']]} value={tab} onChange={setTab} />
      {tab === 'people' && <label className="days-control">Regular = active on at least <input type="number" min="1" max="365" value={days} onChange={e => setDays(Math.max(1, Number(e.target.value) || 1))} /> days</label>}
      {tab === 'overview' && <Overview r={r} />}{tab === 'people' && <Participants r={r} />}{tab === 'apps' && <Applications r={r} />}{tab === 'inter' && <Interactions r={r} />}{tab === 'quality' && <Quality q={r.quality} />}
      <SummaryNote warn>{r.notes.join(' ')}</SummaryNote>
    </>}
  </>
}
