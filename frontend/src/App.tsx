import React, { useCallback, useEffect, useRef, useState } from 'react'
import type { OrgNode, Stats, PublicCompanyData } from './types'
import { OrgChart } from './components/OrgChart'
import { SearchBar } from './components/SearchBar'
import { ExecPanel } from './components/ExecPanel'

const API = import.meta.env.VITE_API_URL || '/api'

const DEPT_TYPES = new Set<OrgNode['node_type']>([
  'global', 'region', 'dept_primary', 'dept_secondary', 'dept_tertiary',
])

const SECTOR_COLORS: Record<string, string> = {
  Automotive: '#F59E0B',
  Govt:       '#3B82F6',
  NGO:        '#10B981',
  Startup:    '#8B5CF6',
  Public:     '#06B6D4',
  Private:    '#64748B',
}

// ── Helpers ───────────────────────────────────────────────────────────

function flattenTree(node: OrgNode, out: OrgNode[] = []): OrgNode[] {
  out.push(node)
  if (node.children) node.children.forEach(c => flattenTree(c, out))
  return out
}

function filterToDeptNodes(node: OrgNode): OrgNode {
  const allChildren = node.children ?? []
  const deptChildren = allChildren.filter(c => DEPT_TYPES.has(c.node_type))
  const hasPeopleBelow = allChildren.some(c => !DEPT_TYPES.has(c.node_type))
  return {
    ...node,
    children: deptChildren.map(filterToDeptNodes),
    expanded: false,
    has_more: node.has_more || hasPeopleBelow || undefined,
  }
}

function mergeChildren(tree: OrgNode, targetId: string, newChildren: OrgNode[]): OrgNode {
  if (tree.node_id === targetId) {
    const existing    = tree.children ?? []
    const existingIds = new Set(existing.map(c => c.node_id))
    const added       = newChildren.filter(c => !existingIds.has(c.node_id))
    return { ...tree, expanded: true, has_more: false, children: [...existing, ...added] }
  }
  return {
    ...tree,
    children: (tree.children ?? []).map(c => mergeChildren(c, targetId, newChildren)),
  }
}

function collapseNode(tree: OrgNode, targetId: string): OrgNode {
  if (tree.node_id === targetId) {
    return { ...tree, expanded: false, has_more: true, children: [] }
  }
  return {
    ...tree,
    children: (tree.children ?? []).map(c => collapseNode(c, targetId)),
  }
}

/** Build synthetic BOD and EM group nodes from public company data */
function buildSyntheticNodes(data: PublicCompanyData): OrgNode[] {
  const nodes: OrgNode[] = []

  if (data.board.length > 0) {
    nodes.push({
      node_id:      'syn_bod',
      node_type:    'dept_primary',
      label:        'Board of Directors',
      layer:        -1,
      sector:       'All',
      color:        '#3491E8',
      is_ghost:     false,
      is_synthetic: true,
      expanded:     true,
      has_more:     false,
      metadata:     { people_count: data.board.length },
      children: data.board.map((m, i) => ({
        node_id:      `syn_bod_${i}`,
        node_type:    'person' as const,
        label:        m.name,
        layer:        m.layer,
        sector:       'All',
        color:        '#3491E8',
        is_ghost:     false,
        is_synthetic: true,
        expanded:     false,
        has_more:     false,
        metadata: {
          designation: m.title,
          ...(m.age ? { age: m.age } : {}),
          ...(m.pay ? { pay: m.pay } : {}),
          data_source: (m as any).source === 'web' ? 'Website' : 'Yahoo Finance',
        },
      })),
    })
  }

  if (data.executives.length > 0) {
    nodes.push({
      node_id:      'syn_em',
      node_type:    'dept_primary',
      label:        'Executive Management',
      layer:        0,
      sector:       'All',
      color:        '#10B981',
      is_ghost:     false,
      is_synthetic: true,
      expanded:     true,
      has_more:     false,
      metadata:     { people_count: data.executives.length },
      children: data.executives.map((m, i) => ({
        node_id:      `syn_em_${i}`,
        node_type:    'person' as const,
        label:        m.name,
        layer:        m.layer,
        sector:       'All',
        color:        '#10B981',
        is_ghost:     false,
        is_synthetic: true,
        expanded:     false,
        has_more:     false,
        metadata: {
          designation: m.title,
          ...(m.age ? { age: m.age } : {}),
          ...(m.pay ? { pay: m.pay } : {}),
          data_source: (m as any).source === 'web' ? 'Website' : 'Yahoo Finance',
        },
      })),
    })
  }

  return nodes
}

