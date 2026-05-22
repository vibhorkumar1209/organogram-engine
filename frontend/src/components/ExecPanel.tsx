import React, { useState, useMemo } from 'react'
import type { OrgNode } from '../types'

interface Props {
  deptNode:   OrgNode | null
  executives: OrgNode[] | null   // null = loading; flat sorted-by-layer list
  onClose:    () => void
}

// Grade labels per Global_Designation_Hierarchy.xlsx (G0–G10)
const LAYER_LABELS: Record<number, string> = {
  0:  'Board of Management',     // G0 — Non-Executive / Supervisory
  1:  'C-Suite',                 // G1 — CEO, CFO, COO, CTO, CMO, CHRO …
  2:  'Executive VP',            // G2 — EVP / Executive Director
  3:  'SVP / Managing Director', // G3 — SVP, MD, Group President
  4:  'VP / Head of',            // G4 — VP, Head of Function
  5:  'Senior Director / AVP',   // G5 — Senior Director, Associate VP
  6:  'Director',                // G6 — Director
  7:  'Senior Manager',          // G7 — Senior Manager, Associate Director
  8:  'Manager',                 // G8 — Manager, Supervisor
  9:  'Senior / Lead / Staff',   // G9 — Senior IC, Lead, Principal, Staff
  10: 'Analyst / Specialist',    // G10 — Analyst, Specialist, Associate, IC
}

// Board of Management card — 3-layer hierarchy per Global_Designation_Hierarchy.xlsx
// G0: Chairman (apex) | G1: Vice Chair / Committee Chairs | G2: NEDs / INEDs
const BOD_LAYER_LABELS: Record<number, string> = {
  0: 'Chairman',
  1: 'Committee Chair',
  2: 'Non-Executive Director',
}

// Human-readable labels for board sub-roles (stored in node metadata.board_sub_role)
function getBoardRoleLabel(role: string): string {
  const MAP: Record<string, string> = {
    vice_chair:     'Vice Chair',
    lead_director:  'Lead Director',
    audit_chair:    'Audit Committee',
    comp_chair:     'Comp & Rem',
    nom_chair:      'Nom & Gov',
    risk_chair:     'Risk Committee',
    tech_chair:     'Tech & Innovation',
    esg_chair:      'ESG / Sustainability',
    finance_chair:  'Finance Committee',
    committee_chair:'Committee Chair',
  }
  return MAP[role] ?? ''
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
  isLast:    boolean
}

// Detect the GLOBAL CEO / Group President as the apex of EM.
// Excludes: co-/deputy/vice CEO; regional/country CEOs; plain MD (EVP, not group CEO).
function isCeoTitle(person: OrgNode): boolean {
  const d = ((person.metadata?.designation as string) ?? '').toLowerCase()
  const text = d || person.label.toLowerCase()

  // Exclude co-/deputy/vice/assistant qualifiers
  if (/\b(co-?|deputy|vice|assistant|associate)\b/.test(text)) return false

  // Exclude regional / country / divisional scope — NOT the global CEO.
  // Covers major geographies and common regional abbreviations.
  if (/\b(emea|apac|apj|latam|mena|asean|gcc|anz|asia|pacific|europe|america|americas|africa|middle\s+east|regional|divisional|country|international|australia|new\s+zealand|india|china|japan|south\s+korea|uk|u\.k\.|germany|france|italy|spain|canada|brazil|singapore|hong\s+kong|south\s+east\s+asia|southeast\s+asia)\b/.test(text)) return false

  // Exclude "[non-global word] CEO" patterns — e.g. "australia ceo", "uk ceo".
  // Allowed global prefixes: group, global, interim, acting, incoming.
  const m = text.match(/(\w+)\s+ceo\b/)
  if (m && m[1]) {
    const globalPfx = new Set(['group', 'global', 'chief', 'interim', 'acting', 'incoming', 'the'])
    if (!globalPfx.has(m[1])) return false
  }

  return (
    /\bchief\s+executive\b/.test(text) ||           // Chief Executive Officer / Chief Executive
    /\bceo\b/.test(text) ||                          // CEO / Group CEO
    /^(?:group\s+)?president$/.test(text) ||         // President or Group President (exact)
    /^group\s+(?:chief\s+executive|managing\s+director)$/.test(text)  // Group MD only
  )
}

