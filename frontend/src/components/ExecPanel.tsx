import React, { useState } from 'react'
import type { OrgNode } from '../types'

interface Props {
  deptNode:   OrgNode | null
  executives: OrgNode[] | null   // null = loading; flat sorted-by-layer list
  onClose:    () => void
}

const LAYER_LABELS: Record<number, string> = {
  0: 'Board / Apex',
  1: 'C-Suite',
  2: 'EVP / MD',
  3: 'VP / Director',
  4: 'Senior Director',
  5: 'Director / Head',
  6: 'Senior Manager',
  7: 'Manager',
  8: 'Senior IC',
  9: 'IC / Analyst',
  10: 'Graduate / Intern',
}

const SECTOR_COLORS: Record<string, string> = {
  Automotive: '#F59E0B',
  Govt:       '#3B82F6',
  NGO:        '#10B981',
  Startup:    '#8B5CF6',
  Public:     '#06B6D4',
  Private:    '#64748B',
  All:        '#3491E8',
}

function nodeColor(node: OrgNode, fallback: string): string {
  return SECTOR_COLORS[node.sector] ?? node.color ?? fallback
}

// ── Hierarchy builder ─────────────────────────────────────────────────
// Given a flat sorted-by-layer list, build a manager→reportee tree.
// People at the same layer are siblings (peers).
// People at layer N+1 become direct reports of ALL people at layer N above them,
// attributed to the entire preceding peer group as a virtual "manager group".
//
// We use a stack-based approach: push each layer group; when a deeper layer appears,
// it hangs under the most-senior still-open group.

interface ExecTreeNode {
  person:    OrgNode
  reports:   ExecTreeNode[]
  isLast:    boolean   // used for connector rendering
}

function buildExecTree(people: OrgNode[]): ExecTreeNode[] {
  if (!people.length) return []

  // Sort by layer asc
  const sorted = [...people].sort((a, b) => {
    const la = a.layer ?? 99
    const lb = b.layer ?? 99
    return la !== lb ? la - lb : a.label.localeCompare(b.label)
  })

  // Group consecutive same-layer people
  interface LayerGroup { layer: number; nodes: ExecTreeNode[] }
  const groups: LayerGroup[] = []
  for (const p of sorted) {
    const l = p.layer ?? 99
    const last = groups[groups.length - 1]
    const tNode: ExecTreeNode = { person: p, reports: [], isLast: false }
    if (last && last.layer === l) {
      last.nodes.push(tNode)
    } else {
      groups.push({ layer: l, nodes: [tNode] })
    }
  }

  // Build tree: each group's nodes become children of ALL nodes in the group above
  // (in practice we assign them to the first node of the parent group as primary manager)
  // We distribute reportees across parent nodes round-robin for a balanced tree,
  // but since we don't have actual reporting data, we put all under the group's first node.
  // The visual is indentation by layer, not strict manager→reportee.

  // Mark isLast within each parent's reports list after assignment
  const assignReports = (parentGroup: LayerGroup, childGroup: LayerGroup) => {
    // If only 1 parent, all children go under it
    if (parentGroup.nodes.length === 1) {
      parentGroup.nodes[0].reports = childGroup.nodes
    } else {
      // Distribute roughly evenly
      const perParent = Math.ceil(childGroup.nodes.length / parentGroup.nodes.length)
      let ci = 0
      for (const parent of parentGroup.nodes) {
        parent.reports = childGroup.nodes.slice(ci, ci + perParent)
        ci += perParent
      }
    }
    // Mark isLast
    for (const parent of parentGroup.nodes) {
      parent.reports.forEach((r, i) => { r.isLast = i === parent.reports.length - 1 })
    }
  }

  for (let i = 1; i < groups.length; i++) {
    assignReports(groups[i - 1], groups[i])
  }

  // Root = first group's nodes
  const roots = groups[0]?.nodes ?? []
  roots.forEach((r, i) => { r.isLast = i === roots.length - 1 })
  return roots
}