function prependSyntheticNodes(tree: OrgNode, synNodes: OrgNode[]): OrgNode {
  const existing = (tree.children ?? []).filter(c => !c.is_synthetic)
  return { ...tree, children: [...synNodes, ...existing] }
}

function stripSyntheticNodes(tree: OrgNode): OrgNode {
  return { ...tree, children: (tree.children ?? []).filter(c => !c.is_synthetic) }
}

// Map a raw executive record from /executives endpoint → OrgNode
function toPersonNode(p: Record<string, any>, fallbackColor: string): OrgNode {
  return {
    node_id:   p.node_id,
    node_type: 'person',
    label:     p.label ?? `${p.first_name ?? ''} ${p.last_name ?? ''}`.trim(),
    layer:     p.layer ?? 9,
    sector:    p.sector ?? 'All',
    color:     p.color ?? fallbackColor,
    is_ghost:  false,
    expanded:  false,
    has_more:  false,
    metadata:  p.metadata ?? {
      designation:    p.designation,
      company:        p.company,
      location:       p.location,
      linkedin_url:   p.linkedin_url,
      dept_primary:   p.dept_primary,
      dept_secondary: p.dept_secondary,
      nlp_industry:   p.nlp_industry,
      nlp_confidence: p.nlp_confidence,
      nlp_method:     p.nlp_method,
    },
  }
}

