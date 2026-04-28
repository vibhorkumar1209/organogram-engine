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
        leaf_dept_id = self.ensure_department(
            rec.region, rec.sector,
            rec.dept_primary, rec.dept_secondary, rec.dept_tertiary
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
                "dept_primary":   rec.dept_primary,
                "dept_secondary": rec.dept_secondary,
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

    db = OrganogramDB(db_path=db_path)
    db.upsert_dag(dag)

    return dag, db


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
