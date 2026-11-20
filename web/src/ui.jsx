import React from 'react'
import { ArrowDownToLine, CircleAlert, Database, Info, LoaderCircle, X } from 'lucide-react'
import { ResponsiveContainer } from 'recharts'

export const colors = { primary: '#155eef', secondary: '#78a5ed', ink: '#182f50', sky: '#a6c7ff', gray: '#c3d0e3', blue: '#417ee8', green: '#16a34a', amber: '#d97706' }
export const tooltipStyle = { background: '#fff', border: '1px solid #dce7f7', borderRadius: 9, boxShadow: '0 8px 30px #153f7417', color: '#28486c', fontSize: 12 }

export function fmt(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '–'
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })
}
/** A duration in hours as people read it: 45 s, 12 min, 3.4 h, 2.1 d ('–' when unknown). */
export const dur = h => { if (h === null || h === undefined || Number.isNaN(Number(h))) return '–'; const x = Number(h)
  if (x === 0) return '0'; if (x < 1 / 60) return `${Math.max(1, Math.round(x * 3600))} s`; if (x < 1) return `${fmt(x * 60, x * 60 < 10 ? 1 : 0)} min`
  if (x < 48) return `${fmt(x, 1)} h`; return `${fmt(x / 24, 1)} d` }
/** The wait on an arrow: a duration, or "overlaps" when the next step usually starts before this one ends. */
export const waitText = (h, overlap) => (overlap != null && overlap >= 0.5 ? `overlaps (${Math.round(overlap * 100)}% of times)` : dur(h))
export const hours = (h, d = 1) => (h === null || h === undefined ? '–' : `${fmt(h, d)} h`)
export const pct = (x, d = 0) => (x === null || x === undefined ? '–' : `${fmt(100 * Number(x), d)}%`)
export function shortDate(s) {
  if (!s) return '—'
  const d = new Date(String(s).slice(0, 10) + 'T00:00:00')
  return Number.isNaN(d.valueOf()) ? s : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function ChartWrap({ children, height = 280 }) {
  return <div style={{ height, width: '100%' }}><ResponsiveContainer width="100%" height="100%">{children}</ResponsiveContainer></div>
}
export function Panel({ title, subtitle, action, children, className = '' }) {
  return <section className={'panel ' + className}>
    {(title || action) && <div className="panel-head"><div>{title && <h3>{title}</h3>}{subtitle && <p>{subtitle}</p>}</div>{action}</div>}
    {children}
  </section>
}
export function Metric({ label, value, icon: Icon, foot, tint = 'aqua' }) {
  return <div className="metric">{Icon && <div className={'metric-icon ' + tint}><Icon size={19} /></div>}<div className="metric-label">{label}</div><div className="metric-value">{value}</div>{foot && <div className="metric-foot">{foot}</div>}</div>
}
export function Tag({ children, tone = 'neutral', title }) { return <span className={'tag ' + tone} title={title}>{children}</span> }
export function Empty({ children }) { return <div className="no-data"><Database size={29} /><p>{children || 'No data available for this view.'}</p></div> }
export function SummaryNote({ children, warn = false }) { return <div className={'summary-note ' + (warn ? 'warning' : '')}><Info size={16} /><span>{children}</span></div> }
export function StatusBanner({ type = 'error', children, onClose }) {
  return <div className={'status-banner ' + type}><CircleAlert size={17} /><span>{children}</span>{onClose && <button onClick={onClose} aria-label="Close"><X size={16} /></button>}</div>
}
export function Button({ children, onClick, variant = '', disabled = false, loading = false, type = 'button', title }) {
  return <button className={'btn ' + variant} type={type} onClick={onClick} disabled={disabled || loading} title={title}>{loading ? <LoaderCircle size={16} className="spin" /> : null}{children}</button>
}
export function WorkspaceHeader({ kicker, title, children, actions }) {
  return <div className="workspace-header"><div className="workspace-header-row"><div><div className="workspace-kicker">{kicker}</div><h1>{title}</h1>{children && <p>{children}</p>}</div>{actions && <div className="workspace-actions">{actions}</div>}</div></div>
}
export function SectionHeader({ eyebrow, title, subtitle }) {
  return <div className="section-intro"><div className="eyebrow">{eyebrow}</div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
}
export function Tabs({ tabs, value, onChange }) {
  return <div className="section-tabs" role="tablist">{tabs.map(([id, Icon, label]) =>
    <button role="tab" aria-selected={value === id} className={value === id ? 'selected' : ''} key={id} onClick={() => onChange(id)}>{Icon && <Icon size={15} />}{label}</button>)}</div>
}
export function DataTable({ rows = [], columns = [], max = 25, empty = 'No records', onExport }) {
  if (!rows?.length) return <Empty>{empty}</Empty>
  return <div className="table-wrap"><table><thead><tr>{columns.map(c => <th key={c.key}>{c.label}</th>)}</tr></thead><tbody>
    {rows.slice(0, max).map((row, i) => <tr key={i}>{columns.map(c => <td key={c.key}>{c.render ? c.render(row[c.key], row, i) : typeof row[c.key] === 'number' ? fmt(row[c.key], c.digits ?? (Number.isInteger(row[c.key]) ? 0 : 2)) : String(row[c.key] ?? '—')}</td>)}</tr>)}</tbody></table>
    {(rows.length > max || onExport) && <div className="table-footer"><span>Showing {Math.min(max, rows.length)} of {fmt(rows.length)} rows</span>{onExport && <button className="text-button" onClick={onExport}><ArrowDownToLine size={14} /> Export CSV</button>}</div>}
  </div>
}
export function HorizontalBars({ rows = [], nameKey, valueKey, max = 8, suffix = '', color = colors.primary, format }) {
  const data = (rows || []).slice(0, max)
  if (!data.length) return <Empty />
  const biggest = Math.max(1e-9, ...data.map(r => Number(r[valueKey] || 0)))
  return <div className="horizontal-bars">{data.map((item, i) => <div className="bar-line" key={i}>
    <div className="bar-label-row"><span title={String(item[nameKey])}>{String(item[nameKey]).length > 48 ? String(item[nameKey]).slice(0, 45) + '…' : item[nameKey]}</span><strong>{format ? format(item[valueKey]) : fmt(item[valueKey], Number.isInteger(item[valueKey]) ? 0 : 1)}{suffix}</strong></div>
    <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.max(0.5, 100 * Number(item[valueKey] || 0) / biggest)}%`, background: color }} /></div>
  </div>)}</div>
}
export function Field({ label, children, help }) {
  return <label className="mapping-field"><span>{label}</span>{children}{help && <small className="field-help">{help}</small>}</label>
}
export function Steps({ items }) {
  return <ol className="mini-steps">{items.map((t, i) => <li key={i}><b>{i + 1}</b><span>{t}</span></li>)}</ol>
}
export function Loading({ children = 'Loading…' }) {
  return <div className="progress-ribbon"><LoaderCircle size={17} className="spin" />{children}</div>
}