// Core layer-group tree: each layer group reports to the layer above.
// Distributes children evenly across the parent group.
function buildLayerTree(sorted: OrgNode[]): ExecTreeNode[] {
  if (!sorted.length) return []

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

  const assignReports = (parentGroup: LayerGroup, childGroup: LayerGroup) => {
    if (parentGroup.nodes.length === 1) {
      parentGroup.nodes[0].reports = childGroup.nodes
    } else {
      const perParent = Math.ceil(childGroup.nodes.length / parentGroup.nodes.length)
      let ci = 0
      for (const parent of parentGroup.nodes) {
        parent.reports = childGroup.nodes.slice(ci, ci + perParent)
        ci += perParent
      }
    }
    for (const parent of parentGroup.nodes) {
      parent.reports.forEach((r, i) => { r.isLast = i === parent.reports.length - 1 })
    }
  }

  for (let i = 1; i < groups.length; i++) {
    assignReports(groups[i - 1], groups[i])
  }

  const roots = groups[0]?.nodes ?? []
  roots.forEach((r, i) => { r.isLast = i === roots.length - 1 })
  return roots
}

// BOD-specific tree builder.
// Per Global_Designation_Hierarchy.xlsx:
//   layer 0: Chairman (sole apex)
//   layer 1: Vice Chair + Committee Chairs (senior board roles)
//   layer 2: Regular NEDs / INEDs
// All non-Chairman directors are shown as FLAT direct reports of the Chairman
// (they all report to the Chairman per the Excel — not to each other).
// Directors are sorted: layer 1 (Committee Chairs/Vice Chair) first, then layer 2 (NEDs).
function buildBODTree(sorted: OrgNode[]): ExecTreeNode[] {
  if (!sorted.length) return []

  const chairmen = sorted.filter(p => (p.layer ?? 99) === 0)
  const others   = sorted
    .filter(p => (p.layer ?? 99) !== 0)
    .sort((a, b) => {
      const la = a.layer ?? 99; const lb = b.layer ?? 99
      return la !== lb ? la - lb : a.label.localeCompare(b.label)
    })

  if (!chairmen.length) {
    // No Chairman detected — show all directors flat
    return sorted.map((p, i) => ({ person: p, reports: [], isLast: i === sorted.length - 1 }))
  }

  // All other directors as flat direct reports of the Chairman
  const reportNodes = others.map((p, i) => ({
    person: p, reports: [], isLast: i === others.length - 1,
  }))

  // When there are multiple co-chairs (rare), first gets all reports
  return chairmen.map((p, i) => ({
    person: p,
    reports: i === 0 ? reportNodes : [],
    isLast: i === chairmen.length - 1,
  }))
}

