"""
Universal Organogram Engine - Structural Engine
Builds a Directed Acyclic Graph (DAG) from classified records.
Inserts Ghost Nodes to maintain 10-layer depth continuity.
Supports recursive CTE-style drill-down queries.
"""

import json
import re
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import networkx as nx

from inference_logic import ClassifiedRecord, InferenceEngine


# ─────────────────────────────────────────────
# NODE TYPES
# ─────────────────────────────────────────────
NODE_GLOBAL    = "global"
NODE_REGION    = "region"
NODE_SECTOR    = "sector"
NODE_DEPT_P    = "dept_primary"
NODE_DEPT_S    = "dept_secondary"
NODE_DEPT_T    = "dept_tertiary"
NODE_PERSON    = "person"
NODE_GHOST     = "ghost"

# ─────────────────────────────────────────────
# CANONICAL DEPARTMENT NAME NORMALIZATION
# ─────────────────────────────────────────────

# Accepted top-level (primary) department names.
# Any name NOT in this set is treated as either a secondary/team name
# ── Fix 3: Sub-department (dept_secondary) canonical hierarchy ───────────────
# Maps raw/vendor dept_secondary values → rational sub-department names
# that fit within the expected parent department hierarchy.
#
# Sales parent should produce: Account Management | New Business | Pre-Sales |
#   Sales Operations | Channel & Partners | Sales & Account Management |
#   Sales & Commercial | Inside Sales
# Marketing parent should produce: Brand | Digital Marketing | Content |
#   Performance Marketing | Product Marketing | Events | Market Research
# Finance parent: FP&A | Accounting | Treasury | Tax | Internal Audit |
#   Investor Relations | Financial Reporting
# etc.
#
# Any sub-dept that doesn't fit a rational model is merged into its parent.
_SUBDEPT_REMAP: dict[str, str] = {
    # Programme / Project / PMO — never a sub-dept name
    "programme":                    "",   # merge into parent
    "programme management":         "",
    "programme delivery":           "",
    "project management":           "",
    "project office":               "",
    "pmo":                          "",
    "delivery":                     "",
    # Generic / non-rational
    "general":                      "",
    "admin":                        "",
    "administration":               "",
    "support":                      "",
    "general management":           "",
    # Sales sub-depts
    "account management":           "Account Management",
    "key accounts":                 "Account Management",
    "strategic accounts":           "Account Management",
    "enterprise accounts":          "Account Management",
    "named accounts":               "Account Management",
    "new business":                 "New Business Development",
    "new business development":     "New Business Development",
    "business development":         "New Business Development",
    "pre-sales":                    "Pre-Sales & Solutioning",
    "presales":                     "Pre-Sales & Solutioning",
    "solution engineering":         "Pre-Sales & Solutioning",
    "sales operations":             "Sales Operations",
    "sales ops":                    "Sales Operations",
    "revenue operations":           "Sales Operations",
    "revops":                       "Sales Operations",
    "sales enablement":             "Sales Operations",
    "inside sales":                 "Inside Sales",
    "telesales":                    "Inside Sales",
    "channel":                      "Channel & Partners",
    "channel management":           "Channel & Partners",
    "channel sales":                "Channel & Partners",
    "partner management":           "Channel & Partners",
    "partnerships":                 "Channel & Partners",
    "commercial":                   "Sales & Commercial",
    "sales & commercial":           "Sales & Commercial",
    "sales & account management":   "Sales & Account Management",
    # Marketing sub-depts
    "brand":                        "Brand & Communications",
    "brand management":             "Brand & Communications",
    "brand & marketing":            "Brand & Communications",
    "digital marketing":            "Digital & Performance Marketing",
    "performance marketing":        "Digital & Performance Marketing",
    "paid media":                   "Digital & Performance Marketing",
    "seo":                          "Digital & Performance Marketing",
    "content":                      "Content & Creative",
    "content marketing":            "Content & Creative",
    "creative":                     "Content & Creative",
    "design":                       "Content & Creative",
    "product marketing":            "Product Marketing",
    "events":                       "Events & Sponsorship",
    "market research":              "Market Research & Insights",
    "consumer insights":            "Market Research & Insights",
    "trade marketing":              "Trade Marketing",
    # Finance sub-depts
    "fp&a":                         "FP&A",
    "financial planning":           "FP&A",
    "budgeting":                    "FP&A",
    "forecasting":                  "FP&A",
    "accounting":                   "Accounting & Reporting",
    "financial reporting":          "Accounting & Reporting",
    "general ledger":               "Accounting & Reporting",
    "treasury":                     "Treasury",
    "cash management":              "Treasury",
    "tax":                          "Tax",
    "direct tax":                   "Tax",
    "indirect tax":                 "Tax",
    "internal audit":               "Internal Audit",
    "audit":                        "Internal Audit",
    "investor relations":           "Investor Relations",
    # HR sub-depts
    "talent acquisition":           "Talent Acquisition",
    "recruitment":                  "Talent Acquisition",
    "learning & development":       "Learning & Development",
    "l&d":                          "Learning & Development",
    "training":                     "Learning & Development",
    "compensation & benefits":      "Compensation & Benefits",
    "c&b":                          "Compensation & Benefits",
    "hrbp":                         "HR Business Partnering",
    "hr business partner":          "HR Business Partnering",
    "people analytics":             "People Analytics",
    "workforce planning":           "People Analytics",
    # Technology sub-depts
    "infrastructure":               "IT Infrastructure",
    "it infrastructure":            "IT Infrastructure",
    "cybersecurity":                "Information Security",
    "security":                     "Information Security",
    "information security":         "Information Security",
    "application development":      "Application Development",
    "software development":         "Application Development",
    "enterprise architecture":      "Enterprise Architecture",
    "data engineering":             "Data Engineering",
    "data science":                 "Data & Analytics",
    "analytics":                    "Data & Analytics",
    # Operations sub-depts
    "supply chain":                 "Supply Chain",
    "logistics":                    "Logistics",
    "warehousing":                  "Logistics",
    "quality":                      "Quality & Compliance",
    "quality assurance":            "Quality & Compliance",
    "hse":                          "Health, Safety & Environment",
    "health & safety":              "Health, Safety & Environment",
    "maintenance":                  "Asset & Maintenance",
    "facilities":                   "Facilities Management",
}

