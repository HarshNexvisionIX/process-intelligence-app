import React, { useMemo, useRef } from 'react'
import { ReactFlow, Background, Controls, MiniMap, Handle, Position, MarkerType } from '@xyflow/react'
import dagre from 'dagre'

const numeric = n => Number(n || 0).toLocaleString()
const NODE_W = 300

/** Keep the biggest items until `coverage` % of the total is reached (visual filter only; totals never change). */
export function coverageSubset(rows, key, coverage = 100) {
  const ranked = [...(rows || [])].sort((a, b) => Number(b[key] || 0) - Number(a[key] || 0))
  if (coverage >= 100) return ranked
  const total = ranked.reduce((s, r) => s + Number(r[key] || 0), 0)
  let sum = 0
  const out = []
  for (const r of ranked) { out.push(r); sum += Number(r[key] || 0); if (total > 0 && (sum / total) * 100 >= coverage) break }
  return out
}

function StepNode({ data }) {
  const v = data.vertical
  const apps = data.apps || []
  const shown = apps.slice(0, data.appLines)
  return <div className={'business-stage' + (data.missing ? ' missing' : '') + (data.selected ? ' stage-selected' : '')} title={data.label}>
    <Handle type="target" position={v ? Position.Top : Position.Left} />
    <div className="business-stage-main">
      <div className="business-stage-count">{data.documented ? data.index : data.missing ? '—' : data.hours == null ? numeric(data.events) : `${Number(data.hours || 0).toFixed(1)}h`}</div>
      <div className="business-stage-info">
        <div className="business-stage-title">{data.label}</div>
        <div className="business-stage-subtitle">{data.documented ? (data.description || data.group || 'Documented step') : data.missing ? 'In the process, not seen in the data'
          : data.hours == null ? `events · ${numeric(data.cases)} cases` : `${data.share != null ? Math.round(100 * data.share) + '% of hours · ' : ''}${numeric(data.events)} events${data.actors ? ` · ${data.actors} people` : ''}`}</div>
      </div>
    </div>
    {shown.length > 0 && <div className="business-stage-apps">
      {shown.map((a, i) => <div className="stage-app-row" key={i} title={`${a.app}: ${Number(a.hours || 0).toFixed(2)} h`}>
        <span>{a.app}</span><b>{Number(a.hours || 0).toFixed(1)}h</b></div>)}
      {apps.length > shown.length && <div className="stage-app-more">+ {apps.length - shown.length} more · select for all</div>}
    </div>}
    {data.clickable && !data.missing && <div className="business-stage-hint">Select for applications, Level 4 and screens</div>}
    <Handle type="source" position={v ? Position.Bottom : Position.Right} />
  </div>
}
function GroupNode({ data }) {
  return <div className="stage-group" style={{ width: data.width, height: data.height }}><span>{data.label}</span></div>
}
const nodeTypes = { step: StepNode, stagebox: GroupNode }

