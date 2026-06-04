import React, { useCallback, useEffect, useRef, useState } from 'react'
import type { OrgNode, Stats } from './types'
import { OrgChart } from './components/OrgChart'
import { SearchBar } from './components/SearchBar'
import { ExecPanel } from './components/ExecPanel'

const API = (import.meta.env.VITE_API_URL || '/api').trim()

// ── History ────────────────────────────────────────────────────────────
const HISTORY_KEY  = 'orgchart_history'
const HISTORY_MAX  = 12

interface HistoryEntry {
  id:          string
  companyName: string
  timestamp:   number
  deptTree:    OrgNode
  stats:       Stats
  industry:    string
  source:      'demo' | 'upload'   // so restore knows whether to reload backend
  execCache:   Record<string, OrgNode[]>   // dept_id → executives (for offline restore)
}

function loadHistory(): HistoryEntry[] {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]') } catch { return [] }
}
function persistHistory(entries: HistoryEntry[]) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(entries)) } catch {}
}
function relativeTime(ts: number): string {
  const m = Math.floor((Date.now() - ts) / 60_000)
  if (m < 1)  return 'Just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

const DEPT_TYPES = new Set<OrgNode['node_type']>([
  'global', 'dept_primary', 'dept_secondary', 'dept_tertiary',
])

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
  const [industry, setIndustry]     = useState<string>('')
  const [allNodes, setAllNodes]     = useState<OrgNode[]>([])
  const [highlight, setHighlight]   = useState<string | null>(null)
  const [dragging, setDragging]     = useState(false)
  const [colWarning, setColWarning] = useState<string | null>(null)
  const [expandingId, setExpandingId] = useState<string | null>(null)
  const [focusNodeId, setFocusNodeId] = useState<string | null>(null)
  const [fitGeneration, setFitGeneration] = useState(0)
  const bumpFit = () => setFitGeneration(g => g + 1)

  // BOD/EM enrichment status shown below the chart
  const [leadershipNote, setLeadershipNote] = useState<string>('')

  // ExecPanel state
  const [panelDept, setPanelDept]   = useState<OrgNode | null>(null)
  const [panelExecs, setPanelExecs] = useState<OrgNode[] | null>(null)
  // Total person count for the open dept (may exceed panelExecs.length when paginated)
  const [panelTotal, setPanelTotal] = useState<number>(0)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [companyWebsite, setCompanyWebsite] = useState('')

  // ── History state ──────────────────────────────────────────────────
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory())

  // Refs for stale-closure-safe access inside handleNodeClick ([] deps)
  const activeEntryIdRef = useRef<string | null>(null)
  const historyRef       = useRef<HistoryEntry[]>(history)
  useEffect(() => { historyRef.current = history }, [history])

  const saveSnapshot = useCallback((tree: OrgNode, s: Stats, ind: string, src: 'demo' | 'upload' = 'upload') => {
    const entry: HistoryEntry = {
      id:          `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
      companyName: tree.label || 'Unknown Company',
      timestamp:   Date.now(),
      deptTree:    tree,
      stats:       s,
      industry:    ind,
      source:      src,
      execCache:   {},
    }
    activeEntryIdRef.current = entry.id
    setHistory(prev => {
      // Replace existing entry for same company so history stays tidy
      const filtered = prev.filter(e => e.companyName !== entry.companyName)
      const next = [entry, ...filtered].slice(0, HISTORY_MAX)
      persistHistory(next)
      return next
    })
  }, [])

  const restoreSnapshot = useCallback((entry: HistoryEntry) => {
    // Restore visual state from localStorage immediately
    activeEntryIdRef.current = entry.id
    setDeptTree(entry.deptTree)
    setStats(entry.stats)
    setIndustry(entry.industry)
    setStatus('ready')
    setStatusMsg('')
    setPanelDept(null); setPanelExecs(null)
    setHighlight(null)
    bumpFit()
    // Demo entries: reload the dataset so /executives calls succeed after restore.
    // Upload entries: exec cache covers this; backend keeps data in memory.
    if (entry.source === 'demo') {
      fetch(`${API}/load-demo`, { method: 'POST' }).catch(() => {})
    }
  }, [])

  const deleteSnapshot = useCallback((id: string) => {
    setHistory(prev => {
      const next = prev.filter(e => e.id !== id)
      persistHistory(next)
      return next
    })
  }, [])

  // Write fetched executives into a specific history entry's exec cache
  const updateExecCache = useCallback((entryId: string, deptId: string, people: OrgNode[]) => {
    setHistory(prev => {
      const next = prev.map(e =>
        e.id === entryId
          ? { ...e, execCache: { ...e.execCache, [deptId]: people } }
          : e
      )
      persistHistory(next)
      return next
    })
  }, [])

  // Eagerly pre-fetch executives for every dept in the tree so the history
  // entry is self-contained — restoring from history shows the full org chart
  // without any backend calls.
  // Skipped for large datasets (>10K people) to avoid fetching MBs of data
  // in the background — users click depts to load people on demand instead.
  const prefetchAllExecutives = useCallback(async (
    tree: OrgNode, entryId: string, currentStats?: Stats | null,
  ) => {
    if ((currentStats?.people_nodes ?? 0) > 10_000) return

    const deptNodes = flattenTree(tree).filter(n =>
      n.node_type === 'dept_primary' ||
      n.node_type === 'dept_secondary' ||
      n.node_type === 'dept_tertiary'
    )
    if (!deptNodes.length) return

    // Fetch 4 departments concurrently to balance speed vs backend load
    const BATCH = 4
    for (let i = 0; i < deptNodes.length; i += BATCH) {
      await Promise.all(
        deptNodes.slice(i, i + BATCH).map(async (node) => {
          try {
            const r = await fetch(`${API}/executives?dept_id=${encodeURIComponent(node.node_id)}&limit=200`)
            if (!r.ok) return
            const data = await r.json()
            if (data.loaded === false) return
            const people: OrgNode[] = (data.executives as Record<string, any>[])
              .filter((p: Record<string, any>) => p.node_id)
              .map((p: Record<string, any>) => toPersonNode(p, node.color))
            updateExecCache(entryId, node.node_id, people)
          } catch { /* silent — background pre-fetch */ }
        })
      )
    }
  }, [updateExecCache])

  const handleReset = async () => {
    try { await fetch(`${API}/reset`, { method: 'POST' }) } catch {}
    setStatus('idle')
    setStatusMsg('')
    setDeptTree(null)
    setViewTree(null)
    setStats(null)
    setIndustry('')
    setAllNodes([])
    setPanelDept(null)
    setPanelExecs(null)
    setPanelTotal(0)
    setColWarning(null)
    setHighlight(null)
    setCompanyWebsite('')
  }

  useEffect(() => {
    loadDemo()
  }, [])

  // Keep allNodes in sync with viewTree for search
  useEffect(() => {
    if (viewTree) setAllNodes(flattenTree(viewTree))
  }, [viewTree])

  // When deptTree changes, sync to viewTree
  useEffect(() => {
    if (!deptTree) return
    setViewTree(deptTree)
    bumpFit()
  }, [deptTree])

  // ── Load demo ──────────────────────────────────────────────────────
  const loadDemo = async (retrying = false) => {
    setStatus('loading')
    setStatusMsg(retrying ? 'Reconnecting to backend…' : 'Loading demo dataset…')
    setPanelDept(null); setPanelExecs(null)
    try {
      const res = await fetch(`${API}/load-demo`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      setStats(data.stats)
      await loadDeptStructure(data.stats, data.industry ?? '', 'demo')
    } catch (e: any) {
      const isNetwork = (e?.message ?? '').toLowerCase().includes('fetch')
      if (isNetwork && !retrying) {
        // Backend may be mid-deploy — wait 6s and retry once
        setStatusMsg('Backend waking up — retrying in 6s…')
        setTimeout(() => loadDemo(true), 6000)
        return
      }
      setStatus('error')
      setStatusMsg('Backend not running — using embedded demo. Start with: cd backend && uvicorn api_server:app')
      loadEmbeddedDemo()
    }
  }

  // ── Fetch dept-only structure ──────────────────────────────────────
  const loadDeptStructure = async (currentStats?: Stats, currentIndustry?: string, src: 'demo' | 'upload' = 'upload') => {
    // dept_only=true: strips person nodes server-side; adds headcount to each
    // dept node.  Keeps the response small regardless of how many people are
    // in the org — people load on demand via /executives when a dept is clicked.
    const res = await fetch(`${API}/tree?root=root_global&max_depth=3&dept_only=true`)
    if (!res.ok) throw new Error(await res.text())
    const raw: OrgNode = await res.json()
    const filtered = filterToDeptNodes(raw)
    setDeptTree(filtered)       // useEffect above will set viewTree
    bumpFit()          // force fit-to-screen on next render
    setStatus('ready')
    // Auto-save to history whenever a chart successfully loads
    if (currentStats) {
      saveSnapshot(filtered, currentStats, currentIndustry ?? '', src)
      // Fire-and-forget: eagerly cache all executives so history is self-contained.
      // Skipped for large datasets — people load on demand instead.
      const snapId = activeEntryIdRef.current
      if (snapId) prefetchAllExecutives(filtered, snapId, currentStats)
    }
  }

  // ── Handle node click: expand / collapse / open panel ─────────────
  const handleNodeClick = useCallback(async (node: OrgNode) => {
    // Synthetic nodes (BOD/EM groups) are pre-expanded — don't interact
    if (node.is_synthetic) return
    // Person nodes: tooltip only — interaction is through dept panel
    if (node.node_type === 'person') return
    // Global root is invisible — cannot be clicked
    if (node.node_type === 'global') return

    const isDept = node.node_type === 'dept_primary' ||
                   node.node_type === 'dept_secondary' ||
                   node.node_type === 'dept_tertiary'

    if (isDept) {
      const realChildren = (node.children ?? []).filter(c => !c.is_synthetic)

      // ── Toggle sub-department expansion in tree ──────────────────────
      if (node.expanded && realChildren.length > 0) {
        // Collapse: remove children, re-mark as expandable
        setViewTree(prev => prev ? collapseNode(prev, node.node_id) : null)
      } else if (node.has_more) {
        // Expand: lazy-fetch only this dept's immediate sub-depts (dept_only keeps it small)
        setExpandingId(node.node_id)
        fetch(`${API}/tree?root=${encodeURIComponent(node.node_id)}&max_depth=2&dept_only=true`)
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
      setPanelTotal(0)

      // Check exec cache first — allows offline history restore to show executives
      const entryId = activeEntryIdRef.current
      const cached  = entryId
        ? (historyRef.current.find(e => e.id === entryId)?.execCache ?? {})[node.node_id]
        : undefined

      if (cached !== undefined) {
        // Serve from cache instantly — no backend call needed
        setPanelExecs(cached)
        setPanelTotal(cached.length)
      } else {
        setPanelExecs(null)
        const deptId    = node.node_id
        const nodeColor = node.color

        // First page: most-senior 200 people (sorted by layer asc then name).
        // panelTotal carries the full count so ExecPanel can show "200 of N".
        const fetchAndSet = (retryAfterReload = false): Promise<void> =>
          fetch(`${API}/executives?dept_id=${encodeURIComponent(deptId)}&limit=200`)
            .then(r => { if (!r.ok) throw new Error(r.statusText); return r.json() })
            .then(async (data) => {
              // Backend restarted and lost in-memory data (Render free tier)
              if (data.loaded === false && !retryAfterReload) {
                const src = entryId
                  ? historyRef.current.find(e => e.id === entryId)?.source
                  : undefined
                if (src === 'demo') {
                  // Auto-reload demo and retry once
                  await fetch(`${API}/load-demo`, { method: 'POST' })
                  return fetchAndSet(true)
                }
                // Upload entry — backend can't restore without the file
                setPanelExecs([])
                return
              }
              const people = (data.executives as Record<string, any>[])
                .filter((p: Record<string, any>) => p.node_id)
                .map((p: Record<string, any>) => toPersonNode(p, nodeColor))
              setPanelExecs(people)
              setPanelTotal(data.total ?? people.length)
              if (entryId) updateExecCache(entryId, deptId, people)
            })
            .catch(() => setPanelExecs([]))

        fetchAndSet()
      }
    }
  }, [updateExecCache])

  // ── File upload ────────────────────────────────────────────────────
  const handleUpload = async (file: File) => {
    setStatus('loading')
    setColWarning(null)
    setPanelDept(null); setPanelExecs(null)
    const form = new FormData()
    form.append('file', file)

    // Tick elapsed seconds — full pipeline (NLP + web BOD/EM enrichment) takes ~60s
    let elapsed = 0
    const tick = setInterval(() => {
      elapsed += 1
      const phase =
        elapsed < 30 ? 'Classifying roles & departments…' :
        elapsed < 55 ? 'Building org hierarchy…' :
        'Fetching Board & Executive leadership from web…'
      setStatusMsg(`${phase}  (${elapsed}s)`)
    }, 1000)
    setStatusMsg(`Processing ${file.name}…`)

    try {
      const uploadUrl = companyWebsite.trim()
        ? `${API}/upload?company_website=${encodeURIComponent(companyWebsite.trim())}`
        : `${API}/upload`

      const res = await fetch(uploadUrl, { method: 'POST', body: form })
      clearInterval(tick)
      if (!res.ok) {
        const errText = await res.text()
        let detail = errText
        try { detail = JSON.parse(errText).detail ?? errText } catch {}
        throw new Error(detail)
      }
      const data = await res.json()
      setStats(data.stats)
      if (data.industry) setIndustry(data.industry)
      if (data.canonical_missing?.length > 0) {
        setColWarning(
          `Could not detect: ${data.canonical_missing.join(' · ')}. ` +
          `Check that your file has columns for name, job title, and company. ` +
          `Detected columns: ${data.detected_columns?.join(', ') ?? '(unknown)'}.`
        )
      }
      await loadDeptStructure(data.stats, data.industry ?? '', 'upload')

      // ── Poll for BOD/EM data (background enrichment takes ~90s) ──────
      // Parallel.AI research runs in the background. Poll every 10s up to
      // 3 minutes; when data arrives, silently reload the dept tree so
      // Board of Directors and Executive Management panels populate.
      setLeadershipNote('🔍 Searching for Board & Executive leadership online…')
      let pollCount = 0
      const MAX_POLLS = 30  // 30 × 10s = 5 minutes (Parallel.AI needs up to 160s)
      const pollTimer = setInterval(async () => {
        pollCount++
        if (pollCount > MAX_POLLS) {
          clearInterval(pollTimer)
          setLeadershipNote('⚠ Leadership search timed out — add a company website URL and re-upload to retry.')
          return
        }
        try {
          const pr = await fetch(`${API}/leadership-ready`)
          if (!pr.ok) { clearInterval(pollTimer); return }
          const pd = await pr.json()
          // Update industry if background task refined it
          if (pd.industry && pd.industry !== industry) {
            setIndustry(pd.industry)
          }
          if (pd.ready) {
            clearInterval(pollTimer)
            setLeadershipNote(`✅ Found ${pd.board_count} board members · ${pd.exec_count} executives`)
            // Quietly reload the tree — BOD/EM nodes now exist in the DAG
            await loadDeptStructure(data.stats, pd.industry || data.industry || '', 'upload')
          } else if (pd.enrichment_done) {
            clearInterval(pollTimer)
            setLeadershipNote('ℹ No public leadership data found — add company website URL and re-upload to retry')
          }
        } catch { clearInterval(pollTimer) }
      }, 10_000)

    } catch (e: any) {
      clearInterval(tick)
      setStatus('error')
      const msg: string = e?.message ?? ''
      const isNetwork = msg.toLowerCase().includes('fetch') || msg.toLowerCase().includes('network')
      setStatusMsg(
        isNetwork
          ? 'Connection failed — backend may be restarting. Wait a moment and try again.'
          : msg || 'Upload failed — please try again.'
      )
    }
  }

  // ── Export org chart as CSV ───────────────────────────────────────
  const [exporting,    setExporting]    = useState(false)
  const [exportError,  setExportError]  = useState('')
  const handleExport = async () => {
    setExporting(true)
    setExportError('')
    try {
      const res = await fetch(`${API}/export?fmt=csv`)
      if (!res.ok) {
        const txt = await res.text().catch(() => `HTTP ${res.status}`)
        throw new Error(txt || `Server error ${res.status}`)
      }
      const blob = await res.blob()
      const cd   = res.headers.get('content-disposition') ?? ''
      const name = cd.match(/filename="([^"]+)"/)?.[1] ?? 'org_chart.csv'
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href = url; a.download = name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e: any) {
      const msg: string = e?.message ?? ''
      setExportError(
        msg.includes('No data') ? 'Re-upload your CSV — backend was restarted'
        : msg.includes('fetch')  ? 'Backend offline — try again in ~30s'
        : msg.slice(0, 120) || 'CSV export failed'
      )
    }
    finally { setExporting(false) }
  }

  // ── Export org chart as PPTX ──────────────────────────────────────
  const [exportingPPT,   setExportingPPT]   = useState(false)
  const [pptError,       setPptError]       = useState('')
  const [backendCompany, setBackendCompany] = useState<string | null>(null)
  // Fetch backend company on mount and after each upload (when status becomes 'ready')
  useEffect(() => {
    if (status !== 'ready') return
    fetch(`${API}/company`).then(r => r.ok ? r.json() : null).then(d => {
      setBackendCompany(d?.loaded ? (d.company ?? null) : null)
    }).catch(() => setBackendCompany(null))
  }, [status])
  const handleExportPPT = async () => {
    setExportingPPT(true)
    setPptError('')
    try {
      // ── Sanity-check: backend must have the same company as what's displayed ──
      const companyRes = await fetch(`${API}/company`).catch(() => null)
      if (companyRes && companyRes.ok) {
        const { loaded, company: backendCo } = await companyRes.json()
        if (!loaded) {
          throw new Error('No data loaded on backend — re-upload your CSV first.')
        }
        const frontendCo = (deptTree?.label ?? '').trim()
        // Fuzzy match: backend label must appear in or equal the frontend label
        const normalise = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (
          frontendCo &&
          backendCo &&
          !normalise(backendCo).includes(normalise(frontendCo).slice(0, 6)) &&
          !normalise(frontendCo).includes(normalise(backendCo).slice(0, 6))
        ) {
          throw new Error(
            `Backend has "${backendCo}" loaded, but you're viewing "${frontendCo}". ` +
            `Re-upload the correct CSV to export this company.`
          )
        }
      }

      const res = await fetch(`${API}/export/pptx`)
      if (!res.ok) {
        const txt = await res.text().catch(() => `HTTP ${res.status}`)
        throw new Error(txt || `Server error ${res.status}`)
      }
      const blob = await res.blob()
      const cd   = res.headers.get('content-disposition') ?? ''
      const name = cd.match(/filename="([^"]+)"/)?.[1] ?? 'org_chart.pptx'
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href = url; a.download = name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e: any) {
      const msg: string = e?.message ?? ''
      setPptError(
        msg.includes('fetch') ? 'Backend restarting — try again in ~30s'
        : msg.slice(0, 200) || 'PPT export failed'
      )
    }
    finally { setExportingPPT(false) }
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
      label: 'AutoPrime Motors', layer: -1, sector: 'All', color: '#3491E8',
      is_ghost: false, expanded: false, metadata: {},
      children: [
        {
          node_id: 'dept__board_of_management', node_type: 'dept_primary',
          label: 'Board of Management', layer: 1, sector: 'All', color: '#3491E8',
          is_ghost: false, expanded: true, has_more: false, metadata: {},
          children: [
            {
              node_id: 'dept__executive_management', node_type: 'dept_primary',
              label: 'Executive Management', layer: 1, sector: 'All', color: '#3491E8',
              is_ghost: false, expanded: true, has_more: false, metadata: {},
              children: [
                {
                  node_id: 'dept__finance', node_type: 'dept_primary',
                  label: 'Finance', layer: 1, sector: 'All', color: '#3491E8',
                  is_ghost: false, expanded: false, has_more: true, metadata: {},
                  children: [],
                },
                {
                  node_id: 'dept__technology', node_type: 'dept_primary',
                  label: 'Technology', layer: 1, sector: 'All', color: '#3491E8',
                  is_ghost: false, expanded: false, has_more: true, metadata: {},
                  children: [],
                },
                {
                  node_id: 'dept__human_resources', node_type: 'dept_primary',
                  label: 'Human Resources', layer: 1, sector: 'All', color: '#3491E8',
                  is_ghost: false, expanded: false, has_more: true, metadata: {},
                  children: [],
                },
              ],
            },
          ],
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
      style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column', background: '#ffffff' }}
      onDragOver={e => { e.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {/* ── HEADER ────────────────────────────────────────────── */}
      <header style={{
        height: 56, flexShrink: 0,
        background: 'linear-gradient(135deg, #0c3649, #12516e)',
        borderBottom: '1px solid rgba(255,255,255,0.1)',
        display: 'flex', alignItems: 'center', padding: '0 20px', gap: 12,
        overflow: 'hidden',
      }}>
        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginRight: 4, flexShrink: 0 }}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <polygon points="12,2 22,7 22,17 12,22 2,17 2,7" stroke="#ffffff" strokeWidth="1.5" fill="none"/>
            <circle cx="12" cy="12" r="3" fill="#ffffff"/>
            <line x1="12" y1="4" x2="12" y2="9" stroke="#ffffff" strokeWidth="1.2"/>
            <line x1="12" y1="15" x2="12" y2="20" stroke="#ffffff" strokeWidth="1.2"/>
            <line x1="4.7" y1="8.5" x2="9" y2="10.5" stroke="#ffffff" strokeWidth="1.2"/>
            <line x1="15" y1="13.5" x2="19.3" y2="15.5" stroke="#ffffff" strokeWidth="1.2"/>
            <line x1="4.7" y1="15.5" x2="9" y2="13.5" stroke="#ffffff" strokeWidth="1.2"/>
            <line x1="15" y1="10.5" x2="19.3" y2="8.5" stroke="#ffffff" strokeWidth="1.2"/>
          </svg>
          <span style={{ color: '#ffffff', fontWeight: 700, fontSize: 14, letterSpacing: 0.5, whiteSpace: 'nowrap' }}>
            Organogram Engine
          </span>
        </div>

        <SearchBar allNodes={allNodes} onFocus={handleSearchFocus} />

        {/* Upload */}
        <button
          onClick={() => fileInputRef.current?.click()}
          style={{
            background: '#E63946', border: 'none', borderRadius: 7,
            padding: '5px 12px', color: '#ffffff', fontSize: 11, cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', flexShrink: 0,
            fontWeight: 600,
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
          onClick={() => loadDemo()}
          style={{
            background: 'transparent', border: '1px solid rgba(255,255,255,0.25)', borderRadius: 7,
            padding: '5px 10px', color: 'rgba(255,255,255,0.75)', fontSize: 11, cursor: 'pointer',
            whiteSpace: 'nowrap', flexShrink: 0,
          }}
        >
          Demo
        </button>

        {status === 'ready' && (
          <button
            onClick={handleExport}
            disabled={exporting}
            title="Download org chart + executives as CSV"
            style={{
              background: 'transparent', border: '1px solid rgba(52,145,232,0.45)', borderRadius: 7,
              padding: '5px 10px', color: '#3491E8', fontSize: 11, cursor: exporting ? 'wait' : 'pointer',
              whiteSpace: 'nowrap', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 5,
              opacity: exporting ? 0.6 : 1,
            }}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            {exporting ? 'Saving…' : 'Save CSV'}
          </button>
        )}
        {exportError && status === 'ready' && (
          <div style={{ fontSize: 9, color: '#E63946', maxWidth: 160, textAlign: 'right', lineHeight: 1.3 }}>
            ⚠ {exportError}
          </div>
        )}

        {status === 'ready' && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
            <button
              onClick={handleExportPPT}
              disabled={exportingPPT}
              title={`Download org chart as PowerPoint for ${deptTree?.label ?? 'current company'} — one slide per department`}
              style={{
                background: 'transparent', border: '1px solid rgba(52,145,232,0.45)', borderRadius: 7,
                padding: '5px 10px', color: '#3491E8', fontSize: 11, cursor: exportingPPT ? 'wait' : 'pointer',
                whiteSpace: 'nowrap', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 5,
                opacity: exportingPPT ? 0.6 : 1,
              }}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <rect x="2" y="3" width="20" height="14" rx="2"/>
                <path d="M8 21h8M12 17v4"/>
              </svg>
              {exportingPPT ? 'Building…' : 'Download PPT'}
            </button>
            {/* Show which company the backend will export — alerts user if stale */}
            {!exportingPPT && (backendCompany || deptTree?.label) && (() => {
              const bco = backendCompany ?? ''
              const fco = deptTree?.label ?? ''
              const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, '')
              const mismatch = bco && fco &&
                !norm(bco).includes(norm(fco).slice(0, 6)) &&
                !norm(fco).includes(norm(bco).slice(0, 6))
              return (
                <div style={{ fontSize: 9, maxWidth: 180, textAlign: 'right', lineHeight: 1.3,
                              color: mismatch ? '#E63946' : '#4a7a9b' }}>
                  {mismatch
                    ? `⚠ Backend: "${bco}" — re-upload to export "${fco}"`
                    : `For: ${bco || fco}`
                  }
                </div>
              )
            })()}
            {pptError && (
              <div style={{ fontSize: 9, color: '#E63946', maxWidth: 180, textAlign: 'right', lineHeight: 1.3 }}>
                ⚠ {pptError}
              </div>
            )}
          </div>
        )}

        {status !== 'idle' && (
          <button
            onClick={handleReset}
            title="Clear loaded data"
            style={{
              background: 'transparent', border: '1px solid rgba(230,57,70,0.45)', borderRadius: 7,
              padding: '5px 10px', color: '#E63946', fontSize: 11, cursor: 'pointer',
              whiteSpace: 'nowrap', flexShrink: 0, display: 'flex', alignItems: 'center', gap: 5,
            }}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
              <polyline points="1 4 1 10 7 10"/>
              <path d="M3.51 15a9 9 0 1 0 .49-3.77"/>
            </svg>
            Reset
          </button>
        )}
      </header>

      {/* ── MAIN AREA ─────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>

        {/* ── SIDEBAR ──────────────────────────────────────────── */}
        <aside style={{
          width: 180, flexShrink: 0, background: '#0c3649',
          borderRight: '1px solid rgba(255,255,255,0.1)',
          padding: '14px 12px', display: 'flex', flexDirection: 'column', gap: 18,
          overflowY: 'auto',
        }}>
          {/* Stats */}
          {stats && (
            <div>
              <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
                Dataset
              </div>
              {([
                ['People',      stats.people_nodes],
                ['Departments', stats.total_nodes - stats.people_nodes - stats.ghost_nodes],
                ['Max Depth',   stats.max_depth],
              ] as [string, number][]).map(([label, val]) => (
                <div key={label} style={{
                  display: 'flex', justifyContent: 'space-between',
                  padding: '3px 0', borderBottom: '1px solid rgba(255,255,255,0.1)', fontSize: 11,
                }}>
                  <span style={{ color: 'rgba(255,255,255,0.6)' }}>{label}</span>
                  <span style={{ color: 'rgba(255,255,255,0.95)', fontWeight: 700 }}>{val}</span>
                </div>
              ))}
              {industry && (
                <div style={{
                  marginTop: 8, padding: '4px 6px',
                  background: 'rgba(255,255,255,0.1)', borderRadius: 4,
                  fontSize: 10, lineHeight: 1.4,
                }}>
                  <span style={{ color: 'rgba(255,255,255,0.4)', display: 'block', marginBottom: 2 }}>INDUSTRY</span>
                  <span style={{ color: 'rgba(255,255,255,0.9)', fontWeight: 600 }}>{industry}</span>
                </div>
              )}
            </div>
          )}

          {/* How to use */}
          <div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)', letterSpacing: 1, marginBottom: 8, textTransform: 'uppercase' }}>
              How to use
            </div>
            {[
              ['›', 'Click org to show departments'],
              ['›', 'Click department to view executives'],
              ['‹', 'Click department again to collapse'],
              ['⊙', 'Fit chart to screen'],
            ].map(([icon, tip]) => (
              <div key={tip} style={{ display: 'flex', gap: 6, padding: '3px 0', fontSize: 10, color: 'rgba(255,255,255,0.5)' }}>
                <span style={{ color: 'rgba(255,255,255,0.85)', width: 10, flexShrink: 0 }}>{icon}</span>
                <span>{tip}</span>
              </div>
            ))}
          </div>

          {/* ── History ─────────────────────────────────────────── */}
          {history.length > 0 && (
            <div>
              <div style={{
                fontSize: 10, color: 'rgba(255,255,255,0.4)', letterSpacing: 1,
                textTransform: 'uppercase', marginBottom: 8,
                display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              }}>
                History
                <button
                  onClick={() => { setHistory([]); persistHistory([]) }}
                  title="Clear all history"
                  style={{
                    background: 'none', border: 'none', cursor: 'pointer',
                    color: '#E63946', fontSize: 9, padding: 0,
                  }}
                >
                  Clear
                </button>
              </div>
              {history.map(entry => (
                <div
                  key={entry.id}
                  onClick={() => restoreSnapshot(entry)}
                  title={`Restore ${entry.companyName}`}
                  style={{
                    display: 'flex', alignItems: 'flex-start', gap: 4,
                    padding: '5px 4px', borderBottom: '1px solid rgba(255,255,255,0.08)',
                    cursor: 'pointer', borderRadius: 4,
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.08)')}
                  onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                >
                  {/* Chart icon */}
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
                    stroke="rgba(255,255,255,0.65)" strokeWidth="2" style={{ flexShrink: 0, marginTop: 1 }}>
                    <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
                    <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
                  </svg>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                      fontSize: 10, color: 'rgba(255,255,255,0.8)', fontWeight: 600,
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}>
                      {entry.companyName}
                    </div>
                    <div style={{ fontSize: 8.5, color: 'rgba(255,255,255,0.38)' }}>
                      {relativeTime(entry.timestamp)}
                      {entry.industry ? ` · ${entry.industry}` : ''}
                    </div>
                    {(() => {
                      const cache = entry.execCache ?? {}
                      const deptCount  = Object.keys(cache).length
                      const execCount  = Object.values(cache).reduce((s, arr) => s + arr.length, 0)
                      if (!deptCount) return null
                      return (
                        <div style={{ fontSize: 8, color: 'rgba(52,145,232,0.75)', marginTop: 1, display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span style={{
                            background: 'rgba(52,145,232,0.15)', borderRadius: 3,
                            padding: '1px 4px', border: '1px solid rgba(52,145,232,0.25)',
                          }}>
                            ✦ {execCount} execs · {deptCount} depts
                          </span>
                        </div>
                      )
                    })()}
                  </div>
                  <button
                    onClick={e => { e.stopPropagation(); deleteSnapshot(entry.id) }}
                    title="Remove from history"
                    style={{
                      background: 'none', border: 'none', cursor: 'pointer',
                      color: 'rgba(255,255,255,0.3)', fontSize: 12, padding: '0 2px',
                      lineHeight: 1, flexShrink: 0,
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

        </aside>

        {/* ── CHART AREA ───────────────────────────────────────── */}
        <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>

          {/* Status bar */}
          {(status === 'loading' || isBackendDown) && (
            <div style={{
              position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
              zIndex: 50,
              background: isBackendDown ? '#fff5f5' : '#f5f9fb',
              border: `1px solid ${isBackendDown ? '#E63946' : '#bad4dc'}`,
              borderRadius: 8, padding: '8px 16px', fontSize: 12,
              color: isBackendDown ? '#E63946' : '#627184',
              maxWidth: 620, textAlign: 'center',
              boxShadow: '0 4px 16px rgba(12,54,73,0.1)',
            }}>
              {status === 'loading' ? `⟳ ${statusMsg}` : `⚠ ${statusMsg}`}
            </div>
          )}

          {/* Expanding indicator */}
          {expandingId && (
            <div style={{
              position: 'absolute', top: 12, right: 60, zIndex: 50,
              background: '#f5f9fb', border: '1px solid #bad4dc',
              borderRadius: 8, padding: '6px 14px', fontSize: 11, color: '#0c3649',
            }}>
              ⟳ Loading…
            </div>
          )}

          {/* BOD/EM enrichment status notice */}
          {leadershipNote && status === 'ready' && (
            <div style={{
              position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
              zIndex: 49, background: '#0c2233', border: '1px solid rgba(52,145,232,0.4)',
              borderRadius: 8, padding: '8px 14px', fontSize: 10, color: '#7ec8f8',
              maxWidth: 560, display: 'flex', gap: 8, alignItems: 'center', whiteSpace: 'nowrap',
            }}>
              <span style={{ flexGrow: 1 }}>{leadershipNote}</span>
              <button
                onClick={() => setLeadershipNote('')}
                style={{ background: 'none', border: 'none', color: '#7ec8f8', cursor: 'pointer', fontSize: 14, padding: 0, lineHeight: 1 }}
              >×</button>
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
              background: 'rgba(12,54,73,0.06)', border: '2px dashed #0c3649',
              borderRadius: 8, margin: 12,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <div style={{ fontSize: 18, color: '#0c3649' }}>Drop CSV / JSON / Excel to load</div>
            </div>
          )}

          {/* Idle state */}
          {status === 'idle' && (
            <div style={{
              position: 'absolute', inset: 0,
              display: 'flex', flexDirection: 'column',
              alignItems: 'center', justifyContent: 'center', gap: 16,
            }}>
              <div style={{ fontSize: 48, opacity: 0.08, color: '#0c3649' }}>⬡</div>
              <div style={{ color: '#627184', fontSize: 14 }}>Drop a file or click "Demo"</div>
              {/* Company website input for BOD/EM enrichment */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                <input
                  type="text"
                  placeholder="Company website (optional) — e.g. morganstanley.com"
                  value={companyWebsite}
                  onChange={e => setCompanyWebsite(e.target.value)}
                  style={{
                    width: 340, padding: '6px 12px',
                    background: '#f5f9fb', border: '1px solid #dde8ed',
                    borderRadius: 6, color: '#00204d', fontSize: 12,
                    outline: 'none',
                  }}
                />
                <div style={{ fontSize: 11, color: '#bad4dc' }}>
                  Used to fetch Board of Directors &amp; Executive Management from the company website
                </div>
              </div>
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
              fontSize: 11, color: '#bad4dc', whiteSpace: 'nowrap', pointerEvents: 'none',
            }}>
              Click the org › to expand departments&nbsp;·&nbsp;
              Click a department › to view executives&nbsp;·&nbsp;
              Scroll / pinch to zoom
            </div>
          )}

          {/* ExecPanel — overlays right side of chart */}
          <ExecPanel
            deptNode={panelDept}
            executives={panelExecs}
            totalCount={panelTotal}
            onClose={() => { setPanelDept(null); setPanelExecs(null); setPanelTotal(0) }}
            apiBase={API}
            companyName={deptTree?.label ?? ''}
          />
        </div>
      </div>
    </div>
  )
}