# or an industry-specific label and gets remapped via _DEPT_REMAP.
_CANONICAL_PRIMARY: frozenset[str] = frozenset({
    "Board of Management",
    "Executive Management",
    "Finance",
    "Human Resources",
    "People & Culture",
    "Legal",
    "Legal & Compliance",
    "Risk & Compliance",
    "Risk Management",
    "Compliance",
    "Technology",
    "Information Technology",
    "Engineering",
    "Data & Analytics",
    "Product Management",
    "Operations",
    "Supply Chain",
    "Manufacturing",
    "Sales",
    "Commercial",
    "Sales & Commercial",
    "Marketing",
    "Customer Experience",
    "Customer Success",
    "Strategy",
    "Corporate Development",
    "Communications",
    "Sustainability",
    "Research & Development",
    "Procurement",
    "Actuarial",
    "Underwriting",
    "Claims",
    "Investment Management",
    "Treasury",
    "Audit",
    "Internal Audit",
})

# Map non-canonical / industry-specific / generic dept names → canonical.
# Keys are lowercase stripped strings.
_DEPT_REMAP: dict[str, str] = {
    # Generic catch-alls
    "general":                          "Operations",
    "general management":               "Operations",
    "administration":                   "Operations",
    "corporate":                        "Executive Management",
    "corporate & executive":            "Executive Management",
    "executive":                        "Executive Management",
    "ceo office":                       "Executive Management",
    "c-suite":                          "Executive Management",
    "managing directors":               "Executive Management",
    "president / evp":                  "Executive Management",
    # HR variants
    "hr":                               "Human Resources",
    "people":                           "Human Resources",
    "talent":                           "Human Resources",
    "talent management":                "Human Resources",
    "talent & culture":                 "People & Culture",
    "people operations":                "Human Resources",
    "workforce":                        "Human Resources",
    "learning & development":           "Human Resources",
    # Finance variants
    "financial planning & analysis":    "Finance",
    "fp&a":                             "Finance",
    "corporate finance":                "Finance",
    "financial services":               "Finance",
    "accounting":                       "Finance",
    "financial advisory":               "Finance",
    "deal advisory":                    "Corporate Development",
    "m&a":                              "Corporate Development",
    "investor relations":               "Finance",
    # Tech variants
    "it":                               "Technology",
    "information technology":           "Technology",
    "digital":                          "Technology",
    "software":                         "Engineering",
    "data":                             "Data & Analytics",
    "analytics":                        "Data & Analytics",
    "data science":                     "Data & Analytics",
    # Sales variants
    "key accounts":                     "Sales",
    "key account management":           "Sales",
    "enterprise sales":                 "Sales",
    "inside sales":                     "Sales",
    "field sales":                      "Sales",
    "retail sales":                     "Sales",
    "channel sales":                    "Sales",
    "business development":             "Sales",
    "revenue":                          "Sales",
    "commercial & sales":               "Sales & Commercial",
    "sales & distribution":             "Sales",
    "bancassurance":                    "Sales",
    # Marketing variants
    "brand management":                 "Marketing",
    "brand & marketing":                "Marketing",
    "trade marketing":                  "Marketing",
    "shopper marketing":                "Marketing",
    "consumer insights":                "Marketing",
    "digital marketing":                "Marketing",
    "performance marketing":            "Marketing",
    "product marketing":                "Marketing",
    "marketing & brand":                "Marketing",
    # Operations / industry-specific
    "vehicle sales":                    "Sales",
    "upstream":                         "Operations",
    "downstream":                       "Operations",
    "e&p":                              "Operations",
    "exploration":                      "Operations",
    "drilling":                         "Operations",
    "refining":                         "Operations",
    "field development":                "Operations",
    "mining operations":                "Operations",
    "plant operations":                 "Operations",
    "service delivery":                 "Operations",
    "facilities":                       "Operations",
    "real estate":                      "Operations",
    "quality":                          "Operations",
    "quality assurance":                "Operations",
    "quality control":                  "Operations",
    "health & safety":                  "Operations",
    "hse":                              "Operations",
    "ehs":                              "Operations",
    "maintenance":                      "Operations",
    "technical services":               "Operations",
    "field services":                   "Operations",
    "service engineering":              "Engineering",
    # Programme / Project — Fix 4: "Programme" is not a department
    "programme":                        "Strategy",
    "programme management":             "Strategy",
    "programme management office":      "Strategy",
    "programme delivery":               "Strategy",
    "project management":               "Strategy",
    "project management office":        "Strategy",
    "pmo":                              "Strategy",
    "epmo":                             "Strategy",
    "delivery":                         "Operations",
    "delivery management":              "Operations",
    "project delivery":                 "Operations",
    "project office":                   "Strategy",
    # Admin / General — non-rational catch-alls
    "admin":                            "Operations",
    "administration":                   "Operations",
    "support":                          "Operations",
    "shared services":                  "Operations",
    "business support":                 "Operations",
    "back office":                      "Operations",
    "office management":                "Operations",
    # Supply chain
    "logistics":                        "Supply Chain",
    "warehousing":                      "Supply Chain",
    "distribution":                     "Supply Chain",
    "sourcing":                         "Procurement",
    "indirect procurement":             "Procurement",
    "direct procurement":               "Procurement",
    # Legal
    "legal & regulatory":               "Legal & Compliance",
    "regulatory":                       "Legal & Compliance",
    "regulatory affairs":               "Legal & Compliance",
    "compliance & legal":               "Legal & Compliance",
    "governance":                       "Legal & Compliance",
    "governance risk & compliance":     "Risk & Compliance",
    "grc":                              "Risk & Compliance",
    # Risk
    "risk":                             "Risk Management",
    "credit risk":                      "Risk Management",
    "market risk":                      "Risk Management",
    "operational risk":                 "Risk Management",
    "enterprise risk":                  "Risk Management",
    "risk advisory":                    "Risk Management",
    # R&D
    "r&d":                              "Research & Development",
    "innovation":                       "Research & Development",
    "product & engineering":            "Engineering",
    "research & development":           "Research & Development",
    # Customer
    "customer service":                 "Customer Experience",
    "customer support":                 "Customer Experience",
    "client services":                  "Customer Experience",
    "client success":                   "Customer Success",
    "after sales":                      "Customer Experience",
    "after-sales":                      "Customer Experience",
    "post sales":                       "Customer Experience",
    "service":                          "Customer Experience",
    # Strategy / Corporate
    "corporate strategy":               "Strategy",
    "group strategy":                   "Strategy",
    "strategy & corporate development": "Strategy",
    "business strategy":                "Strategy",
    "transformation":                   "Strategy",
    "business transformation":          "Strategy",
    "digital transformation":           "Strategy",
    "change management":                "Strategy",
    # Communications / PR
    "public relations":                 "Communications",
    "pr":                               "Communications",
    "corporate communications":         "Communications",
    "internal communications":          "Communications",
    "external affairs":                 "Communications",
    "public affairs":                   "Communications",
    # Sustainability / ESG
    "esg":                              "Sustainability",
    "sustainability & esg":             "Sustainability",
    "environment":                      "Sustainability",
    "csr":                              "Sustainability",
}


