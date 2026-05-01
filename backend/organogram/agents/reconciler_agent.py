"""
Agent 4 — Reconciler Agent.

Responsibility: build the canonical hierarchy from
  - AuthoritativeLeader records (Levels 1–2, immutable)
  - NormalizedPerson records (Levels 3–10, from LinkedIn NLP)

Logic is FULLY DETERMINISTIC. No LLM calls.
Unparented persons attach to a synthetic "{Function} — Unassigned" branch.
"""
from __future__ import annotations
from datetime import date
from typing import Optional
import uuid

from ..schemas.types import (
    AuthoritativeLeader, NormalizedPerson,
    CanonicalNode, CanonicalOrganogram,
)
from ..utils.rule_loader import RuleLibrary, INDUSTRY_TO_ARCHETYPE


# Industries that get the multi-view (Functional + Geographic + BU) treatment
# when client_archetype = "Enterprise" (master prompt §8.1)
MULTI_VIEW_INDUSTRIES = {
    "Automotive",
    "Industrial Manufacturing — Discrete",
    "Industrial Manufacturing - Discrete",
    "Industrial Manufacturing — Process",
    "Industrial Manufacturing - Process",
    "IT Services",
    "Pharmaceuticals / Life Sciences",
    # Specialty Chemicals falls under Process; treated multi-view if client is Enterprise
}


def _new_id(prefix: str = "p") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# Map common board/exco titles to functions for placement under the CEO
TITLE_FUNCTION_HINTS = {
    "ceo": "Strategy", "chief executive": "Strategy", "president": "Strategy",
    "chairman": "Strategy", "director": "Strategy",
    "cfo": "Finance", "chief financial": "Finance",
    "coo": "Operations", "chief operating": "Operations",
    "cmo": "Marketing", "chief marketing": "Marketing",
    "cto": "R&D", "chief technology": "R&D",
    "cio": "IT", "chief information": "IT",
    "chro": "HR", "chief human": "HR", "chief people": "HR",
    "cso": "Strategy", "chief strategy": "Strategy",
    "cro": "Risk", "chief risk": "Risk",
    "ccompo": "Compliance", "chief compliance": "Compliance",
    "general counsel": "Legal", "chief legal": "Legal",
    "chief revenue": "Sales", "chief sales": "Sales",
    "chief customer": "Customer Service",
    "chief product": "Strategy",
    "chief data": "IT", "chief ai": "IT",
    "chief medical": "Medical Affairs",
    "chief nursing": "Operations",
    "chief underwriting": "Underwriting",
    "chief actuary": "Actuarial",
    "chief claims": "Claims",
    "chief credit": "Credit",
    "chief investment": "Strategy",
}


def _function_from_title(title: str) -> str:
    title_l = title.lower()
    for key, func in TITLE_FUNCTION_HINTS.items():
        if key in title_l:
            return func
    return "Strategy"