export default function FlowMap({ nodes = [], edges = [], direction = 'TB', appLines = 4, nodeCoverage = 100, pathCoverage = 100,
  showOrder = true, groups = true, onSelect = null, selected = null, height = 760 }) {
  const graph = useMemo(() => {
    const vertical = direction === 'TB'
    const real = nodes.filter(n => !n.missing)
    const sizeKey = real.some(n => n.hours != null) ? 'hours' : 'events'
    const keepIds = new Set(coverageSubset(real, sizeKey, nodeCoverage).map(n => n.id))
    nodes.filter(n => n.missing).forEach(n => keepIds.add(n.id))
    const visNodes = nodes.filter(n => keepIds.has(n.id))
    const observed = edges.filter(e => e.kind !== 'order' && keepIds.has(e.source) && keepIds.has(e.target))
    const keptObserved = coverageSubset(observed, 'count', pathCoverage)
    const order = showOrder ? edges.filter(e => e.kind === 'order' && keepIds.has(e.source) && keepIds.has(e.target)) : []
    const useGroups = groups && visNodes.some(n => n.group)
    const g = new dagre.graphlib.Graph({ multigraph: true, compound: useGroups })
    g.setGraph({ rankdir: direction, ranksep: vertical ? 70 : 110, nodesep: vertical ? 45 : 40, marginx: 30, marginy: 30 })
    g.setDefaultEdgeLabel(() => ({}))
    const h = n => (n.missing ? 70 : 84 + Math.min(appLines, (n.apps || []).length) * 22 + ((n.apps || []).length > appLines ? 16 : 0) + (onSelect ? 16 : 0))
    const groupIds = new Map()
    for (const n of visNodes) {
      g.setNode(n.id, { width: NODE_W, height: h(n) })
      if (useGroups && n.group) {
        const gid = 'group::' + n.group
        if (!groupIds.has(gid)) { groupIds.set(gid, n.group); g.setNode(gid, { label: n.group, paddingTop: 34 }) }
        g.setParent(n.id, gid)
      }
    }
    const flowEdges = []
    const add = (e, i, kind) => {
      const count = Number(e.count || 0)
      const isOrder = kind === 'order'
      const color = isOrder ? '#94a3b8' : '#155eef'
      g.setEdge(e.source, e.target, {}, kind + i)
      flowEdges.push({
        id: kind + i, source: e.source, target: e.target, type: isOrder ? 'smoothstep' : 'default',
        label: isOrder ? '' : numeric(count), animated: false,
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 15, height: 15 },
        style: { stroke: color, strokeWidth: isOrder ? 1.3 : 1.4 + Math.min(4, Math.log10(1 + count)), strokeDasharray: isOrder ? '4 4' : undefined },
        labelStyle: { fill: '#344B69', fontSize: 11, fontWeight: 750 }, labelBgStyle: { fill: '#fff', fillOpacity: 0.94 }, labelBgPadding: [5, 7], labelBgBorderRadius: 6,
      })
    }
    order.forEach((e, i) => add(e, i, 'order'))
    keptObserved.forEach((e, i) => add(e, i, 'obs'))
    dagre.layout(g)
    const flowNodes = []
    const stepNodes = visNodes.map(n => { const p = g.node(n.id); return { id: n.id, type: 'step', position: { x: p.x - NODE_W / 2, y: p.y - h(n) / 2 }, data: { ...n, vertical, appLines, selected: selected === n.id, clickable: Boolean(onSelect) } } })
    for (const [gid, label] of groupIds) {                       // stage box = bounding box of its steps
      const kids = stepNodes.filter(s => s.data.group === label)
      if (!kids.length) continue
      const x0 = Math.min(...kids.map(k => k.position.x)) - 16, y0 = Math.min(...kids.map(k => k.position.y)) - 36
      const x1 = Math.max(...kids.map(k => k.position.x + NODE_W)) + 16, y1 = Math.max(...kids.map(k => k.position.y + h(k.data))) + 16
      flowNodes.push({ id: gid, type: 'stagebox', position: { x: x0, y: y0 }, data: { label, width: x1 - x0, height: y1 - y0 }, selectable: false, draggable: false, zIndex: -1, style: { zIndex: -1 } })
    }
    flowNodes.push(...stepNodes)
    return { flowNodes, flowEdges, shown: visNodes.filter(n => !n.missing).length, total: real.length, paths: keptObserved.length, allPaths: observed.length }
  }, [nodes, edges, direction, appLines, nodeCoverage, pathCoverage, showOrder, groups, selected, onSelect])
  const box = useRef(null)
  function init(inst) {                       // readable zoom, start of the process at the top / left
    const ns = graph.flowNodes
    if (!ns.length || !box.current) return
    const minx = Math.min(...ns.map(n => n.position.x)), miny = Math.min(...ns.map(n => n.position.y))
    const maxx = Math.max(...ns.map(n => n.position.x + (n.data.width || NODE_W))), maxy = Math.max(...ns.map(n => n.position.y + (n.data.height || 140)))
    const W = box.current.clientWidth, H = box.current.clientHeight, bw = maxx - minx + 60, bh = maxy - miny + 60
    const z = Math.min(1, Math.max(0.62, Math.min(W / bw, H / bh)))
    inst.setViewport({ x: bw * z < W ? (W - bw * z) / 2 - minx * z + 30 * z : 20 - minx * z, y: bh * z < H ? (H - bh * z) / 2 - miny * z + 30 * z : 46 - miny * z, zoom: z })
  }
  if (!nodes.length) return <div className="empty-graph">Nothing to draw yet.</div>
  return <div className="flow-container business-flow" style={{ height }} ref={box}>
    <ReactFlow nodes={graph.flowNodes} edges={graph.flowEdges} nodeTypes={nodeTypes} onInit={init}
      minZoom={0.1} maxZoom={1.8} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
      onNodeClick={(_, node) => { if (onSelect && node.type === 'step' && !node.data.missing) onSelect(node.id) }}>
      <Background gap={24} color="#DFE8F6" />
      <Controls position="bottom-left" showInteractive={false} />
      <MiniMap pannable zoomable nodeColor={n => (n.type === 'stagebox' ? '#eef4ff' : '#cfe0ff')} maskColor="rgba(239,245,255,.55)" />
    </ReactFlow>
    <div className="business-flow-summary">{graph.shown} of {graph.total} items{graph.allPaths ? ` · ${graph.paths} of ${graph.allPaths} observed transitions` : ''}{showOrder ? ' · dashed grey = business order' : ''}</div>
  </div>
}
