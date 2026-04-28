import React from 'react'
import type { OrgNode } from '../types'

interface Props {
  node: OrgNode | null
  x: number
  y: number
}

const NODE_TYPE_LABELS: Record<string, string> = {
  global:        'Global HQ',
  region:        'Regional Office',
  dept_primary:  'Primary Department',
  dept_secondary:'Sub-Department',
  dept_tertiary: 'Team / Division',
  person:        'Employee',
  ghost:         'Placeholder',
}

const LAYER_LABELS: Record<number, string> = {
  '-1': 'Root',
  0: 'Board / Apex',
  1: 'C-Suite',
  2: 'SVP / EVP',
  3: 'VP / Director',
  4: 'Senior Director',
  5: 'Director / Head',
  6: 'Assoc. Director',
  7: 'Manager / Lead',
  8: 'Sr. Contributor',
  9: 'Contributor',
  10: 'Entry / Intern',
} as any

export const NodeTooltip: React.FC<Props> = ({ node, x, y }) => {
  if (!node) return null

  const typeLabel  = NODE_TYPE_LABELS[node.node_type] ?? node.node_type
  const layerLabel = LAYER_LABELS[node.layer] ?? `Layer ${node.layer}`

  return (
    <div
      className="tooltip"
      style={{
        left: x + 14,
        top: y - 10,
        borderColor: node.is_ghost ? '#374151' : node.color,
      }}
    >
      <div style={{ color: node.is_ghost ? '#6b7280' : node.color, fontWeight: 700, marginBottom: 6, fontSize: 13 }}>
        {node.is_ghost ? '✦ ' : ''}{node.label}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '3px 10px', color: '#94a3b8' }}>
        <span style={{ color: '#475569' }}>Type</span>
        <span>{typeLabel}</span>

        {node.layer >= 0 && (
          <>
            <span style={{ color: '#475569' }}>Layer</span>
            <span>{node.layer} — {layerLabel}</span>
          </>
        )}

        {node.sector && node.sector !== 'All' && (
          <>
            <span style={{ color: '#475569' }}>Sector</span>
            <span>{node.sector}</span>
          </>
        )}

        {node.metadata?.designation && (
          <>
            <span style={{ color: '#475569' }}>Title</span>
            <span>{node.metadata.designation}</span>
          </>
        )}

        {node.metadata?.company && (
          <>
            <span style={{ color: '#475569' }}>Company</span>
            <span>{node.metadata.company}</span>
          </>
        )}

        {node.metadata?.location && (
          <>
            <span style={{ color: '#475569' }}>Location</span>
            <span>{node.metadata.location}</span>
          </>
        )}

        {node.metadata?.dept_primary && node.node_type === 'person' && (
          <>
            <span style={{ color: '#475569' }}>Dept</span>
            <span>{node.metadata.dept_primary} › {node.metadata.dept_secondary}</span>
          </>
        )}

        {node.metadata?.linkedin_url && (
          <>
            <span style={{ color: '#475569' }}>LinkedIn</span>
            <a
              href={String(node.metadata.linkedin_url)}
              target="_blank"
              rel="noreferrer"
              style={{ color: '#3491E8', fontSize: 11, pointerEvents: 'auto' }}
              onClick={e => e.stopPropagation()}
            >
              View Profile ↗
            </a>
          </>
        )}

        {node.node_type === 'person' && node.metadata?.nlp_industry && node.metadata.nlp_industry !== 'generic' && (
          <>
            <span style={{ color: '#475569' }}>Industry</span>
            <span style={{ color: '#10b981' }}>{String(node.metadata.nlp_industry).replace(/_/g, ' ')}</span>
          </>
        )}

        {node.node_type === 'person' && node.metadata?.nlp_confidence != null && (
          <>
            <span style={{ color: '#475569' }}>Confidence</span>
            <span style={{ color: (node.metadata.nlp_confidence as number) >= 0.8 ? '#10b981' : (node.metadata.nlp_confidence as number) >= 0.6 ? '#f59e0b' : '#ef4444' }}>
              {Math.round((node.metadata.nlp_confidence as number) * 100)}%
              <span style={{ color: '#475569', fontStyle: 'italic', marginLeft: 4, fontSize: 10 }}>
                ({node.metadata.nlp_method})
              </span>
            </span>
          </>
        )}

        {node.is_ghost && (
          <>
            <span style={{ color: '#475569' }}>Note</span>
            <span style={{ color: '#6b7280', fontStyle: 'italic' }}>Data pending</span>
          </>
        )}
      </div>
    </div>
  )
}