def _canonical_dept(dept_primary: str, layer: int) -> str:
    """
    Return a canonical primary department name.

    Layer overrides take priority:
      L0  → Board of Management (always)
      L1  → Executive Management (C-Suite)
      L2  → Executive Management (MD / EVP level)

    For L3+, remaps non-canonical / industry-specific / generic names to the
    closest canonical function. Falls back to "Operations" when nothing matches.
    """
    # Layer-based hard overrides (most important — independent of NLP dept)
    if layer == 0:
        return "Board of Management"
    if layer in (1, 2):
        return "Executive Management"

    # Attempt remap via exact lookup (case-insensitive)
    key = dept_primary.strip().lower()
    if key in _DEPT_REMAP:
        return _DEPT_REMAP[key]

    # Accept if it's already canonical
    for canon in _CANONICAL_PRIMARY:
        if dept_primary.strip().lower() == canon.lower():
            return canon

    # Partial-word remap for compound names not in the explicit map
    for banned_key, replacement in _DEPT_REMAP.items():
        if banned_key in key:
            return replacement

    # Unknown but non-empty name — accept as-is (analyst wrote something real)
    if dept_primary.strip():
        return dept_primary.strip()

    # Ultimate fallback
    return "Operations"


def _canonical_subdept(dept_secondary: str) -> str:
    """
    Normalize a dept_secondary value using _SUBDEPT_REMAP.

    Returns:
      - The remapped canonical sub-dept name (e.g. "Account Management")
      - "" if the sub-dept should be merged into the parent (e.g. "Programme")
      - The original value stripped if it's not in the remap (pass-through)
    """
    if not dept_secondary or not dept_secondary.strip():
        return ""
    key = dept_secondary.strip().lower()
    if key in _SUBDEPT_REMAP:
        return _SUBDEPT_REMAP[key]   # "" means merge into parent
    # Partial match for compound names not explicitly listed
    for remap_key, remap_val in _SUBDEPT_REMAP.items():
        if remap_key and remap_key in key:
            return remap_val
    return dept_secondary.strip()


