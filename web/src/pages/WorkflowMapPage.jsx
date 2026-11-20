import React, { useEffect, useMemo, useRef, useState } from 'react'
import { ReactFlow, Background, Controls, Handle, Position, MarkerType } from '@xyflow/react'
import { ArrowRight, BrainCircuit, CheckCircle2, CircleDashed, Cog, FileOutput, FileInput, GitBranch, ShieldCheck, UserCheck, Workflow, X } from 'lucide-react'
import { api } from '../api.js'
import { DataTable, Loading, Panel, StatusBanner, SummaryNote, Tag, WorkspaceHeader } from '../ui.jsx'

const KIND = { service: ['Service', Cog, 'svc'], agent: ['AI agent', BrainCircuit, 'agent'], human: ['Human gate', UserCheck, 'human'] }
const STATUS = { done: ['done', 'positive'], partial: ['partial', 'warning'], ready: ['ready', 'warning'], todo: ['not yet', 'neutral'] }
const W = 250, H = 132, GAPX = 34, GAPY = 26, PER_COL = 4, LANE_PAD = 22, HEAD = 46

function AgentNode({ data }) {
  const [label, Icon, cls] = KIND[data.kind] || KIND.service
  const [st, tone] = STATUS[data.status] || STATUS.todo
  return <div className={`agent-card ${cls} ${data.status} ${data.selected ? 'selected' : ''}`}>
    <Handle type="target" position={Position.Left} id="l" /><Handle type="target" position={Position.Top} id="t" />
    <div className="agent-top"><span className="agent-id">{data.id}</span><span className={`agent-kind ${cls}`}><Icon size={12} /> {label}</span></div>
    <div className="agent-name">{data.name}</div>
    <div className="agent-io"><span title="inputs"><FileInput size={12} /> {data.inputs.length} in</span><span title="outputs"><FileOutput size={12} /> {data.outputs.length} out</span>{data.task && <span className="agent-task" title="AI task, behind the approval gate">{data.task}</span>}</div>
    <div className="agent-foot"><Tag tone={tone}>{data.status === 'done' ? <CheckCircle2 size={11} /> : <CircleDashed size={11} />} {st}</Tag></div>
    <Handle type="source" position={Position.Right} id="r" /><Handle type="source" position={Position.Bottom} id="b" />
  </div>
}
function LaneNode({ data }) { return <div className="lane-box" style={{ width: data.w, height: data.h }}><div className="lane-title">{data.label}</div><div className="lane-count">{data.count} components</div></div> }
const nodeTypes = { agent: AgentNode, lane: LaneNode }

