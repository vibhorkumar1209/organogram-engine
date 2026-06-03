import React, { useState } from 'react'
import { createPortal } from 'react-dom'
import type { OrgNode } from '../types'

export type ReportType =
  | 'no_longer_here'
  | 'wrong_dept'
  | 'wrong_hierarchy'
  | 'new_executive'
  | 'other'

const REPORT_TYPES: { value: ReportType; label: string; desc: string }[] = [
  { value: 'no_longer_here',   label: 'No longer at company',  desc: 'This person has left or retired' },
  { value: 'wrong_dept',       label: 'Wrong department',       desc: 'Person is in an incorrect department' },
  { value: 'wrong_hierarchy',  label: 'Wrong reporting line',   desc: 'Reporting hierarchy is incorrect' },
  { value: 'new_executive',    label: 'Add new executive',      desc: 'Someone is missing from the chart' },
  { value: 'other',            label: 'Other correction',       desc: 'Any other inaccuracy' },
]

interface Props {
  person?:     OrgNode         // pre-filled when flagging an existing person
  deptName:    string
  companyName: string
  apiBase:     string
  onClose:     () => void
}

export const FeedbackModal: React.FC<Props> = ({
  person, deptName, companyName, apiBase, onClose,
}) => {
  const defaultType: ReportType = person ? 'no_longer_here' : 'new_executive'
  const [type,       setType]       = useState<ReportType>(defaultType)
  const [linkedIn,   setLinkedIn]   = useState('')
  const [notes,      setNotes]      = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted,  setSubmitted]  = useState(false)
  const [error,      setError]      = useState('')

  const handleSubmit = async () => {
    if (type === 'new_executive' && !linkedIn.trim()) {
      setError('LinkedIn URL is required for new executives.')
      return
    }
    setError('')
    setSubmitting(true)
    try {
      const res = await fetch(`${apiBase}/report-change`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type,
          companyName,
          personName:  person?.label ?? undefined,
          personTitle: person?.metadata?.designation
            ? String(person.metadata.designation)
            : undefined,
          currentDept: deptName || undefined,
          linkedInUrl: linkedIn.trim() || undefined,
          notes:       notes.trim()    || undefined,
          timestamp:   Date.now(),
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      setSubmitted(true)
      setTimeout(onClose, 1800)
    } catch (e: any) {
      setError(e?.message ?? 'Submission failed — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  // ── Styles ────────────────────────────────────────────────────────────
  const overlay: React.CSSProperties = {
    position: 'fixed', inset: 0, zIndex: 2000,
    background: 'rgba(8,15,22,0.72)', backdropFilter: 'blur(4px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 16,
  }
  const card: React.CSSProperties = {
    background: '#0c1e2a', border: '1px solid #1e3a4a',
    borderRadius: 12, padding: '24px 24px 20px',
    width: '100%', maxWidth: 420,
    boxShadow: '0 24px 64px rgba(0,0,0,0.6)',
    display: 'flex', flexDirection: 'column', gap: 16,
  }
  const label: React.CSSProperties = {
    fontSize: 10, color: 'rgba(255,255,255,0.45)',
    letterSpacing: 1, textTransform: 'uppercase', marginBottom: 5,
    display: 'block',
  }
  const inputStyle: React.CSSProperties = {
    width: '100%', boxSizing: 'border-box',
    background: '#071219', border: '1px solid #1e3a4a',
    borderRadius: 6, padding: '8px 10px',
    color: 'rgba(255,255,255,0.85)', fontSize: 12,
    outline: 'none',
  }

  if (submitted) {
    return createPortal(
      <div style={overlay} onClick={onClose}>
        <div style={{ ...card, alignItems: 'center', gap: 12, padding: 32 }}>
          <div style={{ fontSize: 32 }}>✓</div>
          <div style={{ fontSize: 14, fontWeight: 700, color: '#52d08c' }}>
            Report submitted
          </div>
          <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.45)', textAlign: 'center' }}>
            Thank you — our team will review this correction.
          </div>
        </div>
      </div>,
      document.body
    )
  }

  return createPortal(
    <div style={overlay} onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div style={card}>

        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#fff', marginBottom: 2 }}>
              Report a Change
            </div>
            <div style={{ fontSize: 10, color: 'rgba(255,255,255,0.38)' }}>
              {companyName}{deptName ? ` · ${deptName}` : ''}
            </div>
          </div>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: 'rgba(255,255,255,0.4)',
            fontSize: 18, cursor: 'pointer', lineHeight: 1, padding: 0,
          }}>×</button>
        </div>

        {/* Person context (if flagging existing) */}
        {person && (
          <div style={{
            background: 'rgba(52,145,232,0.08)', border: '1px solid rgba(52,145,232,0.2)',
            borderRadius: 6, padding: '8px 12px',
            fontSize: 11, color: 'rgba(255,255,255,0.7)',
          }}>
            <span style={{ fontWeight: 700, color: '#fff' }}>{person.label}</span>
            {person.metadata?.designation && (
              <span style={{ color: 'rgba(255,255,255,0.45)' }}>
                {' · '}{String(person.metadata.designation).slice(0, 50)}
              </span>
            )}
          </div>
        )}

        {/* Change type */}
        <div>
          <span style={label}>Change type</span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {REPORT_TYPES.map(t => (
              <label key={t.value} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '7px 10px', borderRadius: 6, cursor: 'pointer',
                background: type === t.value ? 'rgba(52,145,232,0.12)' : 'rgba(255,255,255,0.03)',
                border: `1px solid ${type === t.value ? 'rgba(52,145,232,0.4)' : 'rgba(255,255,255,0.07)'}`,
                transition: 'all 0.12s',
              }}>
                <input
                  type="radio" value={t.value}
                  checked={type === t.value}
                  onChange={() => setType(t.value)}
                  style={{ accentColor: '#3491E8', flexShrink: 0 }}
                />
                <div>
                  <div style={{ fontSize: 11, fontWeight: 600, color: type === t.value ? '#7ec8f8' : 'rgba(255,255,255,0.75)' }}>
                    {t.label}
                  </div>
                  <div style={{ fontSize: 9, color: 'rgba(255,255,255,0.35)', marginTop: 1 }}>
                    {t.desc}
                  </div>
                </div>
              </label>
            ))}
          </div>
        </div>

        {/* LinkedIn URL — required for new executives */}
        {(type === 'new_executive' || type === 'wrong_hierarchy') && (
          <div>
            <span style={label}>
              LinkedIn URL{type === 'new_executive' ? ' *' : ' (optional)'}
            </span>
            <input
              style={inputStyle}
              type="url"
              placeholder="https://www.linkedin.com/in/..."
              value={linkedIn}
              onChange={e => setLinkedIn(e.target.value)}
            />
          </div>
        )}

        {/* Notes */}
        <div>
          <span style={label}>Notes</span>
          <textarea
            style={{ ...inputStyle, resize: 'vertical', minHeight: 72 }}
            placeholder={
              type === 'no_longer_here'  ? 'When did they leave? (optional)' :
              type === 'wrong_dept'      ? 'Which department should they be in?' :
              type === 'wrong_hierarchy' ? 'Who do they actually report to?' :
              type === 'new_executive'   ? 'Name, title, and any other details…' :
              'Describe the correction…'
            }
            value={notes}
            onChange={e => setNotes(e.target.value)}
          />
        </div>

        {/* Error */}
        {error && (
          <div style={{ fontSize: 11, color: '#E63946', background: 'rgba(230,57,70,0.08)',
            border: '1px solid rgba(230,57,70,0.2)', borderRadius: 5, padding: '6px 10px' }}>
            {error}
          </div>
        )}

        {/* Actions */}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onClose} style={{
            background: 'none', border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: 6, padding: '7px 16px',
            color: 'rgba(255,255,255,0.55)', fontSize: 11, cursor: 'pointer',
          }}>
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            style={{
              background: submitting ? '#1e3a4a' : '#3491E8',
              border: 'none', borderRadius: 6, padding: '7px 18px',
              color: '#fff', fontSize: 11, fontWeight: 700,
              cursor: submitting ? 'not-allowed' : 'pointer',
              opacity: submitting ? 0.7 : 1,
              transition: 'background 0.15s',
            }}
          >
            {submitting ? 'Submitting…' : 'Submit Report'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  )
}
