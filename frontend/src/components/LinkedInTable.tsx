import React, { useState } from 'react'
import { createPortal } from 'react-dom'

export interface LinkedInRow {
  'Full Name': string
  'Expected Title/Role': string
  'Company': string
  'LinkedIn Profile URL': string
  'Confidence Level': string
  'Matched Name and Title on LinkedIn': string
  Panel: string
  Department: string
  node_id: string
}

export interface LinkedInReport {
  rows: LinkedInRow[]
  columns: string[]
  markdown: string
  linkedin_done: boolean
  summary: { searched?: number; found?: number; not_found?: number; provided?: number }
}

interface Props {
  report:      LinkedInReport | null   // null = loading
  companyName: string
  onRefresh:   () => void
  onClose:     () => void
}

// Confidence colours, readable on the dark card.
export const CONFIDENCE_STYLE: Record<string, { fg: string; bg: string; border: string }> = {
  High:       { fg: '#52d08c', bg: 'rgba(82,208,140,0.10)', border: 'rgba(82,208,140,0.35)' },
  Medium:     { fg: '#f5b642', bg: 'rgba(245,182,66,0.10)', border: 'rgba(245,182,66,0.35)' },
  Low:        { fg: '#f08a5d', bg: 'rgba(240,138,93,0.10)', border: 'rgba(240,138,93,0.35)' },
  Provided:   { fg: '#7cc4ff', bg: 'rgba(124,196,255,0.10)', border: 'rgba(124,196,255,0.35)' },
  'Not Found':{ fg: 'rgba(255,255,255,0.45)', bg: 'transparent', border: 'rgba(255,255,255,0.15)' },
}