// Main tree builder.
// BOD: Chairman at apex, all other directors flat below (buildBODTree).
// EM:  CEO promoted to single root; all other C-Suite as direct reports.
// Other depts: standard layer-group tree (buildLayerTree).
function buildExecTree(people: OrgNode[], isEM: boolean = false, isBOD: boolean = false): ExecTreeNode[] {
  if (!people.length) return []

  const sorted = [...people].sort((a, b) => {
    const la = a.layer ?? 99
    const lb = b.layer ?? 99
    return la !== lb ? la - lb : a.label.localeCompare(b.label)
  })

  // BOD: special flat-under-chairman tree
  if (isBOD) return buildBODTree(sorted)

  if (!isEM) return buildLayerTree(sorted)

  // EM: CEO at top, C-Suite peers as direct reports, deeper layers cascade.
  const topLayer = sorted[0]?.layer ?? 99
  const topGroup = sorted.filter(p => (p.layer ?? 99) === topLayer)
  const deeper   = sorted.filter(p => (p.layer ?? 99) !== topLayer)
  const ceoIdx   = topGroup.findIndex(p => isCeoTitle(p))

  // Only one person at top → standard layer tree (they're the natural apex)
  if (topGroup.length <= 1) return buildLayerTree(sorted)

  // No identifiable global CEO → flat list; avoids falsely nesting C-Suite
  // under whoever happens to sort first (e.g. a regional head or COO).
  if (ceoIdx < 0) {
    return sorted.map((p, i) => ({
      person: p, reports: [], isLast: i === sorted.length - 1,
    }))
  }

  const ceo   = topGroup[ceoIdx]
  const peers = topGroup.filter((_, i) => i !== ceoIdx)

  // peers (same layer as CEO) + deeper form the sub-tree under CEO.
  // buildLayerTree naturally puts peers at root of sub-tree, deeper layers below.
  const subTree = buildLayerTree([...peers, ...deeper])
  subTree.forEach((r, i) => { r.isLast = i === subTree.length - 1 })

  return [{ person: ceo, reports: subTree, isLast: true }]
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
          {/* Committee / sub-role badge — shown for board members with a specific role */}
          {(() => {
            const subRole = p.metadata?.board_sub_role as string | undefined
            if (!subRole || subRole === 'director' || subRole === 'chairman') return null
            const roleLabel = getBoardRoleLabel(subRole)
            if (!roleLabel) return null
            return (
              <div style={{
                display: 'inline-block', marginTop: 2,
                fontSize: 8, color: '#f59e0b',
                background: '#1c1500', borderRadius: 3,
                padding: '1px 5px', border: '1px solid #3d2900',
              }}>
                {roleLabel}
              </div>
            )
          })()}
          {/* Footer: region + location + linkedin + pay + source */}
          <div style={{ display: 'flex', gap: 7, marginTop: 2, flexWrap: 'wrap', alignItems: 'center' }}>
            {p.metadata?.region && (
              <span style={{ fontSize: 8.5, color: '#1e4d6b', background: '#0d2b3e',
                borderRadius: 3, padding: '1px 5px', border: '1px solid #1a3a52' }}>
                🌐 {String(p.metadata.region)}
              </span>
            )}
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
            {/* Source badge — website vs AI knowledge vs uploaded data */}
            {(() => {
              const m = String(p.metadata?.nlp_method ?? '')
              if (m.includes('web'))
                return (
                  <span style={{ fontSize: 8, color: '#10b981', background: '#052e16',
                    borderRadius: 3, padding: '1px 5px', border: '1px solid #064e3b',
                    marginLeft: 'auto' }}>
                    🌐 Website
                  </span>
                )
              if (m.includes('llm_leadership'))
                return (
                  <span style={{ fontSize: 8, color: '#60a5fa', background: '#0c1d2e',
                    borderRadius: 3, padding: '1px 5px', border: '1px solid #1e3a5f',
                    marginLeft: 'auto' }}>
                    ✦ AI Knowledge
                  </span>
                )
              return null
            })()}
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

  // Region sub-card collapse state (all expanded by default)
  const [collapsedRegions, setCollapsedRegions] = useState<Set<string>>(new Set())
  const toggleRegion = (region: string) =>
    setCollapsedRegions(prev => {
      const next = new Set(prev)
      if (next.has(region)) next.delete(region); else next.add(region)
      return next
    })

  const isBoard = deptNode?.node_id === 'dept__board_of_management'
  const isEM    = deptNode?.node_id === 'dept__executive_management'
  const labels  = isBoard ? BOD_LAYER_LABELS : LAYER_LABELS

  // ── Geographic grouping ──────────────────────────────────────────────
  // Group executives by their region metadata.
  // Primary region = region of the most-senior person (lowest layer number);
  // that region gets the full hierarchy tree (Chairman / CEO at apex).
  // All other regions get a local layer tree (regional head at apex).
  const regionGroups = useMemo<[string, OrgNode[]][]>(() => {
    if (!executives || executives.length === 0) return []
    const map = new Map<string, OrgNode[]>()
    for (const p of executives) {
      const r = ((p.metadata?.region as string) || '').trim() || 'Global HQ'
      if (!map.has(r)) map.set(r, [])
      map.get(r)!.push(p)
    }
    // Determine primary region from the apex person
    const apex = [...executives].sort((a, b) => (a.layer ?? 99) - (b.layer ?? 99))[0]
    const primary = ((apex?.metadata?.region as string) || '').trim() || 'Global HQ'
    // Sort: primary first, then by headcount descending
    return [...map.entries()].sort(([ra, a], [rb, b]) => {
      if (ra === primary) return -1
      if (rb === primary) return 1
      return b.length - a.length
    })
  }, [executives])

  // Layer distribution summary (header)
  const byLayer: Record<number, number> = {}
  if (executives) {
    for (const p of executives) {
      const l = p.layer ?? 9
      byLayer[l] = (byLayer[l] ?? 0) + 1
    }
  }
  const layerSummary = Object.entries(byLayer)
    .sort(([a], [b]) => Number(a) - Number(b))
    .map(([l, n]) => `${n} ${labels[Number(l)] ?? LAYER_LABELS[Number(l)] ?? `L${l}`}`)
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
              {isBoard ? 'Board of Directors' : 'Executives'}
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
            {regionGroups.length} region{regionGroups.length !== 1 ? 's' : ''} · {executives?.length ?? 0} total
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

        {/* ── Geographic sub-cards ─────────────────────────────── */}
        {regionGroups.map(([region, regionExecs], gi) => {
          // Primary region uses the full hierarchy builder (Chairman / CEO apex).
          // Non-primary regions use a local layer tree (regional head at apex).
          const isPrimary    = gi === 0
          const regionTree   = buildExecTree(regionExecs, isEM && isPrimary, isBoard)
          const isRgnCollapsed = collapsedRegions.has(region)

          return (
            <div key={region}>
              {/* Region sub-card header */}
              <div
                onClick={() => toggleRegion(region)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 7,
                  padding: '6px 14px 6px 11px',
                  background: '#050c14',
                  borderTop:   gi === 0 ? 'none' : '1px solid #080f18',
                  borderBottom: isRgnCollapsed ? 'none' : '1px solid #080f18',
                  borderLeft:  `3px solid ${color}${isPrimary ? '55' : '25'}`,
                  cursor: 'pointer', userSelect: 'none',
                }}
              >
                <span style={{ fontSize: 8.5, color: color + (isPrimary ? '99' : '55') }}>🌐</span>
                <span style={{
                  fontSize: 8.5, fontWeight: 700, letterSpacing: 1.1,
                  textTransform: 'uppercase',
                  color: isPrimary ? '#2a5878' : '#1a3a52',
                  flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {region}
                </span>
                <span style={{ fontSize: 8, color: '#122a3d', flexShrink: 0 }}>
                  {regionExecs.length} {regionExecs.length === 1 ? 'exec' : 'execs'}
                </span>
                <span style={{ fontSize: 7.5, color: '#1a3045', marginLeft: 6, flexShrink: 0 }}>
                  {isRgnCollapsed ? '▸' : '▾'}
                </span>
              </div>

              {/* People tree for this region */}
              {!isRgnCollapsed && regionTree.map((root, ri) => (
                <PersonRow
                  key={root.person.node_id}
                  node={root}
                  depth={0}
                  isLast={ri === regionTree.length - 1}
                  parentLines={[]}
                  color={color}
                  onToggle={toggleCollapse}
                  collapsed={collapsed}
                />
              ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
