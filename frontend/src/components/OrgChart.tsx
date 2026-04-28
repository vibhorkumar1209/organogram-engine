import React, { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'
import type { OrgNode } from '../types'
import { NodeTooltip } from './NodeTooltip'

interface Props {
  tree: OrgNode | null
  highlightId: string | null
  onNodeClick: (node: OrgNode) => void
  /** After expanding this node ID, auto-pan to show its new children */
  focusNodeId?: string | null
  /**
   * Increment this number to trigger a fit-to-screen on the next render.
   * App.tsx bumps it whenever the tree is structurally reset.
   */
  fitGeneration?: number
}

// ── Layout constants (horizontal tree: left → right) ──────────────────
const NODE_W      = 188
const NODE_H      = 54
const DEPTH_GAP   = 52
const SIBLING_GAP = 16

// ── Colours ───────────────────────────────────────────────────────────
const SECTOR_COLORS: Record<string, string> = {
  Automotive: '#F59E0B',
  Govt:       '#3B82F6',
  NGO:        '#10B981',
  Startup:    '#8B5CF6',
  Public:     '#06B6D4',
  Private:    '#64748B',
  All:        '#3491E8',
}

const NODE_TYPE_ICON: Record<string, string> = {
  global:         '⬡',
  region:         '◈',
  dept_primary:   '▣',
  dept_secondary: '▤',
  dept_tertiary:  '▥',
  person:         '●',
  ghost:          '◌',
}

const NODE_TYPE_LABEL: Record<string, string> = {
  global:         'Organisation',
  region:         'Region',
  dept_primary:   'Department',
  dept_secondary: 'Sub-Department',
  dept_tertiary:  'Team',
  ghost:          'Level placeholder ✦',
}

function buildD3Hierarchy(node: OrgNode): d3.HierarchyNode<OrgNode> {
  return d3.hierarchy(node, d => {
    const valid = (d.children ?? []).filter(c => c.node_id)
    return valid.length > 0 ? valid : null
  })
}

function accentColor(d: d3.HierarchyNode<OrgNode>): string {
  return SECTOR_COLORS[d.data.sector] ?? d.data.color ?? '#64748B'
}

export const OrgChart: React.FC<Props> = ({ tree, highlightId, onNodeClick, focusNodeId, fitGeneration }) => {
  const svgRef             = useRef<SVGSVGElement>(null)
  const containerRef       = useRef<HTMLDivElement>(null)
  const zoomRef            = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const fitTransformRef    = useRef<d3.ZoomTransform>(d3.zoomIdentity)
  // Track which fitGeneration we last fit to; if it changes (or dims changes after fit request)
  const lastFitGen         = useRef<number>(-1)
  const pendingFitGen      = useRef<number>(fitGeneration ?? 0)

  const [tooltip, setTooltip] = useState<{ node: OrgNode; x: number; y: number } | null>(null)
  const [dims, setDims]       = useState({ w: 0, h: 0 })

  // Observe container resize
  useEffect(() => {
    if (!containerRef.current) return
    const ro = new ResizeObserver(entries => {
      for (const e of entries)
        setDims({ w: e.contentRect.width, h: e.contentRect.height })
    })
    ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [])

  // When fitGeneration bumps, record it as pending so next render fits
  useEffect(() => {
    if (fitGeneration !== undefined) pendingFitGen.current = fitGeneration
  }, [fitGeneration])

  // ── Render D3 tree ──────────────────────────────────────────────────
  useEffect(() => {
    if (!tree || !svgRef.current) return
    if (!dims.w || !dims.h) return   // wait for ResizeObserver to give real container dims

    // ── Decide whether to fit or restore ──────────────────────────────
    // Fit if: never fit before, OR a new fitGeneration is pending
    const doFit = lastFitGen.current < pendingFitGen.current
    const savedTransform = doFit ? null : d3.zoomTransform(svgRef.current)

    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()

    const root = buildD3Hierarchy(tree)

    // ── Layout ─────────────────────────────────────────────────────────
    const treeLayout = d3.tree<OrgNode>()
      .nodeSize([NODE_H + SIBLING_GAP, NODE_W + DEPTH_GAP])
      .separation((a, b) => a.parent === b.parent ? 1.1 : 1.5)

    treeLayout(root)

    // ── Bounds ─────────────────────────────────────────────────────────
    let minSX = Infinity, maxSX = -Infinity
    let minSY = Infinity, maxSY = -Infinity
    root.each(d => {
      const sx = (d as any).y
      const sy = (d as any).x
      if (sx < minSX) minSX = sx
      if (sx > maxSX) maxSX = sx
      if (sy < minSY) minSY = sy
      if (sy > maxSY) maxSY = sy
    })

    const treeW = maxSX - minSX + NODE_W + 100
    const treeH = maxSY - minSY + NODE_H + 100
    const offX  = -minSX + 50
    const offY  = -minSY + 50

    // ── Zoom ───────────────────────────────────────────────────────────
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.04, 3])
      .on('zoom', e => g.attr('transform', e.transform.toString()))
    zoomRef.current = zoom
    svg.call(zoom)

    const g = svg.append('g')

    // ── Apply transform: fit when requested, restore otherwise ─────────
    if (doFit) {
      const scale        = Math.min(dims.w / treeW, dims.h / treeH, 1.15) * 0.88
      const tx           = (dims.w  - treeW * scale) / 2
      const ty           = (dims.h  - treeH * scale) / 2
      const fitTransform = d3.zoomIdentity.translate(tx, ty).scale(scale)
      fitTransformRef.current = fitTransform
      svg.call(zoom.transform, fitTransform)
      lastFitGen.current = pendingFitGen.current
    } else if (savedTransform) {
      // Restore user's current viewport (do NOT overwrite fitTransformRef so ⊙ resets to last fit)
      svg.call(zoom.transform, savedTransform)

      // ── Auto-pan to show newly expanded node + its children ──────────
      if (focusNodeId) {
        const focusD3 = root.descendants().find(d => d.data.node_id === focusNodeId)
        if (focusD3 && (focusD3.children ?? []).length > 0) {
          const children = focusD3.children!
          const childrenMinY = Math.min(...children.map(c => (c as any).x + offY))
          const childrenMaxY = Math.max(...children.map(c => (c as any).x + offY))
          const childrenX    = Math.max(...children.map(c => (c as any).y + offX)) + NODE_W / 2

          const s  = savedTransform.k
          const cx = (focusD3 as any).y + offX   // node screen X in tree coords
          const cy = (childrenMinY + childrenMaxY) / 2   // midpoint of children vertical span

          // Target: put children into the right half of the visible area
          const targetScreenX = dims.w * 0.55
          const targetScreenY = dims.h / 2

          const newTx = targetScreenX - cx * s - (childrenX - cx) * s * 0.4
          const newTy = targetScreenY - cy * s

          // Clamp so we don't over-pan
          const newTransform = d3.zoomIdentity
            .translate(newTx, newTy)
            .scale(s)

          svg.transition().duration(420).ease(d3.easeCubicOut)
            .call(zoom.transform, newTransform)
        }
      }
    }

    // ── Links ──────────────────────────────────────────────────────────
    g.selectAll('.org-link')
      .data(root.links())
      .join('path')
      .attr('class', 'org-link')
      .attr('d', d3.linkHorizontal<any, any>()
        .x((d: any) => d.y + offX)
        .y((d: any) => d.x + offY)
      )

    // ── Node groups ────────────────────────────────────────────────────
    const nodeGroups = g.selectAll('.org-node')
      .data(root.descendants())
      .join('g')
      .attr('class', d => {
        let cls = 'org-node'
        if (d.data.node_type === 'person')  cls += ' person-node'
        if (d.data.node_id === highlightId) cls += ' node-highlight'
        return cls
      })
      .attr('transform', d => `translate(${(d as any).y + offX}, ${(d as any).x + offY})`)
      .style('cursor', 'pointer')
      .on('mouseenter', (e, d) => setTooltip({ node: d.data, x: e.clientX, y: e.clientY }))
      .on('mousemove',  (e)    => setTooltip(t => t ? { ...t, x: e.clientX, y: e.clientY } : t))
      .on('mouseleave', ()     => setTooltip(null))
      .on('click', (e, d) => {
        e.stopPropagation()
        if (!d.data.is_synthetic) onNodeClick(d.data)
      })

    // ── Card background ────────────────────────────────────────────────
    nodeGroups.append('rect')
      .attr('x', -NODE_W / 2).attr('y', -NODE_H / 2)
      .attr('width', NODE_W).attr('height', NODE_H)
      .attr('rx', 8).attr('ry', 8)
      .attr('fill', d => {
        if (d.data.node_id === highlightId) return '#0f2a3f'
        if (d.data.node_type === 'person')  return '#091624'
        return '#0a1520'
      })
      .attr('stroke', d => accentColor(d))
      .attr('stroke-width', d => {
        if (d.data.node_id === highlightId) return 2.5
        if (d.data.node_type === 'person')  return 1
        return 1.5
      })
      .attr('stroke-opacity', d => d.data.node_type === 'person' ? 0.5 : 1)

    // ── Left accent bar ────────────────────────────────────────────────
    nodeGroups.append('rect')
      .attr('x', -NODE_W / 2).attr('y', -NODE_H / 2)
      .attr('width', 4).attr('height', NODE_H)
      .attr('rx', 2)
      .attr('fill', d => accentColor(d))
      .attr('fill-opacity', d => d.data.node_type === 'person' ? 0.45 : 0.85)

    // ── Icon ──────────────────────────────────────────────────────────
    nodeGroups.append('text')
      .attr('x', -NODE_W / 2 + 18).attr('y', 4)
      .attr('font-size', 13).attr('text-anchor', 'middle')
      .attr('fill', d => accentColor(d))
      .attr('fill-opacity', d => d.data.node_type === 'person' ? 0.6 : 1)
      .text(d => NODE_TYPE_ICON[d.data.node_type] ?? '●')

    // ── Primary label ─────────────────────────────────────────────────
    nodeGroups.append('text')
      .attr('x', -NODE_W / 2 + 32).attr('y', -6)
      .attr('font-size', d => d.data.node_type === 'person' ? 11.5 : 11)
      .attr('font-weight', 700)
      .attr('fill', d => d.data.node_type === 'person' ? '#cbd5e1' : '#e2e8f0')
      .attr('text-anchor', 'start')
      .text(d => {
        const lbl = d.data.label
        return lbl.length > 18 ? lbl.slice(0, 17) + '…' : lbl
      })

    // ── Secondary label ───────────────────────────────────────────────
    nodeGroups.append('text')
      .attr('x', -NODE_W / 2 + 32).attr('y', 9)
      .attr('font-size', 9.5)
      .attr('fill', d => d.data.node_type === 'person' ? '#4a6580' : '#374e65')
      .attr('text-anchor', 'start')
      .text(d => {
        if (d.data.node_type === 'person' && d.data.metadata?.designation)
          return String(d.data.metadata.designation).slice(0, 23)
        return NODE_TYPE_LABEL[d.data.node_type] ?? d.data.sector ?? ''
      })

    // ── Layer badge — dept/region/global only ──────────────────────────
    nodeGroups.filter(d => d.data.layer >= 0 && d.data.node_type !== 'person')
      .append('rect')
      .attr('x', NODE_W / 2 - 28).attr('y', -NODE_H / 2 + 5)
      .attr('width', 24).attr('height', 15).attr('rx', 3)
      .attr('fill', d => accentColor(d) + '1a')
      .attr('stroke', d => accentColor(d) + '40')
      .attr('stroke-width', 0.5)

    nodeGroups.filter(d => d.data.layer >= 0 && d.data.node_type !== 'person')
      .append('text')
      .attr('x', NODE_W / 2 - 16).attr('y', -NODE_H / 2 + 15)
      .attr('font-size', 9).attr('font-weight', 700).attr('text-anchor', 'middle')
      .attr('fill', d => accentColor(d))
      .text(d => `L${d.data.layer}`)

    // ── Seniority tier for person cards ───────────────────────────────
    const SENIORITY_SHORT: Record<number, string> = {
      0: 'Board', 1: 'C-Suite', 2: 'EVP', 3: 'VP',
      4: 'Sr.Dir', 5: 'Dir', 6: 'Sr.Mgr', 7: 'Mgr',
      8: 'Sr.IC', 9: 'IC', 10: 'Grad',
    }
    nodeGroups.filter(d => d.data.node_type === 'person' && d.data.layer >= 0)
      .append('text')
      .attr('x', -NODE_W / 2 + 32).attr('y', NODE_H / 2 - 6)
      .attr('font-size', 8.5)
      .attr('fill', d => accentColor(d) + '70')
      .text(d => {
        const tier = SENIORITY_SHORT[d.data.layer]
        return tier ? `L${d.data.layer} · ${tier}` : `L${d.data.layer}`
      })

    // ── Expand/collapse chevron ────────────────────────────────────────
    const needsExpand   = nodeGroups.filter(d => !!(d.data.has_more && !d.data.expanded))
    const needsCollapse = nodeGroups.filter(d => !!(
      d.data.expanded &&
      !d.data.is_synthetic &&
      (d.data.children ?? []).filter(c => c.node_id).length > 0
    ))

    ;[needsExpand, needsCollapse].forEach((sel, i) => {
      sel.append('circle')
        .attr('cx', NODE_W / 2 + 13).attr('cy', 0).attr('r', 9)
        .attr('fill', '#060d14')
        .attr('stroke', d => accentColor(d) + (i === 0 ? '55' : '33'))
        .attr('stroke-width', 1.2)
      sel.append('text')
        .attr('x', NODE_W / 2 + 13).attr('y', 5)
        .attr('font-size', 13).attr('text-anchor', 'middle')
        .attr('fill', d => accentColor(d) + (i === 0 ? 'cc' : '77'))
        .text(i === 0 ? '›' : '‹')
    })

    // ── People count badge on dept nodes ──────────────────────────────
    nodeGroups.filter(d =>
      ['dept_primary', 'dept_secondary', 'dept_tertiary'].includes(d.data.node_type) &&
      d.data.metadata?.people_count != null
    ).append('text')
      .attr('x', -NODE_W / 2 + 32).attr('y', NODE_H / 2 - 6)
      .attr('font-size', 9)
      .attr('fill', d => accentColor(d) + '88')
      .text(d => `${d.data.metadata.people_count} people`)

  }, [tree, highlightId, dims, onNodeClick, focusNodeId])

  // ── Zoom controls ──────────────────────────────────────────────────
  const handleZoom = (factor: number) => {
    if (!svgRef.current || !zoomRef.current) return
    d3.select(svgRef.current).transition().duration(300)
      .call(zoomRef.current.scaleBy, factor)
  }

  const handleReset = () => {
    if (!svgRef.current || !zoomRef.current) return
    d3.select(svgRef.current).transition().duration(400)
      .call(zoomRef.current.transform, fitTransformRef.current)
  }

  return (
    <div ref={containerRef} style={{ width: '100%', height: '100%', position: 'relative' }}>
      <svg
        ref={svgRef}
        width="100%"
        height="100%"
        style={{ display: 'block', background: 'transparent' }}
        onClick={() => setTooltip(null)}
      />

      <div style={{
        position: 'absolute', bottom: 20, right: 20,
        display: 'flex', flexDirection: 'column', gap: 6,
      }}>
        <button className="zoom-btn" onClick={() => handleZoom(1.3)} title="Zoom In">+</button>
        <button className="zoom-btn" onClick={() => handleZoom(0.75)} title="Zoom Out">−</button>
        <button className="zoom-btn" onClick={handleReset} title="Fit to screen" style={{ fontSize: 13 }}>⊙</button>
      </div>

      <NodeTooltip
        node={tooltip?.node ?? null}
        x={tooltip?.x ?? 0}
        y={tooltip?.y ?? 0}
      />
    </div>
  )
}