function downloadCsv(rows: LinkedInRow[], columns: string[], companyName: string) {
  const cols = [...columns, 'Panel', 'Department']
  const lines = [
    cols.join(','),
    ...rows.map(r => cols.map(c => `"${String((r as any)[c] ?? '').replace(/"/g, '""')}"`).join(',')),
  ]
  const blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(companyName || 'company').replace(/\s+/g, '_')}_leadership_linkedin.csv`
  a.click()
  URL.revokeObjectURL(url)
}

export const LinkedInTable: React.FC<Props> = ({ report, companyName, onRefresh, onClose }) => {
  const [copied, setCopied] = useState(false)
  const [filter, setFilter] = useState<string>('All')

  const rows = report?.rows ?? []
  const shown = filter === 'All' ? rows : rows.filter(r => r['Confidence Level'] === filter)
  const count = (lvl: string) => rows.filter(r => r['Confidence Level'] === lvl).length

  const copyMarkdown = async () => {
    if (!report) return
    try {
      await navigator.clipboard.writeText(report.markdown)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch { /* clipboard blocked — the CSV is the fallback */ }
  }

  const btn: React.CSSProperties = {
    background: 'none', border: '1px solid #1e3a4a', borderRadius: 6,
    padding: '5px 10px', color: 'rgba(255,255,255,0.75)', fontSize: 11,
    cursor: 'pointer', whiteSpace: 'nowrap',
  }
  const th: React.CSSProperties = {
    textAlign: 'left', padding: '8px 10px', fontSize: 9.5, fontWeight: 600,
    color: 'rgba(255,255,255,0.45)', letterSpacing: 0.6, textTransform: 'uppercase',
    borderBottom: '1px solid #1e3a4a', position: 'sticky', top: 0, background: '#0c1e2a',
    whiteSpace: 'nowrap',
  }
  const td: React.CSSProperties = {
    padding: '8px 10px', fontSize: 11.5, color: 'rgba(255,255,255,0.85)',
    borderBottom: '1px solid rgba(30,58,74,0.6)', verticalAlign: 'top',
  }

  return createPortal(
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(8,15,22,0.72)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16,
      }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div style={{
        background: '#0c1e2a', border: '1px solid #1e3a4a', borderRadius: 12,
        width: '100%', maxWidth: 1180, maxHeight: '88vh',
        boxShadow: '0 24px 64px rgba(0,0,0,0.6)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {/* Header */}
        <div style={{ padding: '18px 20px 12px', display: 'flex', gap: 12,
                      alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#fff' }}>
              LinkedIn profiles — Board &amp; Executive Management
            </div>
            <div style={{ fontSize: 10.5, color: 'rgba(255,255,255,0.45)', marginTop: 3 }}>
              {companyName}
              {report && ` · ${rows.length - count('Not Found')} of ${rows.length} matched`}
              {report && !report.linkedin_done && ' · search still running'}
            </div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.38)', marginTop: 6, lineHeight: 1.5 }}>
              Each profile is checked against the person's name, the company and a leadership title.
              When that can't be confirmed the answer is “Not Found”, never a guess.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            <button style={btn} onClick={onRefresh} title="Re-read the latest results">↻ Refresh</button>
            <button style={btn} onClick={copyMarkdown} disabled={!report}>
              {copied ? '✓ Copied' : 'Copy as Markdown'}
            </button>
            <button style={btn} disabled={!report}
                    onClick={() => report && downloadCsv(rows, report.columns, companyName)}>
              Download CSV
            </button>
            <button onClick={onClose} title="Close" style={{
              background: 'none', border: 'none', color: 'rgba(255,255,255,0.5)',
              fontSize: 20, cursor: 'pointer', padding: '0 4px', lineHeight: 1,
            }}>×</button>
          </div>
        </div>

        {/* Confidence filter */}
        {report && rows.length > 0 && (
          <div style={{ padding: '0 20px 10px', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {['All', 'High', 'Medium', 'Low', 'Provided', 'Not Found'].map(lvl => {
              const n = lvl === 'All' ? rows.length : count(lvl)
              if (lvl !== 'All' && n === 0) return null
              const st = CONFIDENCE_STYLE[lvl]
              const active = filter === lvl
              return (
                <button key={lvl} onClick={() => setFilter(lvl)} style={{
                  ...btn, padding: '3px 9px', fontSize: 10.5,
                  color: st?.fg ?? 'rgba(255,255,255,0.8)',
                  background: active ? (st?.bg || 'rgba(255,255,255,0.08)') : 'transparent',
                  border: `1px solid ${active ? (st?.border ?? '#3d6880') : '#1e3a4a'}`,
                }}>
                  {lvl} · {n}
                </button>
              )
            })}
          </div>
        )}

        {/* Table */}
        <div style={{ overflow: 'auto', flex: 1, borderTop: '1px solid #1e3a4a' }}>
          {!report ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'rgba(255,255,255,0.45)', fontSize: 12 }}>
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div style={{ padding: 32, textAlign: 'center', color: 'rgba(255,255,255,0.45)', fontSize: 12 }}>
              No Board or Executive Management members in this chart yet.
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 860 }}>
              <thead>
                <tr>
                  <th style={th}>Full Name</th>
                  <th style={th}>Expected Title / Role</th>
                  <th style={th}>Company</th>
                  <th style={th}>LinkedIn Profile URL</th>
                  <th style={th}>Confidence</th>
                  <th style={th}>Matched Name &amp; Title on LinkedIn</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(r => {
                  const conf = r['Confidence Level'] || 'Not Found'
                  const st = CONFIDENCE_STYLE[conf] ?? CONFIDENCE_STYLE['Not Found']
                  const url = r['LinkedIn Profile URL']
                  const hasUrl = url && url !== 'Not Found'
                  return (
                    <tr key={r.node_id}>
                      <td style={{ ...td, fontWeight: 600, color: '#fff' }}>
                        {r['Full Name']}
                        <div style={{ fontSize: 9.5, color: 'rgba(255,255,255,0.38)', fontWeight: 400, marginTop: 2 }}>
                          {r.Panel}{r.Department ? ` · leads ${r.Department}` : ''}
                        </div>
                      </td>
                      <td style={td}>{r['Expected Title/Role']}</td>
                      <td style={td}>{r.Company}</td>
                      <td style={{ ...td, wordBreak: 'break-all' }}>
                        {hasUrl
                          ? <a href={url} target="_blank" rel="noreferrer" style={{ color: '#7cc4ff' }}>
                              {url.replace(/^https:\/\/www\./, '')}
                            </a>
                          : <span style={{ color: 'rgba(255,255,255,0.4)' }}>Not Found</span>}
                      </td>
                      <td style={td}>
                        <span style={{
                          fontSize: 10, fontWeight: 600, color: st.fg, background: st.bg,
                          border: `1px solid ${st.border}`, borderRadius: 4, padding: '2px 7px',
                          whiteSpace: 'nowrap',
                        }}>
                          {conf}
                        </span>
                      </td>
                      <td style={{ ...td, color: 'rgba(255,255,255,0.7)' }}>
                        {r['Matched Name and Title on LinkedIn'] || '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