class ReconcilerAgent:
    """Agent 4 — deterministic hierarchy builder."""

    def __init__(
        self,
        rules: RuleLibrary,
        firm: str,
        industry: str,
        org_type: str,
        client_archetype: str = "Enterprise",
        geography_scope: str = "Global",
        sub_industry: Optional[str] = None,
    ):
        self.rules = rules
        self.firm = firm
        self.industry = industry
        self.org_type = org_type
        self.client_archetype = client_archetype
        self.geography_scope = geography_scope
        self.sub_industry = sub_industry

        archetype = self.rules.archetype_for_industry(industry)
        if archetype is None:
            raise ValueError(
                f"No archetype mapping for industry '{industry}'. "
                f"Supported: {list(INDUSTRY_TO_ARCHETYPE.keys())}"
            )
        self.archetype = archetype
        self.archetype_id = archetype["archetype_id"]

        # Track all nodes by id
        self.nodes: dict[str, CanonicalNode] = {}
        # Track per-(function, region) the highest-level node id, used for parenting
        self.function_region_index: dict[tuple[str, str], list[str]] = {}

    # ------------------------------------------------------------------
    # PUBLIC ENTRY POINT
    # ------------------------------------------------------------------
    def reconcile(
        self,
        leaders: list[AuthoritativeLeader],
        persons: list[NormalizedPerson],
    ) -> CanonicalOrganogram:
        # Step 1: seed with authoritative leaders (immutable)
        self._seed_authoritative_leaders(leaders)

        # Step 2: place LinkedIn-derived persons (deterministic)
        # Sort by level ascending so parents exist when children land
        for p in sorted(persons, key=lambda x: x.inferred_level):
            self._place_person(p)

        # Step 3: build views
        functional_roots = self._functional_roots()
        geographic_roots = self._geographic_roots()
        legal_entity_graph = self._legal_entity_graph()
        legal_entity_roots = [g["entity"] for g in legal_entity_graph if g.get("parent") is None]

        org = CanonicalOrganogram(
            firm=self.firm,
            industry=self.industry,
            sub_industry=self.sub_industry,
            org_type=self.org_type,
            archetype=self.archetype_id,
            archetype_version=self.archetype.get("version", 1),
            as_of=date.today().isoformat(),
            geography_scope=self.geography_scope,
            client_archetype=self.client_archetype,
            nodes=list(self.nodes.values()),
            views={
                "functional": functional_roots,
                "geographic": geographic_roots,
                "legal_entity": legal_entity_roots,
            },
            legal_entity_graph=legal_entity_graph,
        )
        return org

    # ------------------------------------------------------------------
    # STEP 1: authoritative leaders
    # ------------------------------------------------------------------
    def _seed_authoritative_leaders(self, leaders: list[AuthoritativeLeader]):
        """Levels 1–2 are immutable. Place CEO first, then C-suite under CEO."""
        ceo_id: Optional[str] = None

        # First pass — create the CEO node (Level 1)
        for L in leaders:
            t = L.title.lower()
            if not L.is_board and ("ceo" in t or "chief executive" in t
                                   or t.startswith("president")
                                   or "directeur général" in t.lower()
                                   or "代表取締役" in L.title):
                node = self._make_node(
                    name=L.name, title_native=L.title, title_en=L.title,
                    function="Strategy", level=1,
                    region="Global", country=None,
                    legal_entity=self.firm,
                    source=L.source_url, source_type=L.source_type,
                    reports_to_id=None,
                )
                ceo_id = node.id
                break

        if ceo_id is None:
            # Synthetic CEO node so the tree has a root
            node = self._make_node(
                name="(CEO not identified)", title_native="CEO",
                title_en="CEO", function="Strategy", level=1,
                region="Global", country=None, legal_entity=self.firm,
                source="", source_type="synthetic",
                reports_to_id=None,
            )
            ceo_id = node.id

        # Second pass — place all other leaders
        for L in leaders:
            if L.name == self.nodes[ceo_id].name:
                continue
            level = 1 if L.is_board else 2
            function = _function_from_title(L.title)
            self._make_node(
                name=L.name, title_native=L.title, title_en=L.title,
                function=function, level=level,
                region="Global", country=None,
                legal_entity=self.firm,
                source=L.source_url, source_type=L.source_type,
                reports_to_id=ceo_id if not L.is_board else None,
            )

    # ------------------------------------------------------------------
    # STEP 2: place a single LinkedIn-derived person
    # ------------------------------------------------------------------
    def _place_person(self, p: NormalizedPerson):
        # Unclassified → synthetic branch under "Unclassified — Unassigned"
        if p.function == "Unclassified" or p.inferred_level >= 99:
            parent_id = self._ensure_unassigned_branch("Unclassified", p.region)
            self._make_node(
                name=p.name, title_native=p.title_native, title_en=p.title_en,
                function="Unclassified", level=p.inferred_level if p.inferred_level < 99 else 10,
                region=p.region, country=p.country,
                legal_entity=p.legal_entity or self.firm,
                source=p.source_url, source_type=p.source_type,
                reports_to_id=parent_id,
                inference_note=p.inference_note,
            )
            return

        # Find a parent: closest higher-level node in the same function and region.
        parent_id = self._find_parent(p.function, p.inferred_level, p.region)

        # If no parent exists, attach to the synthetic Unassigned branch
        if parent_id is None:
            parent_id = self._ensure_unassigned_branch(p.function, p.region)

        self._make_node(
            name=p.name, title_native=p.title_native, title_en=p.title_en,
            function=p.function, level=p.inferred_level,
            region=p.region, country=p.country,
            legal_entity=p.legal_entity or self.firm,
            source=p.source_url, source_type=p.source_type,
            reports_to_id=parent_id,
            inference_note=p.inference_note,
        )

    def _find_parent(
        self, function: str, level: int, region: str
    ) -> Optional[str]:
        """
        Find the closest higher-level node in same function. Search order:
          1. Same region, walking up levels.
          2. Region == 'Global' (the global C-suite), walking up levels.
          3. Any region (last resort), walking up levels.
        Crossing into another country/region is the LAST resort, never the first.
        """
        # Tier 1: same region
        for L in range(level - 1, 0, -1):
            for node in self.nodes.values():
                if (node.function == function
                        and node.level == L
                        and node.region == region):
                    return node.id
        # Tier 2: Global (the C-suite)
        for L in range(level - 1, 0, -1):
            for node in self.nodes.values():
                if (node.function == function
                        and node.level == L
                        and node.region == "Global"):
                    return node.id
        # Tier 3: any region
        for L in range(level - 1, 0, -1):
            for node in self.nodes.values():
                if node.function == function and node.level == L:
                    return node.id
        return None

    def _ensure_unassigned_branch(self, function: str, region: str) -> str:
        """Create or reuse a synthetic '{Function} — Unassigned' parent node."""
        synth_name = f"{function} — Unassigned"
        for node in self.nodes.values():
            if node.name == synth_name and node.region == region:
                return node.id
        # Anchor under CEO if a CEO exists; else under firm root
        ceo_id = next((n.id for n in self.nodes.values() if n.level == 1), None)
        node = self._make_node(
            name=synth_name, title_native=synth_name, title_en=synth_name,
            function=function, level=2,
            region=region, country=None,
            legal_entity=self.firm,
            source="", source_type="synthetic",
            reports_to_id=ceo_id,
            inference_note="Synthetic parent for unparented persons.",
        )
        return node.id

    # ------------------------------------------------------------------
    # NODE CREATION HELPER
    # ------------------------------------------------------------------
    def _make_node(self, **kw) -> CanonicalNode:
        node = CanonicalNode(id=_new_id(), **kw)
        # Wire reports_to_id <-> direct_reports_ids
        if node.reports_to_id and node.reports_to_id in self.nodes:
            self.nodes[node.reports_to_id].direct_reports_ids.append(node.id)
        self.nodes[node.id] = node
        # Index for parent lookups
        key = (node.function, node.region)
        self.function_region_index.setdefault(key, []).append(node.id)
        return node

    # ------------------------------------------------------------------
    # VIEW BUILDERS
    # ------------------------------------------------------------------
    def _functional_roots(self) -> list[str]:
        """Functional view roots = Level-1 nodes (CEO/Chairman)."""
        return [n.id for n in self.nodes.values()
                if n.level == 1 and n.reports_to_id is None]

    def _geographic_roots(self) -> list[str]:
        """
        Geographic view roots = top-of-country node per country.
        Selection logic:
          1. If the country has a person whose title contains 'Country Manager',
             'Country Head', 'Country Director', 'Managing Director', or who
             is a CEO/President of a country-named legal entity — that's the
             country root.
          2. Otherwise: highest-rank (lowest level number) person in that country.
        Plus: emit the Global root (CEO) as a separate entry so the view can
        anchor itself at the global level above the countries.
        """
        per_country: dict[str, str] = {}
        country_signal_titles = (
            "country manager", "country head", "country director",
            "managing director", "representative director",
        )
        # Pass 1 — explicit country leaders
        for n in self.nodes.values():
            if not n.country:
                continue
            tl = n.title_en.lower()
            if any(s in tl for s in country_signal_titles):
                # Prefer the highest one we find
                if (n.country not in per_country
                        or self.nodes[per_country[n.country]].level > n.level):
                    per_country[n.country] = n.id
        # Pass 2 — for countries without an explicit leader, pick highest-rank resident
        for n in self.nodes.values():
            if not n.country or n.country in per_country:
                continue
            if n.level >= 99:
                continue  # skip Unclassified
            if n.country not in per_country:
                per_country[n.country] = n.id
            else:
                cur = self.nodes[per_country[n.country]]
                if n.level < cur.level:
                    per_country[n.country] = n.id

        roots = list(per_country.values())
        # Prepend the global CEO if present
        global_ceo = next(
            (n.id for n in self.nodes.values()
             if n.level == 1 and n.region == "Global"
             and ("ceo" in n.title_en.lower()
                  or "chief executive" in n.title_en.lower())),
            None,
        )
        if global_ceo and global_ceo not in roots:
            roots.insert(0, global_ceo)
        return roots

    def _legal_entity_graph(self) -> list[dict]:
        """Holding -> subsidiaries. Built from legal_entity values seen on nodes."""
        entities_seen: dict[str, set[str]] = {}
        for n in self.nodes.values():
            le = n.legal_entity or self.firm
            entities_seen.setdefault(le, set()).add(n.id)

        graph: list[dict] = []
        for entity, person_ids in entities_seen.items():
            graph.append({
                "entity": entity,
                "parent": None if entity == self.firm else self.firm,
                "country": None,
                "person_ids": sorted(person_ids),
            })
        # Move firm to top of list
        graph.sort(key=lambda g: 0 if g["entity"] == self.firm else 1)
        return graph
