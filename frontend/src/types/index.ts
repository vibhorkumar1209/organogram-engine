export interface OrgNode {
  node_id: string
  node_type: 'global' | 'region' | 'dept_primary' | 'dept_secondary' | 'dept_tertiary' | 'person' | 'ghost'
  label: string
  layer: number
  sector: string
  color: string
  is_ghost: boolean
  is_synthetic?: boolean   // true for BOD/EM nodes built from public company data
  expanded: boolean
  has_more?: boolean
  metadata: Record<string, string | number>
  children?: OrgNode[]
}

export interface PublicCompanyPerson {
  name: string
  title: string
  age?: number
  pay?: number
  layer: number
}

export interface PublicCompanyData {
  ticker:      string | null
  domain:      string | null
  companyName: string
  industry:    string
  sector:      string
  website:     string
  pageUrl:     string | null    // leadership page URL found by scraper
  board:       PublicCompanyPerson[]
  executives:  PublicCompanyPerson[]
  tickerError: string | null
  webError:    string | null
}

export interface GraphEdge {
  source: string
  target: string
}

export interface GraphData {
  nodes: OrgNode[]
  edges: GraphEdge[]
  stats: Stats
}

export interface Stats {
  total_nodes: number
  total_edges: number
  people_nodes: number
  ghost_nodes: number
  max_depth: number
}

export type ViewMode = 'tree' | 'radial'