// ── Person row ────────────────────────────────────────────────────────
const PersonRow: React.FC<{
  node:       ExecTreeNode
  depth:      number
  isLast:     boolean
  parentLines: boolean[]   // which ancestor levels still have more siblings
  color:      string
  onToggle:   (id: string) => void
  collapsed:  Set<string>
}> = ({ node, depth, isLast, parentLines, color, onToggle, collapsed }) => {
  const p      = node.person
  const pColor = nodeColor(p, color)
  const hasReports = node.reports.length > 0
  const isCollapsed = collapsed.has(p.node_id)

  const indent = depth * 20

  return (
    <>
      {/* ── This person's row ────────────────────────────────── */}
      <div
        style={{
          display: 'flex', alignItems: 'center',
          paddingLeft: 12, paddingRight: 14,
          paddingTop: 8, paddingBottom: 8,
          borderBottom: '1px solid #080e16',
          position: 'relative',
          cursor: hasReports ? 'pointer' : 'default',
        }}
        onClick={() => hasReports && onToggle(p.node_id)}
      >
        {/* ── Tree connector lines ───────────────────────────── */}
        {depth > 0 && (
          <div style={{
            position: 'absolute', left: 12 + (depth - 1) * 20 + 8,
            top: 0, bottom: isLast ? '50%' : 0,
            width: 1, background: color + '25',
            pointerEvents: 'none',
          }} />
        )}
        {/* Vertical continuation lines for ancestor levels */}
        {parentLines.map((show, li) => show && (
          <div key={li} style={{
            position: 'absolute',
            left: 12 + li * 20 + 8,
            top: 0, bottom: 0,
            width: 1, background: color + '20',
            pointerEvents: 'none',
          }} />
        ))}
        {/* Horizontal elbow */}
        {depth > 0 && (
          <div style={{
            position: 'absolute',
            left: 12 + (depth - 1) * 20 + 8,
            top: '50%', width: 12, height: 1,
            background: color + '30',
            pointerEvents: 'none',
          }} />
        )}

        {/* Spacer for indent */}
        <div style={{ width: indent, flexShrink: 0 }} />

        {/* Expand/collapse arrow */}
        <div style={{
          width: 14, flexShrink: 0, fontSize: 9, color: color + (hasReports ? 'aa' : '30'),
          textAlign: 'center', marginRight: 4, userSelect: 'none',
        }}>
          {hasReports ? (isCollapsed ? '▶' : '▼') : '·'}
        </div>

        {/* Avatar */}
        <div style={{
          width: 30, height: 30, borderRadius: '50%',
          background: pColor + '15', border: `1px solid ${pColor}30`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 12, fontWeight: 700, color: pColor,
          flexShrink: 0, marginRight: 10,
        }}>
          {p.label.charAt(0).toUpperCase()}
        </div>

        {/* Text */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            fontSize: 11.5, fontWeight: depth === 0 ? 700 : 600,
            color: depth === 0 ? '#e2e8f0' : '#b0bec5',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {p.label}
          </div>
          {p.metadata?.designation && (
            <div style={{
              fontSize: 9.5, color: '#3a5a78', marginTop: 1,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>
              {String(p.metadata.designation).slice(0, 36)}
            </div>
          )}
          {/* Footer: location + linkedin + pay + source */}
          <div style={{ display: 'flex', gap: 7, marginTop: 2, flexWrap: 'wrap', alignItems: 'center' }}>
            {p.metadata?.location && (
              <span style={{ fontSize: 8.5, color: '#263d52' }}>
                📍 {String(p.metadata.location).split(',')[0]}
              </span>
            )}
            {p.metadata?.linkedin_url && (
              <a href={String(p.metadata.linkedin_url)} target="_blank" rel="noreferrer"
                style={{ fontSize: 8.5, color: '#3491E8', textDecoration: 'none' }}
                onClick={e => e.stopPropagation()}>
                in ↗
              </a>
            )}
            {p.metadata?.pay && (
              <span style={{ fontSize: 8, color: '#2d4a63' }}>
                💰{(Number(p.metadata.pay) / 1_000_000).toFixed(1)}M
              </span>
            )}
          </div>
        </div>

        {/* Seniority badge */}
        <div style={{
          flexShrink: 0, marginLeft: 6,
          fontSize: 8, color: pColor + '80',
          background: pColor + '12', borderRadius: 3, padding: '2px 5px',
          border: `1px solid ${pColor}20`, whiteSpace: 'nowrap',
        }}>
          L{p.layer ?? '?'}
        </div>

        {/* Reportee count */}
        {hasReports && (
          <div style={{
            flexShrink: 0, marginLeft: 4,
            fontSize: 8, color: '#334155',
          }}>
            {node.reports.length}↓
          </div>
        )}
      </div>

      {/* ── Reportees (recursive) ─────────────────────────────── */}
      {hasReports && !isCollapsed && node.reports.map((child, ci) => (
        <PersonRow
          key={child.person.node_id}
          node={child}
          depth={depth + 1}
          isLast={ci === node.reports.length - 1}
          parentLines={[...parentLines, !isLast]}
          color={color}
          onToggle={onToggle}
          collapsed={collapsed}
        />
      ))}
    </>
  )
}

// ── Main panel ────────────────────────────────────────────────────────
export const ExecPanel: React.FC<Props> = ({ deptNode, executives, onClose }) => {
  const isOpen = deptNode !== null
  const color  = deptNode ? (SECTOR_COLORS[deptNode.sector] ?? deptNode.color ?? '#3491E8') : '#3491E8'

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const toggleCollapse = (id: string) =>
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })

  const tree = executives ? buildExecTree(executives) : []

  // Layer distribution summary
  const byLayer: Record<number, number> = {}
  if (executives) {
    for (const p of executives) {
      const l = p.layer ?? 9
      byLayer[l] = (byLayer[l] ?? 0) + 1
    }
  }
  const layerSummary = Object.entries(byLayer)
    .sort(([a], [b]) => Number(a) - Number(b))
    .map(([l, n]) => `${n} ${LAYER_LABELS[Number(l)] ?? `L${l}`}`)
    .join(' · ')

  return (
    <div style={{
      position: 'absolute', top: 0, right: 0, bottom: 0,
      width: 360, zIndex: 200,
      background: '#07111a',
      borderLeft: `1px solid ${color}33`,
      display: 'flex', flexDirection: 'column',
      boxShadow: '-20px 0 60px rgba(0,0,0,0.8)',
      transform: isOpen ? 'translateX(0)' : 'translateX(100%)',
      transition: 'transform 0.22s ease-out',
      pointerEvents: isOpen ? 'auto' : 'none',
    }}>

      {/* ── Header ──────────────────────────────────────────────── */}
      <div style={{
        padding: '14px 16px 12px',
        borderBottom: `1px solid ${color}18`,
        background: '#080f16',
      }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{
              fontSize: 9, color: '#334155', letterSpacing: 1.2,
              textTransform: 'uppercase', marginBottom: 4,
            }}>
              Executives
            </div>
            <div style={{
              fontSize: 14, fontWeight: 700, color,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              lineHeight: 1.2,
            }}>
              {deptNode?.label ?? ''}
            </div>
            {executives !== null && executives.length > 0 && (
              <div style={{ fontSize: 9, color: '#334155', marginTop: 5, lineHeight: 1.5 }}>
                {layerSummary}
              </div>
            )}
          </div>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: '#475569',
            cursor: 'pointer', fontSize: 18, padding: 0, lineHeight: 1, flexShrink: 0,
          }} title="Close">×</button>
        </div>
      </div>

      {/* ── Legend ─────────────────────────────────────────────── */}
      {executives !== null && executives.length > 0 && (
        <div style={{
          padding: '6px 14px', background: '#060d14',
          borderBottom: `1px solid #0c1e2e`,
          display: 'flex', alignItems: 'center', gap: 12, fontSize: 9, color: '#2d4a63',
        }}>
          <span>▼ collapse</span>
          <span>▶ expand</span>
          <span style={{ marginLeft: 'auto' }}>
            {tree.length} top-level · {executives.length} total
          </span>
        </div>
      )}

      {/* ── Body ────────────────────────────────────────────────── */}
      <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden' }}>

        {/* Loading */}
        {executives === null && (
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            height: 120, color: '#334155', fontSize: 12,
          }}>
            ⟳ Loading…
          </div>
        )}

        {/* Empty */}
        {executives !== null && executives.length === 0 && (
          <div style={{
            padding: '32px 18px', textAlign: 'center',
            color: '#334155', fontSize: 12,
          }}>
            No executives found for this department.
          </div>
        )}

        {/* Hierarchical tree */}
        {tree.map((root, ri) => (
          <PersonRow
            key={root.person.node_id}
            node={root}
            depth={0}
            isLast={ri === tree.length - 1}
            parentLines={[]}
            color={color}
            onToggle={toggleCollapse}
            collapsed={collapsed}
          />
        ))}
      </div>
    </div>
  )
}
