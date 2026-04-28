import React, { useState, useRef } from 'react'
import type { OrgNode } from '../types'

interface Props {
  allNodes: OrgNode[]
  onFocus: (nodeId: string) => void
}

export const SearchBar: React.FC<Props> = ({ allNodes, onFocus }) => {
  const [query, setQuery]     = useState('')
  const [results, setResults] = useState<OrgNode[]>([])
  const [open, setOpen]       = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const q = e.target.value
    setQuery(q)
    if (q.trim().length < 2) {
      setResults([])
      setOpen(false)
      return
    }
    const q_lower = q.toLowerCase()
    const str = (v: string | number | undefined) => String(v ?? '')
    const matches = allNodes
      .filter(n =>
        n.label.toLowerCase().includes(q_lower) ||
        str(n.metadata?.designation).toLowerCase().includes(q_lower) ||
        str(n.metadata?.dept_primary).toLowerCase().includes(q_lower) ||
        n.sector.toLowerCase().includes(q_lower)
      )
      .slice(0, 8)
    setResults(matches)
    setOpen(matches.length > 0)
  }

  const handleSelect = (node: OrgNode) => {
    setQuery(node.label)
    setOpen(false)
    onFocus(node.node_id)
  }

  const NODE_ICONS: Record<string, string> = {
    global:         '🌐',
    region:         '📍',
    dept_primary:   '🏢',
    dept_secondary: '📂',
    dept_tertiary:  '📁',
    person:         '👤',
    ghost:          '◌',
  }

  return (
    <div style={{ position: 'relative' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        background: '#0c1e2e', border: '1px solid #1e3a52',
        borderRadius: 8, padding: '6px 12px',
      }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          ref={inputRef}
          value={query}
          onChange={handleChange}
          onFocus={() => results.length > 0 && setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder="Search name, title, department…"
          style={{
            background: 'transparent', border: 'none', outline: 'none',
            color: '#e2e8f0', fontSize: 13, width: 220,
          }}
        />
        {query && (
          <button
            onClick={() => { setQuery(''); setResults([]); setOpen(false) }}
            style={{ background: 'none', border: 'none', color: '#475569',
                     cursor: 'pointer', padding: 0, lineHeight: 1, fontSize: 16 }}
          >×</button>
        )}
      </div>

      {open && (
        <div style={{
          position: 'absolute', top: '110%', left: 0, right: 0,
          background: '#0a1520', border: '1px solid #1e3a52',
          borderRadius: 8, overflow: 'hidden', zIndex: 200,
          boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
        }}>
          {results.map(node => (
            <div
              key={node.node_id}
              onMouseDown={() => handleSelect(node)}
              style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '8px 14px', cursor: 'pointer',
                borderBottom: '1px solid #0c1e2e',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#0c3649')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              <span style={{ fontSize: 16 }}>{NODE_ICONS[node.node_type] ?? '•'}</span>
              <div>
                <div style={{ fontSize: 13, color: '#e2e8f0' }}>{node.label}</div>
                {node.metadata?.designation && (
                  <div style={{ fontSize: 11, color: '#475569' }}>
                    {node.metadata.designation} · {node.sector}
                  </div>
                )}
              </div>
              <div style={{
                marginLeft: 'auto', fontSize: 10, color: node.color,
                background: node.color + '22', borderRadius: 4,
                padding: '2px 6px', whiteSpace: 'nowrap',
              }}>
                L{node.layer}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