export default function WorkflowMapPage({ job }) {
  const [mode, setMode] = useState(job?.mode || 'nocase')
  const [d, setD] = useState(null)
  const [sel, setSel] = useState(null)
  const [error, setError] = useState('')
  const live = job && (job.mode || 'case') === mode
  useEffect(() => { setD(null); setSel(null); api.systemMap(mode, live ? job.job_id : null).then(setD).catch(e => setError(e.message)) }, [mode, job?.job_id])
  const graph = useMemo(() => {
    if (!d) return null
    const nodes = [], edges = []
    let x = 0
    const maxRows = Math.max(...d.lanes.map(l => Math.min(PER_COL, d.nodes.filter(n => n.lane === l.id).length)))
    const laneH = HEAD + maxRows * (H + GAPY) + LANE_PAD
    for (const lane of d.lanes) {
      const items = d.nodes.filter(n => n.lane === lane.id)
      const cols = Math.max(1, Math.ceil(items.length / PER_COL))
      const w = cols * W + (cols - 1) * GAPX + 2 * LANE_PAD
      nodes.push({ id: 'lane-' + lane.id, type: 'lane', position: { x, y: 0 }, data: { label: lane.label, w, h: laneH, count: items.length }, draggable: false, selectable: false, zIndex: -1 })
      items.forEach((n, i) => {
        const c = Math.floor(i / PER_COL), r = i % PER_COL
        nodes.push({ id: n.id, type: 'agent', position: { x: x + LANE_PAD + c * (W + GAPX), y: HEAD + r * (H + GAPY) }, data: { ...n, selected: sel === n.id }, draggable: false })
      })
      x += w + 40
    }
    const byId = Object.fromEntries(d.nodes.map(n => [n.id, n]))
    d.edges.forEach((e, i) => {
      const done = byId[e.source]?.status === 'done'
      const back = d.nodes.indexOf(byId[e.target]) < d.nodes.indexOf(byId[e.source])
      const color = done ? '#155eef' : '#a9b8cc'
      const same = byId[e.source]?.lane === byId[e.target]?.lane && !back
      edges.push({ id: 'e' + i, source: e.source, target: e.target, sourceHandle: same ? 'b' : 'r', targetHandle: same ? 't' : 'l', type: same ? 'smoothstep' : 'default', animated: done && byId[e.target]?.status !== 'done',
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 }, style: { stroke: color, strokeWidth: done ? 1.6 : 1.1, opacity: done ? 0.75 : 0.55, strokeDasharray: back ? '5 5' : undefined } })
    })
    return { nodes, edges }
  }, [d, sel])
  const box = useRef(null)
  function init(inst) {
    if (!graph || !box.current) return
    const lanes = graph.nodes.filter(n => n.type === 'lane')
    const right = Math.max(...lanes.map(n => n.position.x + n.data.w)), bottom = Math.max(...lanes.map(n => n.data.h))
    const W = box.current.clientWidth, H = box.current.clientHeight
    const z = Math.min(1, Math.max(0.72, Math.min(W / (right + 40), H / (bottom + 40))))
    inst.setViewport({ x: right * z < W ? (W - right * z) / 2 : 16, y: 16, zoom: z })
  }
  const node = d?.nodes.find(n => n.id === sel)
  const counts = d ? { all: d.nodes.length, agents: d.nodes.filter(n => n.kind === 'agent').length, human: d.nodes.filter(n => n.kind === 'human').length, done: d.nodes.filter(n => n.status === 'done').length } : {}
  return <>
    <WorkspaceHeader kicker="CONTROL TOWER · HOW THIS APP WORKS (NOT YOUR BUSINESS PROCESS)" title="App workflow map">
      Every service, AI agent and human gate inside this app for the {mode === 'nocase' ? 'no-case-ID' : 'case-ID'} workflow, with what each one takes in and gives out. For the map of your business process, see Business process map.{live ? ` Status is live for the current analysis (${job.source_name}).` : ' Choose an analysis of this kind to see live status.'}
    </WorkspaceHeader>
    {error && <StatusBanner>{error}</StatusBanner>}
    <div className="tower-bar">
      <div className="pill-tabs small"><button className={mode === 'nocase' ? 'active' : ''} onClick={() => setMode('nocase')}><Workflow size={15} /> No case ID</button><button className={mode === 'case' ? 'active' : ''} onClick={() => setMode('case')}><GitBranch size={15} /> Case ID</button></div>
      {d && <div className="tower-kpis"><div><strong>{counts.all}</strong><span>components</span></div><div><strong>{counts.agents}</strong><span>AI agents</span></div><div><strong>{counts.human}</strong><span>human gates</span></div><div><strong>{live ? `${counts.done}/${counts.all}` : '–'}</strong><span>done for this analysis</span></div>
        <div className={d.ai?.available ? 'ok' : 'off'}><strong><ShieldCheck size={16} /></strong><span>AI {d.ai?.available ? 'available' : 'not available'}</span></div></div>}
    </div>
    <div className="tower-legend"><span className="lg svc"><Cog size={12} /> Service: deterministic code, no AI</span><span className="lg agent"><BrainCircuit size={12} /> AI agent: asks the AI only after approval, rules fallback</span><span className="lg human"><UserCheck size={12} /> Human gate: a person decides</span><span className="lg"><i className="line solid" /> data flows (blue = produced)</span><span className="lg"><i className="line dashed" /> feedback loop</span></div>
    {!graph ? <Loading /> : <div className="tower-wrap">
      <div className="tower-canvas" ref={box}><ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} onInit={init} minZoom={0.2} maxZoom={1.6}
        nodesConnectable={false} elementsSelectable={false} onNodeClick={(_, n) => n.type === 'agent' && setSel(n.id)}><Background gap={22} color="#e3ebf7" /><Controls position="bottom-left" showInteractive={false} /></ReactFlow></div>
      {node && <aside className="tower-drawer">
        <div className="drawer-head"><div><span className="agent-id">{node.id}</span><h3>{node.name}</h3><Tag tone={(STATUS[node.status] || STATUS.todo)[1]}>{(STATUS[node.status] || STATUS.todo)[0]}</Tag> <Tag>{(KIND[node.kind] || KIND.service)[0]}</Tag></div><button className="icon-button" onClick={() => setSel(null)} aria-label="Close"><X size={18} /></button></div>
        <p className="drawer-does">{node.does}</p>
        <div className="io-block"><h4><FileInput size={14} /> Inputs</h4><ul>{node.inputs.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
        <div className="io-arrow"><ArrowRight size={16} /></div>
        <div className="io-block out"><h4><FileOutput size={14} /> Outputs</h4><ul>{node.outputs.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
        {node.writes?.length > 0 && <div className="io-block files"><h4>Files written</h4><ul>{node.writes.map((w, i) => { const f = w.split(' ')[0] === 'library:' ? w : w.split(' ')[0]; const ok = (node.present || []).some(p => w.startsWith(p) || p === w); return <li key={i}><code>{w}</code> {live && (ok ? <Tag tone="positive">present</Tag> : <Tag>not yet</Tag>)}</li> })}</ul></div>}
        {node.task && <SummaryNote>AI task <code>{node.task}</code>: the request is built as a JSON package, shown with its SHA-256 and sent only after approval. Without approval or AI, rules are used.</SummaryNote>}
        <div className="io-block"><h4>Code</h4><code className="module">{node.module}</code></div>
        {node.next?.length > 0 && <div className="io-block"><h4>Feeds</h4><div className="next-chips">{node.next.map(n => <button key={n} className="chip" onClick={() => setSel(n)}>{n} · {d.nodes.find(x => x.id === n)?.name}</button>)}</div></div>}
      </aside>}
    </div>}
    {d && <Panel title="Every component: inputs → outputs" subtitle="The same information as the map, as a table you can read top to bottom">
      <DataTable rows={d.nodes} max={60} columns={[{ key: 'id', label: 'ID' }, { key: 'name', label: 'Component' }, { key: 'kind', label: 'Kind', render: v => (KIND[v] || KIND.service)[0] }, { key: 'lane', label: 'Phase', render: v => d.lanes.find(l => l.id === v)?.label }, { key: 'inputs', label: 'Inputs', render: v => v.join(' · ') }, { key: 'outputs', label: 'Outputs', render: v => v.join(' · ') }, { key: 'writes', label: 'Files', render: v => (v || []).join(', ') }, ...(live ? [{ key: 'status', label: 'Status', render: v => <Tag tone={(STATUS[v] || STATUS.todo)[1]}>{(STATUS[v] || STATUS.todo)[0]}</Tag> }] : [])]} />
      <SummaryNote>Click a box on the map for its full description. AI agents never block the workflow: with AI off, not approved or failing, the same step runs on rules and says so.</SummaryNote>
    </Panel>}
  </>
}