# Department display order — lower number = shown first
DEPT_PRIMARY_ORDER: dict[str, int] = {
    "board of directors":    0,
    "board":                 0,
    "executive management":  1,
    "ceo office":            2,
    "c-suite":               2,
    "corporate":             3,
    "finance":              10,
    "human resources":      11,
    "hr":                   11,
    "legal":                12,
    "risk & compliance":    13,
    "risk":                 13,
    "information technology": 14,
    "it":                   14,
    "data & analytics":     15,
    "data":                 15,
    "engineering":          16,
    "engineering & r&d":    16,
    "operations":           17,
    "manufacturing":        18,
    "supply chain":         19,
    "sales":                20,
    "marketing":            21,
    "customer service":     22,
    "sustainability":       30,
}


SECTOR_COLORS = {
    "Automotive": "#F59E0B",
    "Govt":       "#3B82F6",
    "NGO":        "#10B981",
    "Startup":    "#8B5CF6",
    "Public":     "#06B6D4",
    "Private":    "#64748B",
}


class OrganogramDAG:
    """Directed Acyclic Graph representing the full organogram."""

    def __init__(self, company_name: str = "Organization"):
        self.G = nx.DiGraph()
        self.company_name = company_name
        self._ensure_root()

    def _ensure_root(self):
        root_id = "root_global"
        if root_id not in self.G:
            self.G.add_node(root_id, **{
                "node_id":   root_id,
                "node_type": NODE_GLOBAL,
                "label":     self.company_name,
                "layer":     -1,
                "sector":    "All",
                "color":     "#1E293B",
                "is_ghost":  False,
                "expanded":  False,
                "metadata":  {},
            })
        return root_id

    def _node_id(self, *parts: str) -> str:
        clean = [re.sub(r"[^a-z0-9_]", "_", p.lower().strip())
                 for p in parts if p]
        return "__".join(clean)

    def _ensure_node(self, _nid: str, **attrs) -> str:
        if _nid not in self.G:
            self.G.add_node(_nid, **attrs)
        return _nid

    def _ensure_edge(self, parent: str, child: str):
        if not self.G.has_edge(parent, child):
            self.G.add_edge(parent, child)

    # ─── Build region layer ───────────────────
    def ensure_region(self, region: str, sector: str) -> str:
        rid = self._node_id("region", region)
        self._ensure_node(rid, **{
            "node_id":   rid,
            "node_type": NODE_REGION,
            "label":     region,
            "layer":     0,
            "sector":    sector,
            "color":     SECTOR_COLORS.get(sector, "#64748B"),
            "is_ghost":  False,
            "expanded":  False,
            "metadata":  {"region": region},
        })
        self._ensure_edge("root_global", rid)
        return rid

    # ─── Build department layers ──────────────
    def ensure_department(self, region: str, sector: str,
                           dept_p: str, dept_s: str, dept_t: str
                           ) -> str:
        """
        Create department hierarchy nodes (1–3 levels) and return the
        deepest created node ID (leaf). Redundant nodes are skipped:
        - Secondary is skipped when empty or identical to primary.
        - Tertiary is skipped when empty or identical to secondary/primary.
        """
        region_id = self.ensure_region(region, sector)
        color = SECTOR_COLORS.get(sector, "#64748B")

        # ── Primary dept (always created) ───────────────────────────────
        dp_id = self._node_id("dept", region, dept_p)
        self._ensure_node(dp_id, **{
            "node_id":   dp_id,
            "node_type": NODE_DEPT_P,
            "label":     dept_p,
            "layer":     1,
            "sector":    sector,
            "color":     color,
            "is_ghost":  False,
            "expanded":  False,
            "metadata":  {"dept_primary": dept_p, "region": region},
        })
        self._ensure_edge(region_id, dp_id)
        leaf = dp_id

        # ── Secondary dept (skip if empty or same as primary) ───────────
        effective_s = dept_s if (dept_s and dept_s.lower() != dept_p.lower()) else ""
        if effective_s:
            ds_id = self._node_id("dept", region, dept_p, dept_s)
            self._ensure_node(ds_id, **{
                "node_id":   ds_id,
                "node_type": NODE_DEPT_S,
                "label":     dept_s,
                "layer":     2,
                "sector":    sector,
                "color":     color,
                "is_ghost":  False,
                "expanded":  False,
                "metadata":  {"dept_primary": dept_p, "dept_secondary": dept_s},
            })
            self._ensure_edge(dp_id, ds_id)
            leaf = ds_id

            # ── Tertiary dept (skip if empty or mirrors secondary/primary) ──
            effective_t = (
                dept_t
                if (dept_t
                    and dept_t.lower() != dept_s.lower()
                    and dept_t.lower() != dept_p.lower())
                else ""
            )
            if effective_t:
                dt_id = self._node_id("dept", region, dept_p, dept_s, dept_t)
                self._ensure_node(dt_id, **{
                    "node_id":   dt_id,
                    "node_type": NODE_DEPT_T,
                    "label":     dept_t,
                    "layer":     3,
                    "sector":    sector,
                    "color":     color,
                    "is_ghost":  False,
                    "expanded":  False,
                    "metadata":  {"dept_tertiary": dept_t},
                })
                self._ensure_edge(ds_id, dt_id)
                leaf = dt_id

        return leaf

    # ─── Department sort key (for ordered get_subtree output) ────────
    def _dept_sort_key(self, nid: str) -> tuple:
        attrs = self.G.nodes.get(nid, {})
        node_type = attrs.get("node_type", "")
        label = attrs.get("label", "").lower().rstrip(" ✦")

        if attrs.get("is_ghost"):
            return (900, label)

        if node_type in (NODE_DEPT_P, NODE_REGION):
            return (DEPT_PRIMARY_ORDER.get(label, 50), label)

        if node_type in (NODE_DEPT_S, NODE_DEPT_T):
            return (50, label)

        if node_type == NODE_PERSON:
            return (100 + attrs.get("layer", 99), label)

        return (500, label)

    # ─── Insert person with ghost-node bridging ─
    def insert_person(self, rec: ClassifiedRecord):
        # Enforce canonical department names and layer-based overrides
        # (Board L0 → "Board of Management", C-Suite L1-2 → "Executive Management")
        dept_p = _canonical_dept(rec.dept_primary, rec.layer)
        dept_s = _canonical_subdept(rec.dept_secondary if rec.layer > 2 else "")
        dept_t = rec.dept_tertiary  if rec.layer > 2 else ""
        # If sub-dept remapped to "" (merge into parent) treat as no sub-dept
        if not dept_s:
            dept_t = ""

        leaf_dept_id = self.ensure_department(
            rec.region, rec.sector,
            dept_p, dept_s, dept_t
        )

        person_id = rec.id
        self._ensure_node(person_id, **{
            "node_id":    person_id,
            "node_type":  NODE_PERSON,
            "label":      rec.full_name,
            "layer":      rec.layer,
            "sector":     rec.sector,
            "color":      SECTOR_COLORS.get(rec.sector, "#64748B"),
            "is_ghost":   False,
            "expanded":   False,
            "metadata": {
                "designation":    rec.designation,
                "company":        rec.company,
                "linkedin_url":   rec.linkedin_url,
                "location":       rec.location,
                "dept_primary":   dept_p,
                "dept_secondary": dept_s,
                "nlp_confidence": round(getattr(rec, "nlp_confidence", 0.0), 2),
                "nlp_industry":   getattr(rec, "nlp_industry", "generic"),
                "nlp_method":     getattr(rec, "nlp_method", "fallback"),
            },
        })

        # Build ghost chain from layer 4 → rec.layer
        self._insert_with_ghosts(leaf_dept_id, person_id, rec)

    def _insert_with_ghosts(self, dept_node: str,
                             person_node: str,
                             rec: ClassifiedRecord):
        """
        Bridge the department node (depth 3) to the person node
        by inserting Ghost Nodes for any missing intermediate layers.
        Person layer range: 4–10 corresponds to org depth 4–10.
        """
        person_layer = rec.layer
        start_layer  = 4   # first employee layer after dept tree

        if person_layer <= start_layer:
            # Direct link from dept tertiary → person
            self._ensure_edge(dept_node, person_node)
            return

        # Need to create ghost chain for layers start_layer..(person_layer-1)
        # Labels reflect: BOD(0) → Exec Mgmt(1) → SVP/EVP(2) → VP(3) → Dir(4-5) → Mgr(6-7) → Staff(8-10)
        GHOST_LABELS = {
            0:  "Board of Directors",
            1:  "Executive Management",
            2:  "SVP / EVP",
            3:  "VP / Divisional Head",
            4:  "Senior Director",
            5:  "Director / Head",
            6:  "Senior Manager",
            7:  "Manager / Lead",
            8:  "Senior Contributor",
            9:  "Contributor",
            10: "Entry Level",
        }

        prev = dept_node
        for ghost_layer in range(start_layer, person_layer):
            ghost_id = self._node_id(
                "ghost", dept_node,
                rec.dept_primary, rec.dept_secondary,
                str(ghost_layer)
            )
            if ghost_id not in self.G:
                label = GHOST_LABELS.get(ghost_layer,
                                         f"Layer {ghost_layer} — Pending Data")
                self._ensure_node(ghost_id, **{
                    "node_id":   ghost_id,
                    "node_type": NODE_GHOST,
                    "label":     f"{label} ✦",
                    "layer":     ghost_layer,
                    "sector":    rec.sector,
                    "color":     "#374151",
                    "is_ghost":  True,
                    "expanded":  False,
                    "metadata": {
                        "reason": "Auto-generated placeholder — data pending",
                        "dept_primary": rec.dept_primary,
                    },
                })
                self._ensure_edge(prev, ghost_id)
            prev = ghost_id

        self._ensure_edge(prev, person_node)

    # ─── Recursive CTE-style drill-down ──────
    def get_subtree(self, node_id: str, max_depth: int = 20) -> dict:
        """
        Returns a nested dict representing the subtree rooted at node_id,
        up to max_depth levels deep — like a recursive CTE.
        Children at each level are sorted:
          Board of Directors → Executive Management → CEO Office →
          functional depts (Finance, HR, IT …) → people (by layer) → ghosts.
        """
        if node_id not in self.G:
            return {}

        def recurse(nid: str, depth: int) -> dict:
            attrs = dict(self.G.nodes[nid])
            raw_children = list(self.G.successors(nid))
            if depth >= max_depth:
                return {**attrs, "children": [], "has_more": len(raw_children) > 0}
            children = sorted(raw_children, key=self._dept_sort_key)
            child_nodes = [recurse(c, depth + 1) for c in children]
            return {**attrs, "children": child_nodes}

        return recurse(node_id, 0)

    def get_flat_nodes(self) -> list[dict]:
        return [dict(self.G.nodes[n]) for n in self.G.nodes]

    def get_edges(self) -> list[dict]:
        return [{"source": u, "target": v}
                for u, v in self.G.edges]

    def stats(self) -> dict:
        return {
            "total_nodes": self.G.number_of_nodes(),
            "total_edges": self.G.number_of_edges(),
            "people_nodes": sum(
                1 for n, d in self.G.nodes(data=True)
                if d.get("node_type") == NODE_PERSON
            ),
            "ghost_nodes": sum(
                1 for n, d in self.G.nodes(data=True)
                if d.get("is_ghost")
            ),
            "max_depth": self._max_depth(),
        }

    def _max_depth(self) -> int:
        try:
            return nx.dag_longest_path_length(self.G)
        except Exception:
            return -1