// ─────────────────────────────────────────────────────────────────────
// APP
// ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [status, setStatus]         = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [statusMsg, setStatusMsg]   = useState('')
  const [deptTree, setDeptTree]     = useState<OrgNode | null>(null)
  const [viewTree, setViewTree]     = useState<OrgNode | null>(null)
  const [stats, setStats]           = useState<Stats | null>(null)
  const [allNodes, setAllNodes]     = useState<OrgNode[]>([])
  const [highlight, setHighlight]   = useState<string | null>(null)
  const [dragging, setDragging]     = useState(false)
  const [colWarning, setColWarning] = useState<string | null>(null)
  const [expandingId, setExpandingId] = useState<string | null>(null)
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null)
  const [fitGeneration, setFitGeneration] = useState(0)
  const bumpFit = () => setFitGeneration(g => g + 1)

  // Public company (ticker + domain) state
  const [ticker, setTicker]               = useState('')
  const [domain, setDomain]               = useState('')
  const [publicCompany, setPublicCompany] = useState<PublicCompanyData | null>(null)
  const [lookupLoading, setLookupLoading] = useState(false)
  const [lookupError, setLookupError]     = useState<string | null>(null)

  // ExecPanel state
  const [panelDept, setPanelDept]   = useState<OrgNode | null>(null)
  const [panelExecs, setPanelExecs] = useState<OrgNode[] | null>(null)

  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { loadDemo() }, [])

  // Keep allNodes in sync with viewTree for search
  useEffect(() => {
    if (viewTree) setAllNodes(flattenTree(viewTree))
  }, [viewTree])

  // When deptTree changes, reapply synthetic nodes if a ticker is loaded
  useEffect(() => {
    if (!deptTree) return
    if (publicCompany) {
      const synNodes = buildSyntheticNodes(publicCompany)
      setViewTree(prependSyntheticNodes(deptTree, synNodes))
    } else {
      setViewTree(deptTree)
    }
    bumpFit()
  }, [deptTree, publicCompany])

  // ── Load demo ──────────────────────────────────────────────────────
  const loadDemo = async () => {
    setStatus('loading')
    setStatusMsg('Loading demo dataset…')
    setPanelDept(null); setPanelExecs(null)
    try {
      const res = await fetch(`${API}/load-demo`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      setStats(data.stats)
      await loadDeptStructure()
    } catch {
      setStatus('error')
      setStatusMsg('Backend not running — using embedded demo. Start with: cd backend && uvicorn api_server:app')
      loadEmbeddedDemo()
    }
  }

  // ── Fetch dept-only structure ──────────────────────────────────────
  const loadDeptStructure = async () => {
    const res = await fetch(`${API}/tree?root=root_global&max_depth=1`)
    if (!res.ok) throw new Error(await res.text())
    const raw: OrgNode = await res.json()
    const filtered = filterToDeptNodes(raw)
    setDeptTree(filtered)       // useEffect above will set viewTree
    bumpFit()          // force fit-to-screen on next render
    setStatus('ready')
  }

  // ── Public company lookup (Yahoo Finance + website scrape) ───────────
  const handleLookup = async () => {
    const t = ticker.trim().toUpperCase()
    const d = domain.trim().replace(/^https?:\/\//i, '').replace(/\/.*$/, '')
    if (!t && !d) return
    setLookupLoading(true)
    setLookupError(null)
    try {
      const params = new URLSearchParams()
      if (t) params.set('ticker', t)
      if (d) params.set('domain', d)
      const res = await fetch(`${API}/public-company?${params}`)
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Lookup failed' }))
        throw new Error(err.detail ?? 'Lookup failed')
      }
      const data: PublicCompanyData = await res.json()
      setPublicCompany(data)
      // Show partial-source warnings inline but don't block
      if (data.tickerError || data.webError) {
        const warns = [data.tickerError, data.webError].filter(Boolean).join(' · ')
        setLookupError(`Partial data: ${warns}`)
      }
    } catch (e: any) {
      setLookupError(e.message)
    } finally {
      setLookupLoading(false)
    }
  }

  const handleClearLookup = () => {
    setPublicCompany(null)
    setTicker('')
    setDomain('')
    setLookupError(null)
    setDeptTree(prev => prev ? stripSyntheticNodes(prev) : null)
  }

  // ── Handle node click: expand / collapse / open panel ─────────────
  const handleNodeClick = useCallback(async (node: OrgNode) => {
    // Synthetic nodes (BOD/EM groups) are pre-expanded — don't interact
    if (node.is_synthetic) return
    // Person nodes: tooltip only
    if (node.node_type === 'person') return

    const isGlobalOrRegion = node.node_type === 'global' || node.node_type === 'region'
    const isDept = node.node_type === 'dept_primary' ||
                   node.node_type === 'dept_secondary' ||
                   node.node_type === 'dept_tertiary'

    if (isGlobalOrRegion) {
      // Collapse if already expanded
      if (node.expanded && (node.children ?? []).filter(c => !c.is_synthetic).length > 0) {
        setViewTree(prev => prev ? collapseNode(prev, node.node_id) : null)
        return
      }
      if (!node.has_more) return

      setExpandingId(node.node_id)
      try {
        const res = await fetch(
          `${API}/tree?root=${encodeURIComponent(node.node_id)}&max_depth=2`
        )
        if (!res.ok) throw new Error(await res.text())
        const fetched: OrgNode = await res.json()
        const children = (filterToDeptNodes(fetched).children ?? []).filter(c => c.node_id)
        setViewTree(prev => prev ? mergeChildren(prev, node.node_id, children) : null)
        setFocusNodeId(node.node_id)
        // Clear focusNodeId after the pan animation completes
        setTimeout(() => setFocusNodeId(null), 500)
      } catch (e: any) {
        setStatus('error'); setStatusMsg(e.message)
      } finally {
        setExpandingId(null)
      }

    } else if (isDept) {
      const realChildren = (node.children ?? []).filter(c => !c.is_synthetic)

      // ── Toggle sub-department expansion in tree ──────────────────────
      if (node.expanded && realChildren.length > 0) {
        // Collapse: remove children, re-mark as expandable
        setViewTree(prev => prev ? collapseNode(prev, node.node_id) : null)
      } else if (node.has_more) {
        // Expand: lazy-fetch only this dept's immediate sub-depts
        setExpandingId(node.node_id)
        fetch(`${API}/tree?root=${encodeURIComponent(node.node_id)}&max_depth=2`)
          .then(r => { if (!r.ok) throw new Error(); return r.json() })
          .then((fetched: OrgNode) => {
            const children = (filterToDeptNodes(fetched).children ?? []).filter(c => c.node_id)
            if (children.length > 0) {
              setViewTree(prev => prev ? mergeChildren(prev, node.node_id, children) : null)
              setFocusNodeId(node.node_id)
              setTimeout(() => setFocusNodeId(null), 500)
            }
          })
          .catch(() => {/* tree expand failed silently */})
          .finally(() => setExpandingId(null))
      }

      // ── Always open ExecPanel (employees view – never change this) ────
      setPanelDept(node)
      setPanelExecs(null)
      fetch(`${API}/executives?dept_id=${encodeURIComponent(node.node_id)}`)
        .then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() })
        .then(data => {
          const people = (data.executives as Record<string, any>[])
            .filter(p => p.node_id)
            .map(p => toPersonNode(p, node.color))
          setPanelExecs(people)
        })
        .catch(() => setPanelExecs([]))
    }
  }, [])

  // ── File upload ────────────────────────────────────────────────────
  const handleUpload = async (file: File) => {
    setStatus('loading')
    setColWarning(null)
    setPanelDept(null); setPanelExecs(null)
    setStatusMsg(`Uploading ${file.name}…`)
    const company = file.name.replace(/\.[^.]+$/, '').replace(/[-_]/g, ' ')
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await fetch(
        `${API}/upload?company_name=${encodeURIComponent(company)}`,
        { method: 'POST', body: form }
      )
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      setStats(data.stats)
      if (data.canonical_missing?.length > 0) {
        setColWarning(
          `Auto-mapped columns. Missing: ${data.canonical_missing.join(', ')}. ` +
          `Detected: ${data.detected_columns?.join(', ') ?? '(unknown)'}. ` +
          `Use headers: FirstName, LastName, Designation, Company, Location, LinkedInURL, Industry_Hint`
        )
      }
      await loadDeptStructure()
    } catch (e: any) {
      setStatus('error'); setStatusMsg(e.message)
    }
  }

  const handleSearchFocus = (nodeId: string) => {
    setHighlight(nodeId)
    setTimeout(() => setHighlight(null), 2500)
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) handleUpload(file)
  }

  // ── Embedded demo (offline fallback) ──────────────────────────────
  const loadEmbeddedDemo = () => {
    const raw: OrgNode = {
      node_id: 'root_global', node_type: 'global',
      label: 'Global Conglomerate Inc.', layer: -1, sector: 'All', color: '#3491E8',
      is_ghost: false, expanded: false, metadata: {},
      children: [
        {
          node_id: 'region__north_america', node_type: 'region',
          label: 'North America', layer: 0, sector: 'Startup', color: '#8B5CF6',
          is_ghost: false, expanded: false, has_more: true, metadata: {},
          children: [],
        },
        {
          node_id: 'region__europe', node_type: 'region',
          label: 'Europe', layer: 0, sector: 'Automotive', color: '#F59E0B',
          is_ghost: false, expanded: false, has_more: true, metadata: {},
          children: [],
        },
        {
          node_id: 'region__apac', node_type: 'region',
          label: 'Asia Pacific', layer: 0, sector: 'Govt', color: '#3B82F6',
          is_ghost: false, expanded: false, has_more: true, metadata: {},
          children: [],
        },
      ],
    }
    const filtered = filterToDeptNodes(raw)
    setDeptTree(filtered)
    setStats({ total_nodes: 23, total_edges: 22, people_nodes: 6, ghost_nodes: 0, max_depth: 7 })
    bumpFit()
    setStatus('ready')
    setStatusMsg('Demo loaded (offline mode — start backend for full dataset)')
  }

  const isBackendDown = status === 'error'

  return (
    <div
      style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column', background: '#080f16' }}
      onDragOver={e => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {/* ── HEADER ────────────────────────────────────────────── */}
      <header style={{
        height: 56, flexShrink: 0, background: '#0a1520',
        borderBottom: '1px solid #1e3a52',
        display: 'flex', alignItems: 'center', padding: '0 20px', gap: 12,
        overflow: 'hidden',
      }}>
        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginRight: 4, flexShrink: 0 }}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <polygon points="12,2 22,7 22,17 12,22 2,17 2,7" stroke="#3491E8" strokeWidth="1.5" fill="none"/>
            <circle cx="12" cy="12" r="3" fill="#3491E8"/>
            <line x1="12" y1="4" x2="12" y2="9" stroke="#3491E8" strokeWidth="1.2"/>
            <line x1="12" y1="15" x2="12" y2="20" stroke="#3491E8" strokeWidth="1.2"/>
            <line x1="4.7" y1="8.5" x2="9" y2="10.5" stroke="#3491E8" strokeWidth="1.2"/>
            <line x1="15" y1="13.5" x2="19.3" y2="15.5" stroke="#3491E8" strokeWidth="1.2"/>
            <line x1="4.7" y1="15.5" x2="9" y2="13.5" stroke="#3491E8" strokeWidth="1.2"/>
            <line x1="15" y1="10.5" x2="19.3" y2="8.5" stroke="#3491E8" strokeWidth="1.2"/>
          </svg>
          <span style={{ color: '#e2e8f0', fontWeight: 700, fontSize: 14, letterSpacing: 0.5, whiteSpace: 'nowrap' }}>
            Organogram Engine
          </span>
        </div>

        <SearchBar allNodes={allNodes} onFocus={handleSearchFocus} />

        {/* ── Public company lookup (ticker + domain) ────────── */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          {publicCompany ? (
            /* ── Loaded badge ───────────────────────────────── */
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              background: '#0a1e14', border: '1px solid #10B98144',
              borderRadius: 7, padding: '5px 10px', maxWidth: 280,
            }}>
              <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#10B981', flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 11, color: '#e2e8f0', fontWeight: 600,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {publicCompany.companyName}
                </div>
                <div style={{ fontSize: 9, color: '#334155', marginTop: 1 }}>
                  {[publicCompany.ticker, publicCompany.domain].filter(Boolean).join(' · ')}
                  {publicCompany.pageUrl && (
                    <> · <a href={publicCompany.pageUrl} target="_blank" rel="noreferrer"
                      style={{ color: '#3491E8', textDecoration: 'none' }}>source ↗</a></>
                  )}
                </div>
              </div>
              <button onClick={handleClearLookup}
                style={{ background: 'none', border: 'none', color: '#475569',
                  cursor: 'pointer', fontSize: 14, padding: 0, lineHeight: 1, flexShrink: 0 }}
                title="Clear public company data"
              >×</button>
            </div>
          ) : (
            /* ── Dual input: ticker + domain ─────────────────── */
            <div style={{ display: 'flex', alignItems: 'stretch', gap: 0 }}>
              {/* Ticker field */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 5,
                background: '#0c1e2e', border: '1px solid #1e3a52',
                borderRadius: '7px 0 0 7px', padding: '4px 8px',
              }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2">
                  <polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/>
                  <polyline points="16 7 22 7 22 13"/>
                </svg>
                <input
                  value={ticker}
                  onChange={e => setTicker(e.target.value.toUpperCase())}
                  onKeyDown={e => e.key === 'Enter' && handleLookup()}
                  placeholder="Ticker"
                  maxLength={10}
                  title="Stock ticker symbol (e.g. AAPL)"
                  style={{
                    background: 'transparent', border: 'none', outline: 'none',
                    color: '#e2e8f0', fontSize: 11, width: 56,
                    textTransform: 'uppercase',
                  }}
                />
              </div>
              {/* Divider */}
              <div style={{ width: 1, background: '#1e3a52', alignSelf: 'stretch' }} />
              {/* Domain field */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 5,
                background: '#0c1e2e', border: '1px solid #1e3a52',
                borderLeft: 'none', padding: '4px 8px',
              }}>
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2">
                  <circle cx="12" cy="12" r="10"/>
                  <line x1="2" y1="12" x2="22" y2="12"/>
                  <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                </svg>
                <input
                  value={domain}
                  onChange={e => setDomain(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleLookup()}
                  placeholder="apple.com"
                  maxLength={80}
                  title="Company domain to scrape leadership page (e.g. apple.com)"
                  style={{
                    background: 'transparent', border: 'none', outline: 'none',
                    color: '#e2e8f0', fontSize: 11, width: 90,
                  }}
                />
              </div>
              {/* Lookup button */}
              <button
                onClick={handleLookup}
                disabled={lookupLoading || (!ticker.trim() && !domain.trim())}
                style={{
                  background: lookupLoading ? '#0a1520' : '#0c3649',
                  border: '1px solid #1e3a52', borderLeft: 'none',
                  borderRadius: '0 7px 7px 0', padding: '4px 10px',
                  color: lookupLoading ? '#334155' : '#3491E8',
                  fontSize: 11, cursor: 'pointer', whiteSpace: 'nowrap',
                }}
              >
                {lookupLoading ? '⟳' : 'Lookup'}
              </button>
            </div>
          )}
          {/* Error / warning */}
          {lookupError && (
            <div style={{ fontSize: 10, color: lookupError.startsWith('Partial') ? '#F59E0B' : '#E63946',
              maxWidth: 160, lineHeight: 1.3 }}>
              {lookupError.startsWith('Partial') ? '⚠' : '✕'} {lookupError}
            </div>
          )}
        </div>

        {/* Upload */}
        <button
          onClick={() => fileInputRef.current?.click()}
          style={{
            background: '#0c3649', border: '1px solid #1e3a52', borderRadius: 7,
            padding: '5px 12px', color: '#3491E8', fontSize: 11, cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', flexShrink: 0,
          }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          Upload CSV
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.json,.xlsx,.xls"
          style={{ display: 'none' }}
          onChange={e => e.target.files?.[0] && handleUpload(e.target.files[0])}
        />

        <button
          onClick={loadDemo}
          style={{
            background: 'transparent', border: '1px solid #1e3a52', borderRadius: 7,
            padding: '5px 10px', color: '#64748b', fontSize: 11, cursor: 'pointer',
            whiteSpace: 'nowrap', flexShrink: 0,
          }}
        >
          Demo
        </button>
      </header>

      {/* ── MAIN AREA ─────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

        {/* ── SIDEBAR ──────────────────────────────────────────── */}
        <aside style={{
          width: 180, flexShrink: 0, background: '#080f16',
          borderRight: '1px solid #0c1e2e',
          padding: '14px 12px', display: 'flex', flexDirection: 'column', gap: 18,
          overflowY: 'auto',
        }}>
          {/* Stats */}
          {stats && (
            <div>
              <div style={{ fontSize: 10, color: '#334155', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
                Dataset
              </div>
              {([
                ['People',      stats.people_nodes],
                ['Departments', stats.total_nodes - stats.people_nodes - stats.ghost_nodes],
                ['Max Depth',   stats.max_depth],
              ] as [string, number][]).map(([label, val]) => (
                <div key={label} style={{
                  display: 'flex', justifyContent: 'space-between',
                  padding: '3px 0', borderBottom: '1px solid #0c1e2e', fontSize: 11,
                }}>
                  <span style={{ color: '#475569' }}>{label}</span>
                  <span style={{ color: '#3491E8', fontWeight: 700 }}>{val}</span>
                </div>
              ))}
            </div>
          )}

          {/* How to use */}
          <div>
            <div style={{ fontSize: 10, color: '#334155', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
              How to use
            </div>
            {[
              ['›', 'Click region to show departments'],
              ['›', 'Click department to view executives'],
              ['‹', 'Click region again to collapse'],
              ['⊙', 'Fit chart to screen'],
            ].map(([icon, tip]) => (
              <div key={tip} style={{ display: 'flex', gap: 6, padding: '3px 0', fontSize: 10, color: '#374e65' }}>
                <span style={{ color: '#3491E8', width: 10, flexShrink: 0 }}>{icon}</span>
                <span>{tip}</span>
              </div>
            ))}
          </div>

          {/* Sector legend */}
          <div>
            <div style={{ fontSize: 10, color: '#334155', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
              Sectors
            </div>
            {Object.entries(SECTOR_COLORS).map(([sector, color]) => (
              <div key={sector} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 10 }}>
                <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
                <span style={{ color: '#94a3b8' }}>{sector}</span>
              </div>
            ))}
          </div>

          {/* Seniority scale */}
          <div>
            <div style={{ fontSize: 10, color: '#334155', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
              Seniority
            </div>
            {([
              [0, 'Board / NED'],
              [1, 'C-Suite'],
              [2, 'MD / EVP'],
              [3, 'VP'],
              [4, 'Sr. Director'],
              [5, 'Director'],
              [6, 'Sr. Manager'],
              [7, 'Manager'],
              [8, 'Sr. IC'],
              [9, 'IC / Analyst'],
              [10,'Graduate'],
            ] as [number, string][]).map(([l, label]) => (
              <div key={l} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0', fontSize: 10 }}>
                <div style={{
                  width: 20, height: 14, borderRadius: 3,
                  background: '#0c3649', color: '#3491E8',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 9, fontWeight: 700, flexShrink: 0,
                }}>L{l}</div>
                <span style={{ color: '#475569' }}>{label}</span>
              </div>
            ))}
          </div>
        </aside>

        {/* ── CHART AREA ───────────────────────────────────────── */}
        <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>

          {/* Status bar */}
          {(status === 'loading' || isBackendDown) && (
            <div style={{
              position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
              zIndex: 50, background: isBackendDown ? '#1a0a0a' : '#0c1e2e',
              border: `1px solid ${isBackendDown ? '#E63946' : '#1e3a52'}`,
              borderRadius: 8, padding: '8px 16px', fontSize: 12,
              color: isBackendDown ? '#E63946' : '#94a3b8',
              maxWidth: 620, textAlign: 'center',
            }}>
              {status === 'loading' ? `⟳ ${statusMsg}` : `⚠ ${statusMsg}`}
            </div>
          )}

          {/* Expanding indicator */}
          {expandingId && (
            <div style={{
              position: 'absolute', top: 12, right: 60, zIndex: 50,
              background: '#0c1e2e', border: '1px solid #1e3a52',
              borderRadius: 8, padding: '6px 14px', fontSize: 11, color: '#3491E8',
            }}>
              ⟳ Loading…
            </div>
          )}

          {/* Column mapping warning */}
          {colWarning && status === 'ready' && (
            <div style={{
              position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
              zIndex: 50, background: '#1a1200', border: '1px solid #F59E0B',
              borderRadius: 8, padding: '10px 16px', fontSize: 11, color: '#F59E0B',
              maxWidth: 640, display: 'flex', gap: 10, alignItems: 'flex-start',
            }}>
              <span style={{ flexShrink: 0, fontSize: 14 }}>⚠</span>
              <span>{colWarning}</span>
              <button
                onClick={() => setColWarning(null)}
                style={{ background: 'none', border: 'none', color: '#F59E0B', cursor: 'pointer', fontSize: 16, padding: 0 }}
              >×</button>
            </div>
          )}

          {/* Drag overlay */}
          {dragging && (
            <div style={{
              position: 'absolute', inset: 0, zIndex: 100,
              background: 'rgba(52,145,232,0.08)', border: '2px dashed #3491E8',
              borderRadius: 8, margin: 12,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <div style={{ fontSize: 18, color: '#3491E8' }}>Drop CSV / JSON / Excel to load</div>
            </div>
          )}

          {/* Idle state */}
          {status === 'idle' && (
            <div style={{
              position: 'absolute', inset: 0,
              display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: 16,
            }}>
              <div style={{ fontSize: 48, opacity: 0.1 }}>⬡</div>
              <div style={{ color: '#334155', fontSize: 14 }}>Drop a file or click "Demo"</div>
            </div>
          )}

          {/* Chart */}
          {(status === 'ready' || isBackendDown) && viewTree && (
            <OrgChart
              tree={viewTree}
              highlightId={highlight}
              onNodeClick={handleNodeClick}
              focusNodeId={focusNodeId}
              fitGeneration={fitGeneration}
            />
          )}

          {/* Hint bar */}
          {(status === 'ready' || isBackendDown) && viewTree && (
            <div style={{
              position: 'absolute', bottom: 12, left: '50%', transform: 'translateX(-50%)',
              fontSize: 11, color: '#1e3a52', whiteSpace: 'nowrap', pointerEvents: 'none',
            }}>
              Click a region › to expand departments&nbsp;·&nbsp;
              Click a department › to view executives&nbsp;·&nbsp;
              Scroll / pinch to zoom
            </div>
          )}

          {/* ExecPanel — overlays right side of chart */}
          <ExecPanel
            deptNode={panelDept}
            executives={panelExecs}
            onClose={() => { setPanelDept(null); setPanelExecs(null) }}
          />
        </div>
      </div>
    </div>
  )
}
