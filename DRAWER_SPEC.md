# Org-Chart Side Drawer — Rebuild Specification

The side drawer is the panel that slides in from the right edge of the chart area when a
**department node** on the org chart is clicked. In the code it is the `ExecPanel`
component. This document describes what the current code does: first verified against
the source at commit `decd940` and the deployed app
(`organogram-engine.vercel.app` → `organogram-engine.onrender.com`) on 3 Nov 2026, then
updated for the drawer fixes that followed. [§18](#18-quirks-and-as-is-bugs) lists what
changed and what remains.

Line references are `path:start-end` against that commit. Where the code's
behaviour is a bug or an oddity, it is still documented as-is and also listed under
[§18 Quirks](#18-quirks-and-as-is-bugs). Anything not derivable from the code is marked
**unclear from code**.

---

## 1. Inventory

### 1.1 Files

| File | Role in the drawer |
|---|---|
| `frontend/src/components/ExecPanel.tsx` (1200 lines) | The drawer: container, header, legend/toolbar, region groups, the people tree, person rows, committee rows, footer, CSV export, LinkedIn badge, and the mount point for the report dialog. |
| `frontend/src/components/FeedbackModal.tsx` (253 lines) | "Report a Change" dialog opened from the drawer (per-person flag ⚑, or "Report missing executive"). |
| `frontend/src/components/LinkedInTable.tsx` (226 lines) | "LinkedIn profiles — Board & Executive Management" dialog opened by the drawer's **in LinkedIn** button. Rendered by `App.tsx`. |
| `frontend/src/App.tsx` (2129 lines) | Owns all drawer state, opens the drawer on a department click, fetches its people, caches them, refreshes them after LinkedIn matching, closes it, and renders the add-people dialog, the Paste-JSON dialog and the hidden file input that the drawer's **Add people** buttons lead to. |
| `frontend/src/components/OrgChart.tsx` (396 lines) | D3 chart; its node `click` handler calls `onNodeClick` → `App.handleNodeClick`, which opens the drawer. |
| `frontend/src/types/index.ts` | `OrgNode` type used for the department node and every person. |
| `frontend/src/index.css` | Global body font (monospace), scrollbar styling and `box-sizing`, all of which the drawer inherits. |
| `frontend/index.html`, `vite.config.ts`, `vercel.json` | `VITE_API_URL` / `/api` proxy that `apiBase` comes from. |
| `backend/api_server.py` | Endpoints the drawer and its dialogs call: `GET /executives`, `GET /linkedin-report`, `POST /report-change`, `GET /selectable`, `POST /ingest`, `POST /ingest-json`, `POST /load-demo`, `GET /tree`, `GET /leadership-ready`. |
| `backend/structural_engine.py` | Produces the person metadata the drawer reads (`is_apex`, `leads_department(_id)`, `board_sub_role`, `committees`, `layer`, `region`, …). |

### 1.2 Components (all React function components)

| Component | File:lines | Notes |
|---|---|---|
| `ExecPanel` | `ExecPanel.tsx:717-1319` | Exported. The drawer. |
| `PersonRow` | `ExecPanel.tsx:371-667` | Recursive row: a person, or a committee heading (Board). |
| `LinkedInBadge` | `ExecPanel.tsx:326-353` | The `in ↗ High` link / `in · not found` text on each row. |
| `FeedbackModal` | `FeedbackModal.tsx:30-260` | Portal to `document.body`. |
| `LinkedInTable` | `LinkedInTable.tsx:57-233` | Portal to `document.body`. |
| Add-people dialog | `App.tsx:1863-2018` (inline JSX, no component) | Rendered inside the chart area, not a portal. |
| Paste-JSON dialog | `App.tsx:2060-2133` (inline JSX) | Portal. Reached from the add-people dialog. |

### 1.3 Helpers, constants and types in `ExecPanel.tsx`

| Name | Lines | Purpose |
|---|---|---|
| `GroupMode` type | 5 | `'region' \| 'country'`. |
| `Props` | 7-27 | Drawer props. |
| `LAYER_LABELS` | 30-42 | Seniority names for layers 0–10. |
| `BOD_LAYER_LABELS` | 46-50 | Board names for layers 0–2. |
| `getBoardRoleLabel` | 53-67 | `board_sub_role` → badge text. |
| `SECTOR_COLORS` | 69-77 | Sector → accent colour. |
| `nodeColor` | 79-82 | A person's accent colour. |
| `ExecTreeNode` | 93-98 | Tree node (`person`, `reports`, `isLast`, `isSkippedLevel`). |
| `isCeoTitle` | 102-133 | Fallback apex detector for Executive Management. |
| `buildLayerTree` | 137-183 | Generic layer-by-layer tree. |
| `Committee` type, `committeesOf` | 193-200 | Reads `metadata.committees`. |
| `committeeRank` | 204-210 | Committee ordering. |
| `leaf` | 212 | Makes a childless tree node. |
| `buildBODTree` | 221-268 | Board governance tree. |
| `buildExecTree` | 274-316 | Chooses the tree builder. |
| `LI_CONFIDENCE` | 319-324 | LinkedIn badge colours and tooltips. |
| `toggleKeys` | 361-369 | Enter/Space toggling and `role`/`tabIndex` for collapsible rows and headers. |
| `INFERRED_REPORT_NOTE` | 356-358 | Tooltip copy. |
| `downloadExecsCSV` | 670-714 | Client-side CSV download (the **Save** button). |
| `GEO_PATTERNS`, `inferRegion` | 801-847 | Region grouping. Defined inside the component. |
| `COUNTRY_PATTERNS`, `inferCountry` | 852-925 | Country grouping. Defined inside the component. |

### 1.4 State

**Inside `ExecPanel`** (lost when `ExecPanel` unmounts, which happens when the user switches to the History page). Collapse state is reset whenever a different department opens; `groupMode` is kept:

| State | Lines | Meaning |
|---|---|---|
| `reportPerson: OrgNode \| null` | 735 | Which person the report dialog is for. A sentinel `{node_id:'__new__'}` means "missing executive". `null` = dialog closed. |
| `collapsed: Set<string>` | 738-744 | `node_id`s of collapsed tree nodes, including committee headers (`committee__<name>`). |
| `collapsedRegions: Set<string>` | 747-753 | Collapsed region/country group names. |
| `groupMode: GroupMode` | 795 | `'region'` by default. Kept across departments. |
| `saving: boolean` | 736 | **Save** is fetching the full list. |
| `last` (ref) | 725-731 | Last department, people, total and error shown, so the drawer keeps its content while sliding out. |
| `regionGroups` (memo) | 930-951 | Derived groups. |
| `hasInferredLines` (memo) | 968-978 | Whether any dashed (inferred) line exists. |

**In `App.tsx`** (the drawer is fully controlled from here):

| State / ref | Lines | Meaning |
|---|---|---|
| `panelDept: OrgNode \| null` | 367 | The open department. `null` = drawer closed. |
| `panelExecs: OrgNode[] \| null` | 368 | People shown. `null` = loading. |
| `panelTotal: number` | 370 | Total people in that department (server `total`). |
| `panelError: string \| null` | 372 | Why the people could not be loaded. Shown as the drawer's error state. |
| `linkedInError: string \| null` | 379 | Why the LinkedIn report could not be loaded. |
| `escState` (ref) | 850, set at 1152 | Which layers are open, for the single Escape handler. |
| `panelDeptRef` | 374-375 | Mirror of `panelDept` for the long-lived poll. |
| `linkedInReport: LinkedInReport \| 'loading' \| null` | 378 | LinkedIn dialog. |
| `selectOpen`, `selectable`, `selectedIds`, `ingestTarget`, `ingestUrl`, `ingestBusy`, `ingestNote` | 390-397 | Add-people dialog. |
| `jsonDialog`, `jsonText`, `jsonError` | 1148-1150 | Paste-JSON dialog. |
| `history` (+ `historyRef`, `activeEntryIdRef`) | 400-404 | Browser history entries, each with an `execCache` of drawer contents. |
| `activeJobIdRef` | 412 | Backend `job_id` appended to every call. |

**Shared with the drawer:** the History sidebar (`App.tsx:1644-1658`) reads each entry's `execCache`, the cache that drawer fetches fill, and shows `✦ {total people} execs · {departments cached} depts` under the company name. Hidden when the cache is empty.

### 1.5 API calls

| Call | Made by | When |
|---|---|---|
| `GET {API}/executives?dept_id=<id>&limit=200&job_id=<job>` | `App.loadPanelPeople` (`App.tsx:774-842`), `refreshOpenPanel` (`428-445`), `prefetchAllExecutives` | Open drawer and **Try again**; refresh after LinkedIn matching and after an ingest; background pre-fetch of every department. |
| `GET {API}/executives?dept_id=<id>&offset=<n>&limit=5000` (paged) | `App.loadAllPanelPeople` (`App.tsx:467-482`) | **Save** when the department has more people than the 200 loaded. |
| `POST {API}/load-demo` | `loadPanelPeople` (`App.tsx:774-842`) | Only when `/executives` returns `loaded:false` for a demo chart. Then it retries once. |
| `GET {API}/linkedin-report?job_id=…` | `openLinkedInReport` (`App.tsx:448-464`) | Drawer **in LinkedIn** button; dialog **↻ Refresh**. |
| `POST {API}/report-change` | `FeedbackModal.handleSubmit` | Report dialog **Submit Report**. Sends `jobId`, `nodeId` (the person) and `deptId`. |
| `GET {API}/selectable?job_id=…` | `openSelection` (`App.tsx:1068-1084`) | Drawer **Add people** (header or row). |
| `POST {API}/ingest?job_id=…&scope=…` (multipart `file`) | `ingestFile` (`App.tsx:1130-1135`) | Add-people → **Upload CSV / Excel / JSON**. |
| `POST {API}/ingest?job_id=…&scope=…&source_url=…` | `ingestFromUrl` (`App.tsx:1137-1142`) | Add-people → **Fetch**. |
| `POST {API}/ingest-json?job_id=…&scope=…` | `handlePasteJson` (`App.tsx:1154-1175`) | Paste-JSON → **Add to chart**. |
| `GET {API}/tree?root=root_global&max_depth=3&dept_only=true` | `loadDeptStructure` | After an ingest, to rebuild the chart. `refreshOpenPanel()` then re-reads the open drawer. |
| `GET {API}/leadership-ready` (poll, 10 s) | `runIngest` poll (`App.tsx:956-1035`) | After a new chart. When LinkedIn matching finishes, it clears caches and calls `refreshOpenPanel()`. |

`{API}` = `(import.meta.env.VITE_API_URL || '/api').trim()` (`App.tsx:9`). It is passed to the drawer as `apiBase`.

### 1.6 External libraries

The drawer itself uses only React (`useState`, `useMemo`, `useCallback`) and
`react-dom`'s `createPortal` for its dialogs. There is no icon library: every icon is a Unicode
glyph or an inline SVG. There is no CSS framework in use: Tailwind is installed, but the drawer uses only inline
`style` objects.

---

## 2. Lifecycle and data flow

### 2.1 Opening

1. A user clicks a node in the D3 chart. `OrgChart.tsx:210-213`:
   ```ts
   .on('click', (e, d) => {
     e.stopPropagation()
     if (!d.data.is_synthetic) onNodeClick(d.data)
   })
   ```
2. `App.handleNodeClick` (`App.tsx:865-907`) filters:
   - `is_synthetic` → ignored (`867`).
   - `node_type === 'person'` → ignored (`869`). Person nodes are never shown on the chart anyway: `filterToDeptNodes` (`App.tsx:99-109`) strips them.
   - `node_type === 'global'` → ignored (`871`).
   - Only `dept_primary`, `dept_secondary` and `dept_tertiary` continue (`873-875`). A `ghost` node does nothing, and ghosts are also stripped from the chart.
3. **The same click also toggles the department's branch in the chart** (`877-898`): collapse if it is expanded with real children, or lazily fetch `GET /tree?root=<id>&max_depth=2&dept_only=true` if `has_more`. The drawer opens either way ("Always open ExecPanel … never change this", `901`).
4. `setPanelDept(node)`, and `panelDeptRef.current = node` set at once so no earlier response can be mistaken for this department. The drawer slides in with the department name. Then `loadPanelPeople(node)`.
5. `loadPanelPeople(node, useCache = true)` clears `panelTotal` and `panelError`, then does a **cache check**: if the active history entry's `execCache[node.node_id]` exists, it is shown instantly (`setPanelExecs(cached); setPanelTotal(cached.length)`). **Try again** calls it with `useCache = false`.
6. Otherwise `setPanelExecs(null)` → **Loading** state, then `GET /executives?dept_id=…&limit=200`:
   - OK and not `loaded:false` → map each record with `toPersonNode` (`App.tsx:139-162`), filter out records without `node_id`, and write them to the entry's `execCache`. Then, **only if this department is still the open one** (`panelDeptRef.current.node_id === deptId`), `setPanelExecs(people)` and `setPanelTotal(data.total ?? people.length)`.
   - `loaded:false` (the backend lost the job) and the entry's `source === 'demo'` → `POST /load-demo`, adopt the new `job_id`, retry once.
   - `loaded:false` for an upload, or again after the retry → **Error** state: "This chart is no longer on the server — create it again."
   - HTTP 404 → **Error** state: "This department is no longer in the chart — reload the chart."
   - Other HTTP error → "Server error {status}."
   - Network error → "Connection failed — the backend may be restarting."
   - Errors, like successes, are applied only if the department is still open.

```ts
// App.tsx — loadPanelPeople (abridged)
const stillOpen = () => panelDeptRef.current?.node_id === deptId
const fail = (msg: string) => { if (!stillOpen()) return; setPanelExecs([]); setPanelError(msg) }
const fetchAndSet = (retryAfterReload = false): Promise<void> =>
  apiFetch(`/executives?dept_id=${encodeURIComponent(deptId)}&limit=200`)
    .then(r => {
      if (r.status === 404) throw new Error('This department is no longer in the chart — reload the chart.')
      if (!r.ok) throw new Error(`Server error ${r.status}.`)
      return r.json()
    })
    .then(async (data) => {
      if (data.loaded === false && !retryAfterReload) {
        if (src === 'demo') { /* POST /load-demo, adopt job_id */ return fetchAndSet(true) }
        fail('This chart is no longer on the server — create it again.'); return
      }
      const people = data.executives.filter(p => p.node_id).map(p => toPersonNode(p, nodeColor))
      if (entryId) updateExecCache(entryId, deptId, people)
      if (!stillOpen()) return
      setPanelExecs(people)
      setPanelTotal(data.total ?? people.length)
    })
    .catch(e => fail(/fetch/i.test(e.message) ? 'Connection failed — the backend may be restarting.' : e.message))
```

### 2.2 Which people a department returns (backend)

`GET /executives` (`api_server.py:2195-2245`) walks the department's subtree and collects
every `person`. It traverses through ghost and sub-department nodes, but **stops at any
child `dept_primary`**:

```py
# api_server.py:2218-2232
if node_type == "person":
    people.append(attrs)
# Stop at child dept_primary boundaries — prevents BOD from including
# Executive Management people (EM sits under BOD in the DAG).
if depth > 0 and node_type == "dept_primary":
    return
```

Consequences:
- **Board of Management** excludes Executive Management (a child `dept_primary`).
- **Executive Management** excludes the departments its executives run. Those are created as `dept_primary` (`structural_engine.py:2848`).
- A `dept_primary` such as *Finance & Accounting* **includes** people in its `dept_secondary` sub-teams. In the demo, Vivian Chen appears under both *Finance & Accounting* and *FP&A*.

Results are sorted by `(layer, label)` ascending. Missing layer sorts as 99. The response is paged with `offset` (default 0) and `limit` (default 200, max 5000). The drawer always asks for `limit=200` and never asks for a second page.

### 2.3 Background pre-fetch and the browser cache

After any chart loads, `prefetchAllExecutives` (`App.tsx:600-633`) fetches `/executives` for
**every** `dept_primary`/`dept_secondary`/`dept_tertiary`, 4 at a time, into the history
entry's `execCache`. It is skipped when `stats.people_nodes > 10_000`. The cache is persisted to
`localStorage['orgchart_history']` (max 12 entries, `App.tsx:12-13`, `78-80`).
Consequently most drawer opens are served from the cache, with no Loading state and
`panelTotal = cached.length`, so the "Showing top N of M" note never appears for a cached
department.

### 2.4 Live refresh

When the leadership poll sees `linkedin_done` (`App.tsx:1019-1033`), it calls
`clearExecCache(activeEntry)` and then `refreshOpenPanel()` (`App.tsx:428-445`). That function re-fetches the open
department **without** showing the Loading state, and drops the result if the user has since
switched to another department:

```ts
// App.tsx:431
if (!d || d.loaded === false || panelDeptRef.current?.node_id !== dept.node_id) return
```

This is how LinkedIn badges appear in a drawer that was opened before matching finished.

### 2.5 Closing

The drawer closes in these cases:
- The header **×** button → `closePanel()` (clears department, people, total and error).
- **Escape**, when no layer above it is open. A single `keydown` listener in `App` closes the topmost open layer: the Paste-JSON dialog, then the LinkedIn table, then the add-people dialog (unless an ingest is running), then the drawer. The drawer's own report dialog claims Escape first through a capture-phase listener in `ExecPanel`, so one Escape closes the report dialog and a second closes the drawer.
- Programmatically, whenever a new chart loads or the chart resets: `loadDemo` (`671`), `restoreSnapshot` (`524`), `viewHistoryRun` (`751`), `runIngest` (`916`), `handleReset` (`642-644`).

The following do **not** close it: a click on the chart background (that only hides the hover tooltip, `OrgChart.tsx:377`), and opening the add-people, LinkedIn or report dialogs.

When it closes, the component stays mounted and slides out (`transform: translateX(100%)`). During the 0.22 s slide-out it keeps showing the last department, people, total and error (held in a ref), so the content does not blank out.

---

## 3. Container and layout

```ts
// ExecPanel.tsx:890-900
position: 'absolute', top: 0, right: 0, bottom: 0,
width: 360, zIndex: 200,
background: '#ffffff',
borderLeft: `1px solid ${color}63`,
display: 'flex', flexDirection: 'column',
boxShadow: '-12px 0 40px rgba(12,54,73,0.12)',
transform: isOpen ? 'translateX(0)' : 'translateX(100%)',
transition: 'transform 0.22s ease-out',
pointerEvents: isOpen ? 'auto' : 'none',
```

- **Parent:** the chart area `<div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>` (`App.tsx:1691`). The drawer is a sibling of the SVG chart, the hint bar and the add-people dialog. The parent's `overflow:hidden` clips the closed drawer.
- **Width:** a fixed 360 px. There are no breakpoints, no resizing and no mobile layout.
- **It overlays the chart** rather than pushing it, and covers the chart's zoom buttons (bottom-right, `OrgChart.tsx:380-387`) while open.
- **Vertical sections:** header (fixed), legend/toolbar (fixed, conditional), body (`flex:1; overflowY:auto; overflowX:hidden`, `1057`), footer (fixed, conditional).
- **Font:** inherited from `body`: `'SF Mono', 'Fira Code', 'Cascadia Code', monospace` (`index.css:7-13`), so the whole drawer is monospace. The scrollbar is 6 px with track `#f5f9fb` and thumb `#bad4dc`, radius 3 (`index.css:16-18`).
- **Accent colour `color`** (`ExecPanel.tsx:717`):
  ```ts
  // `||`, not `??`: departments made for executives carry sector "" and color "".
  const color = deptNode ? (SECTOR_COLORS[deptNode.sector] || deptNode.color || '#3491E8') : '#3491E8'
  ```
  It is used for the title, the button borders, the active toggle, tree lines and group headers. Many styles append a 2-digit alpha (`color + '55'`), so a 6-digit hex value is assumed. Empty strings fall back to the next option, so a department with `sector:""` and `color:""` renders in `#3491E8`.

---

## 4. Variants

The drawer has one layout. Which variant you get depends on the department and the data.

| Variant | Condition | Differences |
|---|---|---|
| **Board** | `deptNode.node_id === 'dept__board_of_management'` | Eyebrow "Board of Directors". `BOD_LAYER_LABELS` in the summary. `buildBODTree` for every group (committee headings). Board region rule ("Global HQ" default). No header **Add people** (App passes `onAddPeople` only when `node_id !== 'dept__board_of_management'`). **in LinkedIn** shown. CSV uses Board seniority labels. |
| **Executive Management** | `node_id === 'dept__executive_management'` (`791`) | The primary group uses the CEO-apex tree. Rows show **▸ Department** and **+ Add people** (row-level `onAddPeople` is passed only when `isEM`, `1276`). Header **Add people** and **in LinkedIn** shown. |
| **Any other department** | Every other `dept_*` node | Layer tree. Header **Add people**. No **in LinkedIn**. No row-level add buttons, even if a person somehow has `leads_department`. |

Both the drawer and `App` identify the Board and Executive Management by **`node_id`**
(`dept__board_of_management`, `dept__executive_management`), never by label.

### 4.1 States

| State | Condition | Shows |
|---|---|---|
| Closed | `deptNode === null` | Off-screen; `pointer-events:none`. |
| Loading | `executives === null` | Header: eyebrow + title + **×**, plus **Add people** if allowed. No summary, Save, LinkedIn, legend or footer. Body: `⟳ Loading…` centred in a 120 px box, 12 px, `#627184` (`1152-1159`). |
| Error | `executives.length === 0` and `error` set | Header as in Loading. Body: a red `⚠` (20 px, `#E63946`), "Couldn't load the people in this department.", the reason (10 px, `#93a7b4`, see §2.1), and a **Try again** button (solid `#E63946`, white, 10 px bold, radius 6, padding 5px 14px) when `onRetry` is given. No report button, no footer. |
| Empty | `executives.length === 0` and no error | Header as in Loading. Body: `⊘` (20 px, 0.3 opacity), "No executives found for this department.", and a 10 px `#bad4dc` hint "Try re-uploading your CSV, or wait a moment if Board / EM data is still being fetched in the background.". Then **+ Report a missing executive** if `apiBase`. No footer. |
| Populated | `executives.length > 0` | Everything: summary, Save, LinkedIn (Board/EM), legend/toolbar, groups, footer. |
| Paginated | `totalCount > executives.length` | Adds the amber note "Showing top 200 of N by seniority · Save downloads all N". |
| Closing | `deptNode` just became `null` | Keeps rendering the last content during the slide-out. |
| Has inferred lines | any `isSkippedLevel` in any group's tree | Adds the legend chip "⋯ some reporting lines inferred" (`1106-1119`). |
| Report dialog open | `reportPerson && apiBase` | `FeedbackModal` portal on top (`1306-1316`). |

`apiBase` is always truthy in practice (`'/api'` fallback), so the flag buttons, footer and
report buttons always appear. They are hidden only if a host passes an empty `apiBase`.

---

## 5. Header (`ExecPanel.tsx:994-1094`)

Container: `padding: 14px 16px 12px; borderBottom: 1px solid ${color}22; background: #f5f9fb`.
The inner row is `display:flex; alignItems:flex-start; gap:10; flexWrap:wrap`, so the action buttons wrap below the
title instead of squeezing it. The left column is `flex: 1 1 200px; minWidth: 0`.

| Element | Shown when | Content and style |
|---|---|---|
| Eyebrow | always | `'Board of Directors'` if Board, else `'Executives'`. 9 px, `#627184`, letter-spacing 1.2, uppercase, marginBottom 4. |
| Title | always | `deptNode.label`, or `''`. 14 px, bold 700, `color`, nowrap + ellipsis, line-height 1.2. |
| Layer summary | `executives.length > 0` | `layerSummary`, e.g. `14 C-Suite · 1 Executive VP · 5 SVP / Managing Director · 4 VP / Head of · 1 Senior Director / AVP`. 9 px, `#627184`, marginTop 5, line-height 1.5. Wraps. |
| Pagination note | `totalCount > executives.length` | `Showing top {n} of {total} by seniority · Save downloads all {total}`. Numbers use `toLocaleString()`; the separator is `&nbsp;·&nbsp;`. 8 px, `#b45309` on `#fffbeb`, border `#fde68a`, radius 3, padding 2px 6px, inline-block, marginTop 4. |
| **Save** button | `executives.length > 0` | Download-arrow SVG (9×9, stroke 2.5) + "Save". Title "Download these executives as CSV". `background:none; border:1px solid ${color}40; radius 5; padding 3px 8px; color; 9 px; bold 600; letter-spacing 0.3`. When the department has more people than are loaded, it first fetches them all via `loadAll` (label "Saving…", disabled, `cursor: wait`), falling back to the loaded page if that fails. → `downloadExecsCSV` (§13.4). |
| **in LinkedIn** button | `onShowLinkedIn` provided **and** `executives.length > 0` | Text "in LinkedIn". Title "LinkedIn profiles of the Board and Executive Management, with match confidence". Border `#b9d3f0`, text `#0a66c2`, 9 px, bold 700, radius 5, padding 3px 8px. → `App.openLinkedInReport`. |
| **Add people** button | `onAddPeople` provided and `deptNode` set | Plus SVG (11×11) + "Add people". Title `Add people to {label} from CSV, Excel, JSON or an API`. Solid `#E63946`, white text, 11 px, bold 600, radius 6, padding 4px 10px. → `onAddPeople(deptNode)` → `App.openSelection(dept)`. Shown in Loading/Empty states too. |
| Close **×** | always | 18 px, `#627184`, no background or border, title "Close". → `onClose`. |

### 5.1 Layer summary derivation (`ExecPanel.tsx:954-964`)

```ts
const byLayer: Record<number, number> = {}
for (const p of executives) { const l = p.layer ?? 9; byLayer[l] = (byLayer[l] ?? 0) + 1 }
const layerSummary = Object.entries(byLayer)
  .sort(([a], [b]) => Number(a) - Number(b))
  .map(([l, n]) => `${n} ${labels[Number(l)] ?? LAYER_LABELS[Number(l)] ?? `L${l}`}`)
  .join(' · ')
```

`labels` is `BOD_LAYER_LABELS` for the Board, else `LAYER_LABELS`:

| Layer | `LAYER_LABELS` | `BOD_LAYER_LABELS` |
|---|---|---|
| 0 | Board of Management | Chairman |
| 1 | C-Suite | Committee Chair |
| 2 | Executive VP | Non-Executive Director |
| 3 | SVP / Managing Director | — (falls back to the left column) |
| 4 | VP / Head of | |
| 5 | Senior Director / AVP | |
| 6 | Director | |
| 7 | Senior Manager | |
| 8 | Manager | |
| 9 | Senior / Lead / Staff | |
| 10 | Analyst / Specialist | |
| other | `L{n}` | |

Counts do not pluralise ("1 Chairman", "3 Committee Chair").

---

## 6. Legend / toolbar (`ExecPanel.tsx:1097-1146`)

This row is shown only when `executives.length > 0`. Style: `padding 6px 14px; background #f5f9fb; borderBottom 1px solid #bad4dc; flex; gap 10; 9 px; #627184; wrap`.

Left to right:
1. `▼ collapse` and `▶ expand`: static legend text, not buttons.
2. "⋯ some reporting lines inferred", only when `hasInferredLines`. 8 px italic, `#92400e` on `#fffbeb`, border `#fde68a`, radius 3, padding 1px 6px, `cursor: help`. Its native tooltip is `INFERRED_REPORT_NOTE`: "Reporting line inferred — no intermediate manager found in the data at the expected seniority level. This person has been connected to the nearest available senior executive."
3. **Group-by toggle** (`marginLeft:auto`): two joined buttons, "🌐 Region" and "📍 Country", in a container with border `1px solid #bad4dc`, radius 4 and `overflow:hidden`. Each button is padding 2px 8px, 8 px, bold 600, capitalize, letter-spacing 0.5, and transitions its background over 0.15 s. Active: `background: color; color: #fff`. Inactive: `#f5f9fb` / `#627184`. Click → `setGroupMode`.
4. `{regionGroups.length} · {executives.length}`, e.g. `3 · 33`. 8 px, `#bad4dc`.

`hasInferredLines` (`968-978`) rebuilds every group's tree and checks recursively for `isSkippedLevel`.

---

## 7. Body: geographic groups (`ExecPanel.tsx:1216-1281`)

People are always shown inside groups, one group per region or country. A single group is still
rendered with its header.

### 7.1 Grouping (`930-951`)

```ts
const keyFn = groupMode === 'country' ? (p) => inferCountry(p, isBoard) : (p) => inferRegion(p, isBoard)
// … group into a Map in input order …
const apex    = [...executives].sort((a, b) => (a.layer ?? 99) - (b.layer ?? 99))[0]
const primary = keyFn(apex)
return [...map.entries()].sort(([ra, a], [rb, b]) => {
  if (ra === primary) return -1
  if (rb === primary) return 1
  return b.length - a.length
})
```

- **Primary group** = the group of the most senior person: the lowest layer, first in input order on ties. It is always first.
- The remaining groups are sorted by headcount, largest first. Ties keep first-seen order, and the input is sorted by `(layer, label)`.

### 7.2 `inferRegion(person, forBoard)` (`832-847`)

1. `backendRegion = metadata.region.trim()`.
2. Board → `backendRegion || 'Global HQ'`, with no title inference.
3. If `backendRegion` is set and not `'Global HQ'`, trust it. Example: the demo data's `"Global"` is kept as "Global".
4. If the title (`designation`, else `label`, lower-cased) matches `/\b(global|group)\s+(?:ceo|chief|president|managing|head)\b/`, use `backendRegion || 'Global HQ'`.
5. Otherwise the first matching `GEO_PATTERNS` entry, in this order: Greater China, Hong Kong, Southeast Asia (`south(east|ern)? asia`, `asean`), North America, Latin America (`latam`), Middle East & Africa (`middle east (and africa)?`, `mena`), New Zealand, Australia / NZ (`anz`, `australi`), South Korea, Gulf / GCC, Nordic (`nordics?`, `scandinavia`), India (`india`, `indian`), Greater China (`china`, `chinese`), Japan, Singapore, Asia Pacific (`apac`, `asia.?pacific`, `apj`), EMEA, Europe (`europe`, `european`), Germany, France, United Kingdom (`u.?k.?`, `united kingdom`, `british`), Canada, Americas, Brazil, Mexico (`mexic`), Africa, Middle East, Asia Pacific (`asia`). Full regexes: `801-830`.
6. Otherwise `backendRegion || 'Global HQ'`.

### 7.3 `inferCountry(person, forBoard)` (`897-925`)

1. If `metadata.country` is longer than 1 character, use it.
2. Board → `'Global HQ'`.
3. If `metadata.location` is set, take its last comma-separated part. Common codes are spelled out first (dots removed, case-insensitive): `UK`/`GB` → United Kingdom, `US`/`USA` → United States, `UAE` → UAE, `KSA` → Saudi Arabia. Any other part longer than 2 characters is used as written. So "San Francisco, USA" → "United States" and "London, UK" → "United Kingdom".
4. Otherwise the first matching `COUNTRY_PATTERNS` entry on the title (`852-895`): United States, United Kingdom, Germany, France, Netherlands (`netherlands?`, `dutch`), Switzerland, Sweden, Norway, Denmark, Belgium, Spain, Italy, Poland, Ireland, Austria, Portugal, Canada, Australia, New Zealand, Singapore, Hong Kong, Japan, South Korea, China, India, South Africa, Nigeria, Kenya (`keny`), Egypt, UAE, Saudi Arabia (`saudi`, `ksa`), Qatar, Kuwait, Brazil, Mexico, Chile, Colombia, Malaysia, Indonesia, Thailand, Philippines, Vietnam.
5. Otherwise `inferRegion(person, forBoard)`.

### 7.4 Group header row (`1226-1262`)

Clicking anywhere on the header, or pressing Enter/Space when it has focus, toggles `collapsedRegions` for that group name. It is a `role="button"` with `tabIndex 0` and `aria-expanded`. All groups start expanded, and every group and row expands again when a different department opens.

| Part | Primary group (index 0) | Other groups |
|---|---|---|
| Row | bg `#eef5f9`; no top border | bg `#f5f9fb`; top border `1px solid #dde8ed` |
| Left border | `3px solid ${color}cc` | `3px solid ${color}63` |
| Bottom border | `1px solid #dde8ed`, none when collapsed | same |
| Icon | `📍` in country mode, else `🌐`; 9 px; `color+'cc'` | `color+'55'` |
| Name | 9 px, bold 700, letter-spacing 0.8, uppercase, ellipsis, `color` | `color+'88'` |
| Count badge | 8 px, `#0c3649` on `#dde8ed`, border `#bad4dc`, radius 3, padding 1px 6px | `#627184` on `#f5f9fb` |
| Caret | `▾` expanded / `▸` collapsed; 8 px; `color+'77'` | same |

Padding is `5px 12px 5px 10px`, gap 7, `cursor:pointer`, `user-select:none`.

When a group is expanded, its tree roots are rendered as `PersonRow`s at depth 0 (`1265-1278`).

---

## 8. Tree building (who appears under whom)

`buildExecTree(people, isEM, isBOD)` (`274-316`) is called once per group:
`buildExecTree(regionExecs, isEM && isPrimary, isBoard)` (`1220`). The input is first sorted by
`(layer ?? 99, label)`.

### 8.1 Board → `buildBODTree` (`221-268`), used for every Board group

1. `chairs` = everyone with layer 0.
2. `leads` = non-chairs whose `board_sub_role` is `lead_director` or `vice_chair`.
3. `others` = all other non-chairs.
4. **Committees.** For each person and each `{name, role}` in `metadata.committees`, build a group keyed by the exact committee name. The first member with `role === 'Chair'` becomes the group's chair. Everyone else, including a second "Chair", becomes a member.
5. Order the committees by `committeeRank`: 0 if the name contains "audit"; 1 for `compensation|remuneration|people|human capital|leadership development`; 2 for `nominat|governance`; 3 otherwise. Ties are broken alphabetically (`localeCompare`).
6. Each committee becomes a **synthetic heading node**:
   ```ts
   // ExecPanel.tsx:235-242
   node_id: `committee__${name}`, node_type: 'committee', label: name, layer: 1,
   metadata: { is_group: true,
     designation: `${g.chair ? `Chaired by ${g.chair.label} · ` : ''}${people.length} member${people.length === 1 ? '' : 's'}` }
   ```
   Its children are the chair first, then the members sorted by label.
7. `unassigned` = `others` who are on no committee. When no committee data exists at all, every `other` counts as unassigned.
8. The chair's children are `[...leads, ...committees, ...unassigned]`.
9. The first chair carries this structure. Co-chairs are childless roots after it. With no chair, the children become the roots.

A director on two committees appears under both. A lead director appears as a leaf **and** under
their committees. The chair can also appear inside a committee, for example as its chair.

### 8.2 Executive Management, primary group (`288-315`)

1. `topLayer` = the layer of the first sorted person. `topGroup` = everyone at that layer. `deeper` = the rest.
2. Apex index: the first `topGroup` member with `metadata.is_apex`, else the first for which `isCeoTitle` is true.
3. `topGroup.length <= 1` → `buildLayerTree` (§8.3). Here the single top person is the natural apex.
4. No apex found → a **flat list**: every person is a root with no children.
5. Otherwise the apex is the only root, and **everyone else is a direct report, flat**: `[...peers, ...deeper]` in sorted order. Deeper layers are deliberately not nested (comment at `309-312`).

`isCeoTitle` (`102-133`), used only when no `is_apex` flag is set:
- `is_apex` → true.
- Text = `designation` lower-cased, else `label`.
- False if it matches `\bco-?(ceo|chief executive)\b`, or `\b(deputy|vice|assistant|associate)\b`, or a regional word (`emea|apac|apj|latam|mena|asean|gcc|anz|asia|pacific|europe|america|americas|africa|middle east|regional|divisional|country|international|australia|new zealand|india|china|japan|south korea|uk|u.k.|germany|france|italy|spain|canada|brazil|singapore|hong kong|south(\s)?east asia`).
- False if `(\w+)\s+ceo\b` captures a word outside {group, global, chief, interim, acting, incoming, the, and, founder, president, chairman}.
- True if it matches `chief executive`, `\bceo\b`, `^(group )?president$` or `^group (chief executive|managing director)$`.

### 8.3 Everything else → `buildLayerTree` (`137-183`)

Used for non-EM departments, for non-primary EM groups, and for EM groups with one top person.
1. Split the sorted list into consecutive same-layer groups.
2. For each adjacent pair (parent layer group, child layer group): if the parent group has one person, it gets all the children. Otherwise the children are split into consecutive slices of `ceil(children / parents)`, in order. These reporting lines are invented.
3. When the child layer is more than one step below the parent layer, every child gets `isSkippedLevel = true`, which draws dashed lines and turns on the legend chip.
4. The roots are the first layer group.

---

## 9. Person row (`PersonRow`, `ExecPanel.tsx:371-667`)

Props: `node`, `depth`, `isLast` (index-based, passed by the parent), `parentLines: boolean[]`,
`color` (the drawer accent), `onToggle`, `collapsed`, `onReport?`, `onAddPeople?`.

Derived values: `pColor = nodeColor(p, color) = SECTOR_COLORS[p.sector] ?? p.color ?? color` (`79-82`);
`hasReports`; `isCollapsed`; `isSkipped`; `indent = depth*28`; `isGroup = metadata.is_group`.

### 9.1 Row container (`428-438`)

`display:flex; alignItems:center; padding: 8px 14px 8px 12px; borderBottom: 1px solid #f0f4f7;
position: relative; cursor: pointer` only if it has reports. Background
`rgba(245,159,11,0.03)` when `isSkipped`. **Clicking the row toggles the collapse of its
reports** (`hasReports && onToggle(node_id)`). Rows with reports are also `role="button"`,
`tabIndex 0` and `aria-expanded`, and toggle on Enter/Space (`toggleKeys`). There is no hover style and no other click action.

### 9.2 Tree connector lines (absolutely positioned, `pointer-events:none`)

- **Vertical line to the parent** (depth > 0): `left = 12 + (depth-1)*20 + 8`, top 0, bottom `50%` if this is the last child else 0, width 1. Colour `color+'25'`, or the dashed gradient `repeating-linear-gradient(to bottom, ${color}45, ${color}45 4px, transparent 4px, transparent 8px)` when skipped (`393`, `443-451`).
- **Ancestor continuation lines**: one per `parentLines[li] === true`, at `left = 12 + li*20 + 8`, full height, `color+'20'` (`453-461`).
- **Horizontal elbow** (depth > 0): same left, `top:50%`, width 12, height 1. Colour `color+'30'`, or the dashed `repeating-linear-gradient(to right, …45 3px, transparent 3px … 6px)` when skipped (`463-471`).
- `parentLines` for children = `[...parentLines, !isLast]` (`657`).

### 9.3 Row contents, left to right

1. **Indent spacer**: width `depth*28` (`474`).
2. **Chevron**: width 14, 9 px, centred, marginRight 4, no text selection. `▼` expanded / `▶` collapsed when it has reports, else `·`. Colour `color + 'aa'`, or `color + '30'` without reports (`477-482`).
3. **Avatar**: a 30×30 circle, bg `pColor+'15'`, border `1px solid ${pColor}38`, the first character of `label` upper-cased, 12 px bold, `pColor`, marginRight 10 (`485-493`). There are no photos.
4. **Text column** (`flex:1; minWidth:0`):
   - **Name**: 11.5 px; bold 700 and `#00204d` at depth 0, else 600 and `#3d6880`; nowrap + ellipsis; full name as its tooltip.
   - **Title**: only if `designation` is set. The full text, cut by CSS ellipsis only, with the full title as its tooltip. 9.5 px, `#627184`, marginTop 1.
   - **Board role badge**: only if `board_sub_role` is set and is not `director` or `chairman`, and `getBoardRoleLabel` returns text. Inline-block, 8 px, `#b45309` on `#fef3c7`, border `#fde68a`, radius 3, padding 1px 5px, marginTop 2 (`513-528`). Labels: `vice_chair`→"Vice Chair", `lead_director`→"Lead Director", `audit_chair`→"Audit Committee", `comp_chair`→"Comp & Rem", `nom_chair`→"Nom & Gov", `risk_chair`→"Risk Committee", `tech_chair`→"Tech & Innovation", `esg_chair`→"ESG / Sustainability", `finance_chair`→"Finance Committee", `committee_chair`→"Committee Chair". The backend currently emits `chairman`, `vice_chair`, `lead_director`, `audit_chair`, `comp_chair`, `nom_chair`, `risk_chair`, `tech_chair`, `esg_chair`, `committee_chair` and `director` (`structural_engine.py:1849-1913`).
   - **Department line**: only if `leads_department` is set (`506-536`). It contains:
     - A chip `▸ {leads_department}`: 8.5 px, `#00204d` on `#e8f1fb`, border `#b9d3f0`, radius 3, padding 1px 6px, max-width 190, ellipsis. Tooltip `{name} leads {dept}`.
     - **+ Add people**, only when the row has `onAddPeople` (Executive Management only) **and** `leads_department_id`. 8.5 px, bold 600, `#E63946` on `#fff5f5`, border `#f5c2c6`, radius 3, padding 0 6px, line-height 14px. Tooltip `Add people to {dept} from CSV, Excel, JSON or an API`. Click: `stopPropagation`, then `onAddPeople({node_id: leads_department_id, label: leads_department})`.
   - **Footer line** (flex, gap 7, wrap, marginTop 2) (`538-578`):
     - `🌐 {region}` chip if `region` is set: 8.5 px, `#0c3649` on `#eef5f9`, border `#bad4dc`, radius 3.
     - `📍 {location before first comma}` if `location` is set: 8.5 px, `#627184`.
     - `💰{pay/1e6 to 1dp}M` if `pay` is set: 8 px, `#627184`. The current pipeline does not set `pay` on chart people. It exists only on the separate public-company path (`api_server.py:2465`), so in practice it is never shown.
     - Right-aligned group (`marginLeft:auto`, gap 5):
       - `LinkedInBadge` (§9.4).
       - Source badge: `nlp_method` contains `'web'` → `🌐 Web` (`#059669` on `#ecfdf5`, border `#a7f3d0`), tooltip "Found on the web: the organisation's own pages or cited research". `llm_leadership_web` covers both, so it does not claim "Website". Else if it contains `'llm_leadership'` → `✦ AI Knowledge` (`#0c3649` on `#dde8ed`, border `#bad4dc`). Otherwise nothing. 8 px, radius 3, padding 1px 5px.
5. **Seniority badge**: `L{layer ?? '?'}`, 8 px, `pColor` on `pColor+'15'`, border `pColor+'30'`, radius 3, padding 2px 5px, marginLeft 6 (`582-589`).
6. **Report count**: `{n}↓` when it has reports, 8 px, `#627184`, marginLeft 4 (`592-599`).
7. **Flag ⚑**: when `onReport` is set, which is whenever `apiBase` is set. 11 px, `#bad4dc` at opacity 0.6. On hover it goes to `#E63946` at opacity 1, transitioning over 0.12 s. Tooltip "Report a change for this person". Click: `stopPropagation`, then `onReport(person)`, which opens the report dialog pre-filled (`602-619`).
8. **Children**: when it has reports and is not collapsed, the children are rendered recursively at `depth+1` (`624-637`).

### 9.4 `LinkedInBadge` (`326-353`)

| `linkedin_url` | `linkedin_confidence` | Renders |
|---|---|---|
| empty | `'Not Found'` | `<span>in · not found</span>`, 8 px, `#94a3b8`, tooltip "No LinkedIn profile could be verified for this person". |
| empty | anything else, or missing | nothing |
| set | `High` | link `in ↗ High`, `#047857` on `#ecfdf5`, border `#a7f3d0` |
| set | `Medium` | `in ↗ Medium`, `#b45309` / `#fffbeb` / `#fde68a` |
| set | `Low` | `in ↗ Low`, `#c2410c` / `#fff7ed` / `#fed7aa` |
| set | `Provided` | `in ↗`, `#1d4ed8` / `#eff6ff` / `#bfdbfe` |
| set | empty or unknown | `in ↗` with the Provided style. This is the demo-data case. |

The link opens in a new tab (`target=_blank rel=noreferrer`) and calls `stopPropagation` so it doesn't collapse the row. Style: 8 px,
bold 600, radius 3, padding 1px 5px, nowrap, no underline. Its tooltip (multi-line):
`{conf} confidence: {tip}` + `\nOn LinkedIn: {matched_name} — {matched_title}`, where the tips are
"Name, company and leadership title all match" / "Name and company match; title not confirmed" /
"Likely, but the company could not be confirmed — check before use" / "From your uploaded data".

---

## 10. Committee heading row (Board only, `ExecPanel.tsx:399-425`)

Rendered instead of a person row when `metadata.is_group`. It has no avatar, badges or flag.
- Container: flex, gap 8, `padding: 7px 14px 6px ${12 + depth*20}px`, borderBottom `#f0f4f7`, bg `#f7fafc`, pointer cursor when it has members. Click or Enter/Space → toggle collapse (key `committee__<name>`). `role="button"`, `tabIndex 0`, `aria-expanded`.
- Chevron: width 14, 9 px, `color+'aa'`, `▼`/`▶`/`·`.
- Name: 10 px, bold 700, letter-spacing 0.4, **uppercase**, `#00204d`, ellipsis.
- Right: `designation`, e.g. "Chaired by Roger W. Ferguson Jr. · 3 members". 9 px, `#627184`, ellipsis, `marginLeft:auto`.
- It draws no connector lines. Its members (depth 2) draw their own lines.

---

## 11. Footer (`ExecPanel.tsx:1285-1303`)

Shown when `apiBase` is set and `executives.length > 0`. Style: padding 8px 14px, top border `#eef2f5`,
bg `#f5f9fb`, right-aligned. It holds one button, **+ Report a missing executive**: transparent, border
`rgba(52,145,232,0.25)`, radius 5, padding 4px 12px, `#3491E8`, 9 px, bold 600. It opens the report
dialog in "new executive" mode (`reportPerson = {node_id:'__new__'}`).

The empty-state version has the same text: background
`rgba(52,145,232,0.08)`, radius 6, padding 6px 16px, 10 px.

---

## 12. Interactions

| User action | Result |
|---|---|
| Click a department node in the chart | The drawer opens (or switches) to that department. The chart branch also expands or collapses. If it was open on another department, the header changes at once and the body shows Loading unless the department is cached. |
| Click the same department again | Re-opens and re-fetches unless cached, and toggles the chart branch. It does not close the drawer. |
| Click a person / ghost / root / synthetic node in the chart | Nothing for the drawer. |
| **×** or Escape | Close (§2.5). Escape closes the report dialog first if it is open. |
| **Save** | Downloads a CSV of every person in the department, fetching the rest first if needed (§13.4). |
| **Try again** (error state) | Re-fetches the department, skipping the cache. |
| **in LinkedIn** | Opens the LinkedIn dialog, which shows Loading and then the table. The drawer stays open underneath. |
| Header **Add people** | `openSelection(deptNode)` → the add-people dialog, pre-selecting the department's `node_id` (§13.3). |
| Row **+ Add people** (EM only) | `openSelection({node_id: leads_department_id, label: leads_department})`. That department is pre-selected and checked. |
| Region / Country toggle | Regroups. Collapse state is kept, keyed by node id and group name. The choice carries over to other departments. |
| Click a group header (or Enter/Space) | Collapses or expands that group. |
| Click a person row with reports, or a committee heading (or Enter/Space) | Collapses or expands its subtree. |
| Click a person row without reports | Nothing. |
| LinkedIn badge link | Opens the profile in a new tab. |
| ⚑ | Opens the report dialog for that person. |
| **+ Report a missing executive** | Opens the report dialog with no person. |
| Hover ⚑ | Turns red at full opacity. |
| Hover a badge or chip | Native `title` tooltip. |
| Chart search (top bar) | Highlights a node for 2.5 s (`App.tsx:1278-1281`). Does **not** open the drawer. |
| Keyboard | Escape closes layers (§2.5). Collapsible rows, committee headings and group headers take focus and toggle on Enter/Space. Buttons are native `<button>`s. There is no focus trap and focus is not moved into the drawer on open. |

---

## 13. Things the drawer opens or triggers

### 13.1 Report dialog — `FeedbackModal.tsx`

- Mounted by the drawer when `reportPerson && apiBase`. Props: `person` (undefined for `__new__`), `deptName = deptNode.label`, `deptId = deptNode.node_id`, `jobId`, `companyName` (= the chart root's label), `apiBase`, `onClose → setReportPerson(null)`. Escape closes it.
- Rendered as a portal to `document.body`. The overlay is `fixed inset 0, z 2000, rgba(8,15,22,0.72), backdrop-filter: blur(4px)`, centred with 16 px padding. Clicking the overlay itself closes it.
- Card: `#0c1e2a`, border `#1e3a4a`, radius 12, padding 24/24/20, max-width 420, shadow `0 24px 64px rgba(0,0,0,0.6)`, column with gap 16.
- **Header**: "Report a Change" (15 px bold, white) above `{companyName}{deptName ? ' · ' + deptName : ''}` (10 px, white at 38%). A × button on the right.
- **Person context** (only when flagging a person): `{name}` in bold white + ` · {designation.slice(0,50)}` in a light-blue box.
- **Change type** (label "CHANGE TYPE", 10 px uppercase, letter-spacing 1, white at 45%): five radio cards.

  | value | label | description |
  |---|---|---|
  | `no_longer_here` | No longer at company | This person has left or retired |
  | `wrong_dept` | Wrong department | Person is in an incorrect department |
  | `wrong_hierarchy` | Wrong reporting line | Reporting hierarchy is incorrect |
  | `new_executive` | Add new executive | Someone is missing from the chart |
  | `other` | Other correction | Any other inaccuracy |

  The default is `no_longer_here` when a person is given, else `new_executive`. The selected card has a `rgba(52,145,232,0.12)` background, a `0.4` border and title `#7ec8f8`.
- **LinkedIn URL**: shown for `new_executive` (label "LinkedIn URL *") and `wrong_hierarchy` (label "LinkedIn URL (optional)"). Placeholder `https://www.linkedin.com/in/...`.
- **Notes** textarea (min-height 72, vertical resize). The placeholder depends on the type: "When did they leave? (optional)" / "Which department should they be in?" / "Who do they actually report to?" / "Name, title, and any other details…" / "Describe the correction…".
- **Validation**: `new_executive` with an empty LinkedIn URL → the error "LinkedIn URL is required for new executives." There is no other validation; the field is `type=url` but no submit-time check is made.
- **Submit**: `POST {apiBase}/report-change`, JSON `{type, companyName, personName?, personTitle?, currentDept?, jobId?, nodeId?, deptId?, linkedInUrl?, notes?, timestamp: Date.now()}` (empty strings sent as `undefined`). `jobId` is the chart's job, `nodeId` the flagged person, `deptId` the drawer's department. The button reads "Submitting…" and is disabled while the request runs. A non-OK response throws the raw response text, which is shown as the error. Network failure falls back to "Submission failed — please try again.". The error box is 11 px, `#E63946` on a red-tinted background.
- **Success**: the card is replaced by `✓` (32 px), "Report submitted" (14 px bold, `#52d08c`) and "Thank you — our team will review this correction." It auto-closes after 1800 ms, and any click closes it.
- **Buttons**: Cancel (outlined) and Submit Report (`#3491E8`).
- **Backend**: the report gets an `id` (uuid) and `received_at`, is kept in memory, and is also pushed to the Upstash Redis list `organogram:change_reports` (newest first, capped at 2000) when the store is configured. The response is `{ok:true, id, stored}`, where `stored` says whether it reached the durable store. `GET /change-reports?limit=` (default 200) reads from Redis when configured (`durable: true`), else from memory.

### 13.2 LinkedIn dialog — `LinkedInTable.tsx`

- `App.openLinkedInReport` sets `'loading'`, clears `linkedInError` and fetches `/linkedin-report`. Failures keep the dialog open with an error in the table area: "Couldn't load the LinkedIn profiles." over the reason, which is "This chart is no longer on the server — create it again." for `loaded:false`, "Connection failed — the backend may be restarting. Try Refresh in a moment." for a network failure, or "Server error {status}". **↻ Refresh** retries.
- Rendered by `App` (`1964-1971`) when `linkedInReport` is truthy. `report` = `null` while loading. `companyName` = the chart root's label. `onRefresh` refetches; `onClose` → `null`.
- Portal. The overlay matches the report dialog's (z 2000, blur). Card `#0c1e2a`, max-width 1180, max-height 88vh, radius 12.
- **Header**: "LinkedIn profiles — Board & Executive Management". Subtitle `{company} · {matched} of {total} matched`, where matched = rows whose confidence is not "Not Found", plus ` · search still running` when `!linkedin_done`. Explainer: "Each profile is checked against the person's name, the company and a leadership title. When that can't be confirmed the answer is “Not Found”, never a guess."
- **Buttons**: `↻ Refresh` (tooltip "Re-read the latest results"), `Copy as Markdown` (copies `report.markdown` and shows `✓ Copied` for 1.6 s; clipboard errors are ignored), `Download CSV` (file `{company with _}_leadership_linkedin.csv`, UTF-8 BOM, columns = `report.columns` + Panel + Department, every value quoted) and ×.
- **Filter chips**: All, High, Medium, Low, Provided, Not Found, each as `{lvl} · {n}`. Levels with 0 rows are hidden, except All. The active chip uses the level's colour.
- **Table** (min-width 860, sticky header). Columns: Full Name (with a sub-line `{Panel}{Department ? ' · leads ' + Department : ''}`), Expected Title / Role, Company, LinkedIn Profile URL (link with `https://www.` stripped, or "Not Found"), Confidence (pill), Matched Name & Title on LinkedIn (or "—").
- **Confidence colours (dark)**: High `#52d08c`, Medium `#f5b642`, Low `#f08a5d`, Provided `#7cc4ff`, Not Found white at 45%.
- **States**: error (above); "Loading…"; "No Board or Executive Management members in this chart yet." for zero rows. Escape closes the dialog.
- **Backend rows** (`api_server.py:2118-2160`): one row per Board/EM person, keyed by normalised name. A person on both panels is merged: `Panel` becomes "Board of Directors + Executive Management" and the titles are joined with " / ". Confidence is the stored `linkedin_confidence`, or "Not searched" when there is no URL and no confidence.

### 13.3 Add-people dialog — `App.tsx:1068-1128`, `1863-2018`

- `openSelection(preselect)` sets `selectOpen`, clears the note, sets `selectedIds = [preselect.node_id]` — except for `dept__executive_management`, which is not one of `/selectable`'s targets, so nothing is pre-selected there — and `ingestTarget = preselect.label`, then fetches `GET /selectable`. On a network error `selectable` is set to `null`, which leaves "Loading targets…" on screen.
- It is **not a portal**. It renders inside the chart area at `zIndex: 300`, above the drawer (`zIndex: 200`). The overlay is `rgba(0,32,77,0.35)`; clicking it closes the dialog unless an ingest is in progress. The card is white, 560 wide, max-height 78vh, scrollable, radius 10, padding 20.
- **Title**: `Add people to {target}` or "Add people to the chart". Subtitle: "Selected below. Adjust if this roster spans more than one area, then choose a source." (with a target and at least one row selected) or "Pick the executives or their departments this roster belongs to, then choose a source.", followed by " Anyone already in the chart is merged, not duplicated."
- **Lists from `/selectable`**:
  - "EXECUTIVE MANAGEMENT": a checkbox per executive showing name, title and department (or "runs the company").
  - "THEIR DEPARTMENTS": a checkbox per department showing label, `led by {head}` and `{n} person/people`.
  - Checked rows get a `#eef6fa` background.
- **Source**:
  - "Upload CSV / Excel / JSON" triggers the hidden `<input type=file accept=".csv,.json,.xlsx,.xls">` (`App.tsx:1382-1392`) → `POST /ingest` (multipart).
  - "Paste JSON" opens the Paste-JSON dialog.
  - A URL field (placeholder "…or pull from an API URL returning JSON or CSV", Enter submits) with a "Fetch" button → `POST /ingest?…&source_url=`.
- **Result note**: `Ingesting {label}…`, then `Added {n}` + `, merged {m} already in the chart` + `.`, or the error detail. On success the dialog closes, the selection clears, the chart reloads via `/tree`, and `refreshOpenPanel()` re-reads the open drawer. Escape closes the dialog unless an ingest is running.
- **Scope**: `&scope=<comma-separated selectedIds>`. The backend echoes `scope` but `_apply_ingest` does **not** use it. The classifier decides each person's department (`api_server.py:2008-2034`).
- **Paste-JSON dialog** (`App.tsx:2060-2133`): portal, z 9999. Title "Paste JSON roster". The help text and an example placeholder sit above a monospace textarea. Errors: "Paste some JSON first." or `Not valid JSON — {message}`. "Add to chart" → `POST /ingest-json` with the raw text.

### 13.4 CSV "Save" — `downloadExecsCSV` (`ExecPanel.tsx:670-714`)

- Contains every person in the department, in server order. When more exist than are loaded, `App.loadAllPanelPeople` pages through `/executives` 5000 at a time first.
- Columns: Name, Title, Department (= drawer label), Seniority Level, Region, Location, LinkedIn URL, LinkedIn Confidence, Matched on LinkedIn, Leads Department, Source.
- Seniority Level: for the Board, `0 → 'Chair (Board)'`, `1 → 'Lead / Committee Chair (Board)'`, `2 → 'Director (Board)'` (missing layer → 2). Otherwise the G-scale labels `'Board of Management (G0)' … 'Analyst / Specialist (G10)'` with `layer ?? 9`, falling back to `G{n}`.
- Source: `nlp_method` contains `web` → "Web"; contains `llm_leadership` → "AI Knowledge"; else "Uploaded Data".
- Every value is double-quoted, with `"` escaped as `""`. The file starts with a UTF-8 BOM. Filename: `{label, whitespace→_}_executives.csv`. Built client-side as a Blob URL with a temporary `<a>`.
- Columns are taken from the first row. With no people the button is hidden anyway.

---

## 14. Data contract

### 14.1 Drawer props (`ExecPanel.tsx:7-19`) and how `App` fills them (`App.tsx:2021-2044`)

```tsx
<ExecPanel
  onAddPeople={panelDept && panelDept.node_id !== 'dept__board_of_management' ? (dept) => openSelection(dept) : undefined}
  deptNode={panelDept}
  executives={panelExecs}
  totalCount={panelTotal}
  error={panelError}
  onRetry={() => { if (panelDeptRef.current) loadPanelPeople(panelDeptRef.current, false) }}
  loadAll={loadAllPanelPeople}
  jobId={activeJobIdRef.current}
  onClose={closePanel}
  apiBase={API}
  companyName={deptTree?.label ?? ''}
  onShowLinkedIn={panelDept && ['dept__board_of_management', 'dept__executive_management'].includes(panelDept.node_id)
    ? openLinkedInReport : undefined}
/>
```

### 14.2 `OrgNode` (`types/index.ts:1-14`)

`node_id, node_type, label, layer, sector, color, is_ghost, is_synthetic?, expanded, has_more?,
metadata: Record<string, string|number>, children?`. In practice `metadata` also holds booleans
(`is_apex`, `is_group`) and arrays (`committees`); the type is looser than the data.

### 14.3 `GET /executives` record → drawer person

The server returns the raw graph node attributes. `toPersonNode` (`App.tsx:139-162`) maps them:
`label ?? first_name+last_name`, `layer ?? 9`, `sector ?? 'All'`, `color ?? <department colour>`,
`is_ghost:false`, `expanded:false`, `has_more:false`, `metadata: p.metadata ?? {…flat fields}`. The `metadata` object is
passed through unchanged.

Metadata fields the drawer reads:

| Field | Used for | Values seen |
|---|---|---|
| `designation` | Title line, apex detection, region/country inference, report dialog | free text |
| `region` | Region chip, region grouping | `"Global HQ"`, `"Global"` (demo), `"Americas"`, `"EMEA"`, … |
| `country` | Country grouping | usually `""` |
| `location` | 📍 chip (before the first comma), country grouping | `"San Francisco, USA"` (demo); `""` for researched leaders |
| `pay` | 💰 chip | not set by the current pipeline |
| `linkedin_url`, `linkedin_confidence`, `linkedin_matched_name`, `linkedin_matched_title` | LinkedIn badge, CSV | confidence: `High` / `Medium` / `Low` / `Not Found` / `Provided` / missing |
| `nlp_method` | Source badge, CSV Source | `llm_leadership_web`, `llm_leadership_ai`, `upload_leadership`, `uploaded_data`, `title_keyword`, `title_keyword_high`, … |
| `board_sub_role` | Lead/vice detection (Board tree), role badge | §9.3 |
| `committees` | Board committee groups | `[{name, role: 'Chair' \| 'Member'}]` |
| `is_apex` | EM apex | `true` on one person (`structural_engine.py:3045`) |
| `leads_department`, `leads_department_id` | ▸ chip, row Add people | e.g. `"Finance & Accounting"`, `"dept__finance___accounting"` (`structural_engine.py:2884`) |
| `is_group` | Committee heading (client-made) | `true` |

Node-level fields: `layer` (Board: 0 chair, 1 lead/vice/committee chair, 2 director; others: 1–10),
`sector`, `color`, `label`, `node_id`.

### 14.4 Accent colour table (`SECTOR_COLORS`, `61-69`)

`Automotive #F59E0B`, `Govt #3B82F6`, `NGO #10B981`, `Startup #8B5CF6`, `Public #06B6D4`,
`Private #64748B`, `All #0c3649`. Other sector values fall back to `node.color`. The backend currently
sends sectors such as "Private", "Technology", "General" and "Banking & Financial Services", so most departments
render in `#64748B`.

---

## 15. Visual tokens used by the drawer

| Token | Value | Where |
|---|---|---|
| Panel bg | `#ffffff` | container |
| Header / legend / footer bg | `#f5f9fb` | header, legend, footer |
| Primary group bg | `#eef5f9` | group header |
| Committee row bg | `#f7fafc` | committee heading |
| Row divider | `#f0f4f7` | rows |
| Group divider | `#dde8ed` | groups |
| Legend divider / chip borders | `#bad4dc` | |
| Footer divider | `#eef2f5` | |
| Navy text | `#00204d` | names (depth 0), committee names, ▸ chip |
| Body grey | `#627184` | titles, eyebrow, summary, counts |
| Muted | `#bad4dc` | empty-state hint, legend count, flag rest colour |
| Secondary name | `#3d6880` | names at depth > 0 |
| Brand red | `#E63946` | Add people, flag hover |
| Info blue | `#3491E8` | report buttons, default accent |
| LinkedIn blue | `#0a66c2` (text), `#b9d3f0` (border) | in LinkedIn |
| Amber | `#b45309` / `#fffbeb` / `#fde68a` / `#fef3c7` / `#92400e` | pagination note, inferred chip, board role badge |
| Shadow | `-12px 0 40px rgba(12,54,73,0.12)` | container |
| Font | `'SF Mono','Fira Code','Cascadia Code',monospace` | inherited |
| Sizes | 8, 8.5, 9, 9.5, 10, 11, 11.5, 12, 14, 18, 20 px | as listed per element |

---

## 16. UI copy (every string the drawer itself renders)

`Board of Directors` · `Executives` · `Save` · `Download these executives as CSV` · `in LinkedIn` ·
`LinkedIn profiles of the Board and Executive Management, with match confidence` · `Add people` ·
`Add people to {dept} from CSV, Excel, JSON or an API` · `×` · `Close` · `Saving…` ·
`Showing top {n} of {total} by seniority · Save downloads all {total}` · `▼ collapse` · `▶ expand` ·
`⋯ some reporting lines inferred` · the `INFERRED_REPORT_NOTE` text (§6) · `🌐 Region` · `📍 Country` ·
`{groups} · {people}` · `⟳ Loading…` · `⊘` · `No executives found for this department.` ·
`Try re-uploading your CSV, or wait a moment if Board / EM data is still being fetched in the background.` ·
`⚠` · `Couldn't load the people in this department.` · `Try again` · the error reasons in §2.1 ·
`+ Report a missing executive` · `Report a change for this person` · `⚑` ·
`▸ {dept}` · `{name} leads {dept}` · `+ Add people` · `🌐 {region}` · `📍 {city}` · `💰{x.x}M` ·
`in · not found` · `No LinkedIn profile could be verified for this person` · `in ↗` / `in ↗ High|Medium|Low` ·
the four LinkedIn tooltips (§9.4) · `On LinkedIn: …` · `🌐 Web` · `Found on the web: the organisation's own pages or cited research` · `✦ AI Knowledge` · `L{n}` / `L?` · `{n}↓` ·
`Chaired by {name} · {n} member(s)` · every layer label (§5.1) · every board role label (§9.3) · the
CSV headers and values (§13.4). Dialog copy is in §13.

---

## 17. Real examples

Taken from production. Job ids, LinkedIn slugs and real people's names are redacted. The demo
company *AutoPrime Motors* is fictional, so its names are kept; its LinkedIn slugs are redacted.

### 17.1 Board — Google (10 directors, live)

Input: `deptNode = {node_id:'dept__board_of_management', label:'Board of Management', sector:'Private', color:'#64748B'}`.
Two of the ten people (`GET /executives`):

```json
{"node_id":"llm_7f25…","node_type":"person","label":"[Chair]","layer":0,"sector":"Private","color":"#64748B",
 "metadata":{"designation":"Chair of the Board","company":"Google","linkedin_url":"","region":"Global HQ",
  "location":"","country":"","dept_primary":"Board of Management","nlp_method":"llm_leadership_web",
  "board_sub_role":"chairman","committees":[{"name":"Nominating and Corporate Governance Committee","role":"Chair"}],
  "linkedin_confidence":"Not Found"}}
{"node_id":"llm_cd1c…","label":"[Director R]","layer":1,"metadata":{"designation":"Independent Director",
  "region":"Global HQ","nlp_method":"llm_leadership_web","board_sub_role":"director",
  "committees":[{"name":"Audit Committee","role":"Chair"},{"name":"Risk and Compliance Committee","role":"Chair"}],
  "linkedin_url":"https://www.linkedin.com/in/[redacted]","linkedin_confidence":"Low"}}
```

Rendered (drawer text, top to bottom; accent `#64748B`):

```
BOARD OF DIRECTORS
Board of Management
1 Chairman · 3 Committee Chair · 6 Non-Executive Director        [Save] [in LinkedIn] [×]
▼ collapse  ▶ expand                          [🌐 Region|📍 Country]  1 · 10
🌐 GLOBAL HQ  (10) ▾
▼ (J) [Chair]  Chair of the Board  🌐 Global HQ   in · not found  🌐 Web   L0  5↓  ⚑
   ▼ AUDIT COMMITTEE ........ Chaired by [Director R] · 3 members
      · (R) [Director R]  Independent Director  🌐 Global HQ  in ↗ Low  🌐 Web  L1 ⚑
      · (R) [Director M]  Independent Director  …  in · not found …  L2 ⚑
      · (R) [Director W]  Independent Director  …  in ↗ Medium …    L1 ⚑
   ▼ LEADERSHIP DEVELOPMENT, INCLUSION AND COMPENSATION COMMITTEE … Chaired by [Director W] · 3 members
   ▼ NOMINATING AND CORPORATE GOVERNANCE COMMITTEE … Chaired by [Chair] · 2 members
   ▼ EXECUTIVE COMMITTEE … Chaired by [Co-founder 1] · 3 members
   ▼ RISK AND COMPLIANCE COMMITTEE … Chaired by [Director R] · 3 members
                                                        [+ Report missing executive]
```

Why: the Chair is layer 0 and the only root. There are no lead or vice directors. Five committees, all
directors on at least one of them, so no unassigned leaves and the Chair shows `5↓`. Order: Audit (rank 0),
Leadership Development…Compensation (rank 1), Nominating (rank 2), then Executive and Risk alphabetically
(rank 3). The Chair also appears inside Nominating as its chair. Members are sorted by label. No row has a
role badge, because every `board_sub_role` is `director` or `chairman`. No Add people.

### 17.2 Executive Management — Google (25 executives, live)

Input: `deptNode.node_id = 'dept__executive_management'`. Example person:

```json
{"label":"[CFO]","layer":1,"sector":"Private","metadata":{"designation":"Senior Vice President, Chief Financial Officer, Alphabet and Google",
 "region":"Global HQ","nlp_method":"llm_leadership_web","leads_department":"Finance & Accounting",
 "leads_department_id":"dept__finance___accounting","linkedin_url":"","linkedin_confidence":"Not Found"}}
```

The CEO has `is_apex: true`. Two people have a non-HQ backend region: "Americas" (layer 1) and "EMEA" (layer 2).

Rendered:

```
EXECUTIVES
Executive Management
14 C-Suite · 1 Executive VP · 5 SVP / Managing Director · 4 VP / Head of · 1 Senior Director / AVP
                                           [Save] [in LinkedIn] [+ Add people] [×]
▼ collapse ▶ expand                            [Region|Country]  3 · 25
🌐 GLOBAL HQ (23) ▾
▼ (S) [CEO]  Chief Executive Officer, Alphabet and Go…   🌐 Global HQ  in ↗ High  🌐 Web  L1 22↓ ⚑
   · (A) [CFO]  Senior Vice President, Chief Financial…
         [▸ Finance & Accounting] [+ Add people]
         🌐 Global HQ   in · not found  🌐 Web                      L1 ⚑
   · … 21 more, flat, in (layer, name) order; YouTube/Cloud CEOs have no ▸ chip …
🌐 AMERICAS (1) ▾
   · (L) [Marketing lead]  Founder, Grow with Google & Chief…  [▸ Marketing][+ Add people] 🌐 Americas … L1 ⚑
🌐 EMEA (1) ▾
   · (D) [EMEA president]  President, Google EMEA  🌐 EMEA  in ↗ Low  🌐 Web  L2 ⚑
                                                        [+ Report missing executive]
```

Why: the primary group is Global HQ, where the apex (`is_apex`) sits. Inside it everyone is flat under the CEO,
so the CEO shows `22↓`. The non-primary groups use `buildLayerTree`, each with a single root. Long titles end in a CSS ellipsis where the row runs out of width; the full title is the tooltip.

### 17.3 Executive Management — demo, no `is_apex`, region `"Global"`

Input (9 people, all layer 1, `region:"Global"`, `nlp_method:"title_keyword"` or `"title_keyword_high"`,
`linkedin_url` set but no confidence):
`Alex Vance — Co-Founder & CEO — San Francisco, USA`, `Elena Voss — CEO — Detroit, USA`,
`Jordan Kim — CTO — San Francisco, USA`, `Kofi Asante — President — Accra, Ghana`,
`Mia Thompson — Chief Revenue Officer — New York, USA`, `Ravi Iyer — CEO — Chennai, India`,
`Susan Hartley — Chief Financial Officer — Chicago, USA`, `Tom Briggs — COO — Detroit, USA`,
`Zara Ahmed — Chief Operating Officer — Doha, Qatar`.

Region mode: one group, `🌐 GLOBAL (9)`. The apex is found by `isCeoTitle`: Alex Vance is the first sorted
title that passes ("co-founder & ceo" is not a co-CEO, and `\bceo\b` matches). The other 8 sit flat under him (`8↓`),
including the two other "CEO"s. Every row shows `🌐 Global`, `📍 {city}` and a blue `in ↗` (Provided style, no
confidence). There is no source badge, because `title_keyword` contains neither "web" nor "llm_leadership". The summary reads "9 C-Suite".

Country mode: `📍 UNITED STATES (6)` (primary: Alex Vance → "USA" in the location, spelled out as "United States"), then
`GHANA (1)`, `INDIA (1)`, `QATAR (1)`. USA keeps Alex as apex with 5 reports; each other group has one root.

### 17.4 Regular department with an inferred line — demo *Finance & Accounting*

Input: `Vivian Chen L7 "Senior Manager, FP&A" (Shanghai, China)`, `Emeka Eze L10 "Apprentice, Finance" (Abuja, Nigeria)`,
`Leah Morrison L10 "Junior Accountant" (Toronto, Canada)`, `Robert Clarke L10 "Secretary General, Ministry of Finance" (London, UK)`.

Region mode:

```
EXECUTIVES
Finance & Accounting
1 Senior Manager · 3 Analyst / Specialist                 [Save] [+ Add people] [×]
▼ collapse ▶ expand  [⋯ some reporting lines inferred]   [Region|Country]  1 · 4
🌐 GLOBAL (4) ▾
▼ (V) Vivian Chen  Senior Manager, FP&A  🌐 Global 📍 Shanghai  in ↗   L7 3↓ ⚑
   · (E) Emeka Eze  Apprentice, Finance      … dashed connector …   L10 ⚑
   · (L) Leah Morrison  Junior Accountant    … dashed …             L10 ⚑
   · (R) Robert Clarke  Secretary General, Ministry of Finan … dashed …  L10 ⚑
```

Layer 10 sits more than one step below 7, so the three rows are `isSkippedLevel`: dashed lines, a faint amber background,
and the legend chip. There is no **in LinkedIn** button (not Board or EM). The header **Add people** is shown.

Country mode → groups `CHINA`, `NIGERIA`, `CANADA`, `UNITED KINGDOM` ("London, UK" → "UK" → "United Kingdom"). Verified in the browser.

### 17.5 Empty department — Google *Finance & Accounting*

`GET /executives` → `{"executives":[],"count":0,"total":0,…}`. Its head sits in Executive Management, a
`dept_primary` boundary. Renders the eyebrow `EXECUTIVES`, the title `Finance & Accounting` in `#3491E8` (the
node's `sector` and `color` are both `""`, so the default accent applies), `[+ Add people] [×]`, then `⊘`, the empty-state copy and
`+ Report a missing executive`.

### 17.6 Error and lost-job cases

- Unknown department → HTTP 404 `{"detail":"Node 'nope' not found."}` → Error state, "This department is no longer in the chart — reload the chart."
- Unknown or evicted job → `{"loaded":false,"executives":[],"count":0,"total":0}` → for a demo chart, auto `/load-demo` and retry; otherwise the Error state, "This chart is no longer on the server — create it again."
- Network failure → Error state, "Connection failed — the backend may be restarting.", with **Try again**. Verified by forcing a failure in the browser.

---

## 18. Quirks and as-is bugs

### 18.1 Fixed (behaviour described elsewhere in this document is the fixed behaviour)

1. **Empty-string colour.** `sector:""`/`color:""` fell through `??` and broke the accent styles. Now `||` falls back to `#3491E8` (drawer) or the drawer colour (person).
2. **Board/EM identified by label in `App`.** Now by `node_id` everywhere.
3. **Header Add people on Executive Management** pre-selected an id that no list contained. Now nothing is pre-selected there, and the subtitle reads "Pick…".
4. **Add-people dialog under the drawer.** Now `zIndex 300`, above the drawer's 200.
5. **Stale response race.** A slow response for one department no longer replaces another's people or error; it is still cached.
6. **Save exported at most 200 people** while the note said "Export CSV for full list". Save now fetches every page first, and the note reads "Save downloads all N".
7. **Board CSV used G-scale labels.** Board rows now say Chair / Lead or Committee Chair / Director (Board).
8. **"Website" badge for web research.** Now "🌐 Web" with a tooltip, and CSV Source "Web".
9. **Errors looked like "no executives".** There is now an error state with the reason and **Try again**.
10. **Blank "Loading…" while closing.** The drawer keeps its last content during the slide-out.
11. **Collapse state leaked between departments.** It resets when another department opens; the Region/Country choice is kept on purpose.
12. **Titles hard-cut at 36 characters.** The full title now shows with CSS ellipsis and a tooltip, as does the name.
13. **`netherlans?` typo; two-letter country codes ignored.** Fixed; UK/GB/US/USA/UAE/KSA are spelled out.
14. **LinkedIn dialog vanished on error** and showed "search still running" for a lost chart. It now stays open with the reason.
15. **Change reports had no chart or person ids and were lost on restart.** They now carry `jobId`/`nodeId`/`deptId` and are stored in Upstash Redis.
16. **No Escape or keyboard support.** Escape closes layers top-down; collapsible rows and headers toggle with Enter/Space and expose `aria-expanded`.
17. **Drawer not refreshed after adding people.** It now re-reads its department after an ingest.
18. **Two spellings of the report button.** Both read "+ Report a missing executive".

### 18.2 Remaining (by design, or needing a product decision)

1. **`scope` is ignored by the backend** (§13.3). The selection is sent and echoed, but the classifier places each person. Making it binding would change where people land, so it needs a decision.
2. `buildLayerTree` splits several same-layer managers' reports evenly, in order (§8.3). These lines are invented. They are marked as inferred only when a layer is skipped.
3. `layerSummary` counts a missing layer as 9 while the tree sorts it as 99. In practice `toPersonNode` always sets a layer, so this does not show.
4. The `pay` chip is never shown in the current pipeline.
5. Tooltips are native `title` attributes; there is no focus trap, and focus does not move into the drawer when it opens.
6. Escape handling relies on a capture-phase listener in `ExecPanel` running before `App`'s. That holds for real key presses, which target the focused element. It does not hold for a synthetic event dispatched on `window` itself, where same-target listeners fire in registration order.

---

## 19. Feature checklist (one line each)

1. Opens when a department node (primary, secondary or tertiary) is clicked; ignores person, ghost, root and synthetic nodes.
2. The same click also expands or collapses that department's branch in the chart.
3. A 360 px panel anchored right over the chart, sliding in and out with `translateX` over 0.22 s ease-out, with a left shadow.
4. Fixed header / legend / scrolling body / fixed footer layout; monospace font.
5. Accent colour from department sector (or colour) tints the title, borders, lines, toggle and group headers.
6. Eyebrow "Board of Directors" for the Board, "Executives" otherwise.
7. Department name title with ellipsis.
8. Layer-distribution summary using Board or standard seniority labels.
9. Pagination note when the server total exceeds the loaded people.
10. **Save**: client-side CSV of every person in the department (fetching past the first 200), 11 columns, a BOM, Board seniority labels for the Board.
11. **in LinkedIn**: opens the LinkedIn profile table (Board and EM only, when people exist).
12. **Add people** in the header (all departments except the Board) → add-people dialog pre-scoped to the department.
13. Close **×**.
14. Loading state.
15. Empty state with guidance copy.
16. Error state with the reason and **Try again** (cache bypassed).
17. Legend row: collapse/expand key, inferred-lines chip with tooltip, Region/Country toggle, `groups · people` count.
18. Grouping by inferred region (backend region, then title keywords, with the Board always HQ).
19. Grouping by inferred country (stored country → location suffix → title keywords → region).
20. The primary group (the apex's) is first; the rest are ordered by size.
21. Collapsible group headers with icon, name, count and caret; the primary group is visually stronger.
22. Board tree: Chair → lead/vice directors → committee groups (fixed rank order, chair first) → unassigned directors; co-chair handling.
23. Committee heading rows with "Chaired by … · N members", collapsible.
24. Executive Management tree: apex from `is_apex`, else CEO-title detection; everyone else flat under the apex; flat list when there is no apex.
25. Other departments: layer-group tree, with even distribution and inferred (dashed) skipped-level lines.
26. Tree connector lines (vertical, ancestor continuation, elbows), solid or dashed.
27. Row collapse/expand by clicking rows that have reports; chevrons ▼ ▶ ·.
28. Initial-letter avatar coloured by person sector.
29. Name (bold at the root), title (36-character cut).
30. Board role badge for vice chair, lead director and committee chairs.
31. "▸ Department" chip for executives who run a department.
32. Row **+ Add people** for that department (Executive Management only).
33. Region chip, city chip, pay chip.
34. LinkedIn badge with confidence colour, tooltip with the matched profile, new-tab link; "not found" text.
35. Source badge: Website / AI Knowledge.
36. Seniority `L{n}` badge and `{n}↓` report count.
37. ⚑ flag per person (hover red) → report dialog pre-filled.
38. Footer / empty-state "Report missing executive" → report dialog in new-executive mode.
39. Report dialog: five change types, a conditional LinkedIn field (required for new executives), contextual notes placeholder, validation, submit, success state with auto-close, error display.
40. LinkedIn dialog: summary, explainer, refresh, copy Markdown, CSV download, confidence filter chips, table, loading and empty states.
41. Add-people dialog: executive and department checklists from `/selectable`, upload file, paste JSON, pull from URL, result note, chart reload.
42. Cached drawer contents per chart in browser history (instant open, offline restore).
43. Background pre-fetch of every department's people (skipped above 10k people).
44. Silent refresh of the open drawer when LinkedIn matching finishes.
45. Lost backend job: auto-reload the demo and retry; uploads show empty.
46. Programmatic close on new chart, restore, history view or reset.
47. History sidebar shows per-chart counts of cached drawer people and departments.
48. Escape closes the top layer: report dialog → Paste-JSON → LinkedIn table → add-people dialog → drawer.
49. Keyboard toggling (Enter/Space) and `aria-expanded` on collapsible rows, committee headings and group headers.
50. A response is applied only if its department is still open.
51. Content kept on screen during the slide-out.
52. Collapse state resets per department; Region/Country choice persists.
53. Drawer refreshes after an ingest.

---

## 20. Platform-specific dependencies

| Dependency | What the drawer needs from it |
|---|---|
| Backend base URL (`VITE_API_URL`, default `/api` proxied to `localhost:8769` in dev) | Every call. It must allow CORS from the frontend's origin; the current backend allows `*`. |
| Server-minted `job_id` (`activeJobIdRef`) | Appended as `?job_id=` to every chart-scoped call. Without it, calls fail or 422. |
| `GET /executives?job_id&dept_id&offset&limit` | `{executives:[node…], count, total, offset, limit}`, sorted by `(layer,label)`, `limit` up to 5000 (used by Save); `{loaded:false,…}` when the job is gone; 404 for an unknown department. The subtree stops at child `dept_primary` boundaries. |
| Graph node schema | `node_id`, `node_type`, `label`, `layer`, `sector`, `color` and the metadata fields in §14.3, in particular `is_apex`, `leads_department(_id)`, `board_sub_role`, `committees`, `linkedin_*`, `nlp_method`, `region`, `location`, `country`. |
| Fixed department ids and labels | `dept__board_of_management` / "Board of Management" and `dept__executive_management` / "Executive Management". |
| Backend seniority scale | Layers 0–10 (G0–G10, per the "Global_Designation_Hierarchy.xlsx" referenced in comments) and Board layers 0/1/2. |
| `GET /linkedin-report?job_id` | `{rows:[…9 keys], columns, markdown, linkedin_done, summary}`. The LinkedIn matching itself needs `PARALLEL_API_KEY` on the server. |
| `POST /report-change` | Accepts `{type, companyName, personName?, personTitle?, currentDept?, jobId?, nodeId?, deptId?, linkedInUrl?, notes?, timestamp}`. Stored in memory and in Upstash Redis (`KV_REST_API_URL`/`KV_REST_API_TOKEN`), list `organogram:change_reports`, capped at 2000. Read via `GET /change-reports?limit=`. |
| `GET /selectable?job_id` | `{executives:[{id,label,title,department,department_id}], departments:[{id,label,people,head_name?,head_title?}]}`. |
| `POST /ingest` (multipart `file`, or `source_url`) and `POST /ingest-json` | Return `{ingested:{added,merged,…}, stats, usage}`; they take `scope` but ignore it. The server-side URL fetch refuses private addresses. |
| `POST /load-demo` | Mints a fresh demo job, used for lost-job recovery. |
| `GET /leadership-ready` poll | Its `linkedin_done` flag triggers the drawer refresh. |
| Browser `localStorage['orgchart_history']` | Per-chart `execCache` (max 12 charts). Quota errors are ignored. |
| Browser APIs | `Blob`, `URL.createObjectURL`, programmatic `<a download>`, `navigator.clipboard.writeText` (LinkedIn dialog), `window.open` via `target=_blank`. |
| `document.body` portals | Report, LinkedIn and Paste-JSON dialogs. |
| Leadership pipeline (Parallel research, LLM classification) | Supplies the Board and EM people, committees, the apex and department heads. Without it the Board and EM panels are empty. |
| Global CSS (`index.css`) | Monospace body font, 6 px scrollbar styling, `box-sizing: border-box`. |