# ─────────────────────────────────────────────
# SQLITE PERSISTENCE
# ─────────────────────────────────────────────

class OrganogramDB:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id   TEXT PRIMARY KEY,
            node_type TEXT,
            label     TEXT,
            layer     INTEGER,
            sector    TEXT,
            color     TEXT,
            is_ghost  INTEGER,
            metadata  TEXT
        );

        CREATE TABLE IF NOT EXISTS edges (
            parent_id TEXT,
            child_id  TEXT,
            PRIMARY KEY (parent_id, child_id),
            FOREIGN KEY (parent_id) REFERENCES nodes(node_id),
            FOREIGN KEY (child_id)  REFERENCES nodes(node_id)
        );

        CREATE INDEX IF NOT EXISTS idx_edges_parent ON edges(parent_id);
        CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(node_type);
        CREATE INDEX IF NOT EXISTS idx_nodes_layer  ON nodes(layer);
        """)
        self.conn.commit()

    def upsert_dag(self, dag: OrganogramDAG):
        for node_id, attrs in dag.G.nodes(data=True):
            self.conn.execute("""
                INSERT OR REPLACE INTO nodes
                (node_id, node_type, label, layer, sector, color, is_ghost, metadata)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                node_id,
                attrs.get("node_type"),
                attrs.get("label"),
                attrs.get("layer"),
                attrs.get("sector"),
                attrs.get("color"),
                1 if attrs.get("is_ghost") else 0,
                json.dumps(attrs.get("metadata", {})),
            ))

        for u, v in dag.G.edges:
            self.conn.execute("""
                INSERT OR IGNORE INTO edges (parent_id, child_id)
                VALUES (?,?)
            """, (u, v))

        self.conn.commit()

    def recursive_subtree(self, root_id: str) -> list[dict]:
        """
        True recursive CTE — returns all descendants of root_id.
        """
        cur = self.conn.execute("""
            WITH RECURSIVE subtree(node_id, depth) AS (
                SELECT ?, 0
                UNION ALL
                SELECT e.child_id, subtree.depth + 1
                FROM edges e
                JOIN subtree ON subtree.node_id = e.parent_id
                WHERE subtree.depth < 20
            )
            SELECT n.*, s.depth
            FROM subtree s
            JOIN nodes n ON n.node_id = s.node_id
            ORDER BY s.depth, n.label
        """, (root_id,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["metadata"] = json.loads(r.get("metadata") or "{}")
        return rows

    def search(self, query: str) -> list[dict]:
        q = f"%{query.lower()}%"
        cur = self.conn.execute("""
            SELECT * FROM nodes
            WHERE lower(label) LIKE ?
               OR lower(sector) LIKE ?
               OR lower(node_type) LIKE ?
        """, (q, q, q))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─────────────────────────────────────────────
# BUILDER
# ─────────────────────────────────────────────

import re

def build_from_records(records: list[dict],
                       company_name: str = "Organization",
                       db_path: str = ":memory:"
                       ) -> tuple[OrganogramDAG, OrganogramDB]:
    engine = InferenceEngine()
    classified = engine.classify_all(records)

    dag = OrganogramDAG(company_name=company_name)
    for rec in classified:
        dag.insert_person(rec)

    # Always enrich with LLM-sourced Board & Executive Management,
    # regardless of what was in the uploaded file.
    _enrich_with_llm_leadership(dag, classified, company_name)

    db = OrganogramDB(db_path=db_path)
    db.upsert_dag(dag)

    return dag, db


# ─────────────────────────────────────────────
# LLM LEADERSHIP ENRICHMENT
# ─────────────────────────────────────────────

def _name_key(name: str) -> str:
    """Normalised dedup key: first two words lowercase, letters only."""
    words = re.sub(r"[^a-z ]", "", name.lower()).split()
    return " ".join(words[:2])


def _enrich_with_llm_leadership(
    dag: OrganogramDAG,
    classified: list,
    company_name: str,
) -> None:
    """
    For every distinct company in the dataset, fetch Board of Directors and
    Executive Management via Claude and inject them into the DAG.

    - Runs unconditionally after every upload / demo load.
    - Deduplicates against names already present in the DAG.
    - Board members  → layer 0, dept_primary = "Board of Management"
    - C-Suite execs  → layer 1, dept_primary = "Executive Management"
    - Injected nodes carry nlp_method = "llm_leadership" for provenance.
    """
    try:
        from llm_fallback import llm_fetch_leadership
    except ImportError:
        return

    # ── Collect unique companies ──────────────────────────────────────
    companies: dict[str, dict] = {}   # company_name → {region, sector}

    # 1. Declared company name (always included)
    companies[company_name.strip()] = {"region": "Global HQ", "sector": "Private"}

    # 2. Companies found in the uploaded records
    for rec in classified:
        co = (getattr(rec, "company", "") or "").strip()
        if co and co not in companies:
            companies[co] = {
                "region": getattr(rec, "region", "Global HQ") or "Global HQ",
                "sector": getattr(rec, "sector", "Private")  or "Private",
            }

    # Fill region/sector for the declared company from existing classified records
    if company_name in companies and classified:
        # Use the most common region among all records
        from collections import Counter
        region_counts = Counter(
            getattr(r, "region", "Global HQ") or "Global HQ" for r in classified
        )
        sector_counts = Counter(
            getattr(r, "sector", "Private") or "Private" for r in classified
        )
        companies[company_name]["region"] = region_counts.most_common(1)[0][0]
        companies[company_name]["sector"] = sector_counts.most_common(1)[0][0]

    # ── Build existing-name index for deduplication ───────────────────
    existing_keys: set[str] = set()
    for nid in dag.G.nodes:
        attrs = dag.G.nodes[nid]
        if attrs.get("node_type") == "person":
            existing_keys.add(_name_key(attrs.get("label", "")))

    # ── Fetch and inject per company ─────────────────────────────────
    for co, ctx in companies.items():
        leadership = llm_fetch_leadership(co)
        region = ctx["region"]
        sector = ctx["sector"]

        injections: list[tuple[int, str, str, str]] = []  # (layer, name, title, dept)
        for person in leadership.get("board", []):
            injections.append((0, person["name"], person["title"], "Board of Management"))
        for person in leadership.get("executives", []):
            injections.append((1, person["name"], person["title"], "Executive Management"))

        for layer, name, title, dept_primary in injections:
            key = _name_key(name)
            if key in existing_keys:
                continue   # already in DAG from uploaded data
            existing_keys.add(key)

            # Build a minimal ClassifiedRecord and insert
            from inference_logic import ClassifiedRecord
            rec = ClassifiedRecord(
                id=f"llm_{uuid.uuid4().hex[:12]}",
                full_name=name,
                designation=title,
                company=co,
                linkedin_url="",
                location="",
                sector=sector,
                region=region,
                layer=layer,
                dept_primary=dept_primary,
                dept_secondary="",
                dept_tertiary="",
                nlp_confidence=0.9,
                nlp_industry="llm",
                nlp_method="llm_leadership",
            )
            dag.insert_person(rec)


# ─────────────────────────────────────────────
# CLI TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    data_path = Path(__file__).parent / "test_data.json"
    with open(data_path) as f:
        records = json.load(f)

    dag, db = build_from_records(records, company_name="Global Conglomerate Inc.")
    stats = dag.stats()
    print(f"\n{'='*60}")
    print("DAG Statistics")
    print(f"{'='*60}")
    for k, v in stats.items():
        print(f"  {k:<20}: {v}")

    subtree = db.recursive_subtree("root_global")
    print(f"\nRecursive CTE returned {len(subtree)} nodes from root.\n")

    results = db.search("engineering")
    print(f"Search 'engineering' → {len(results)} matches")
