"""
Schemas — typed dataclasses that flow between agents.
Every agent reads one schema and emits another.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# PERSON RECORD — emitted by Agent 1 (Parser), consumed by Agent 3 (NLP)
# ---------------------------------------------------------------------------
@dataclass
class PersonRecord:
    """A normalized record from the input file."""
    name: str
    title: str
    company: str
    source_url: str
    department: Optional[str] = None
    geography: Optional[str] = None
    tenure: Optional[str] = None
    reports_to_name: Optional[str] = None
    subsidiary: Optional[str] = None

    # Vendor-supplied classification (database format).
    # When present, the NLP Agent uses these as the primary signal and
    # validates against the rule library.
    vendor_function: Optional[str] = None
    vendor_level: Optional[str] = None
    vendor_persona: Optional[str] = None

    # Rich location fields from the vendor (preferred over `geography`)
    job_country: Optional[str] = None
    job_country_code: Optional[str] = None
    job_country_region: Optional[str] = None
    job_continent: Optional[str] = None
    job_state: Optional[str] = None
    job_city: Optional[str] = None

    # Fallback identifiers when COMPANY_NAME is blank
    job_org_linkedin_url: Optional[str] = None
    email_domain: Optional[str] = None
    linkedin_industry: Optional[str] = None
    linkedin_headline: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# AUTHORITATIVE LEADER — emitted by Agent 2 (Web/Filings), Levels 1-2 only
# ---------------------------------------------------------------------------
@dataclass
class AuthoritativeLeader:
    """A BoD or ExCo member sourced from filings or firm webpage."""
    name: str
    title: str
    source_url: str
    source_type: str          # "annual_report" | "filing" | "firm_website"
    is_board: bool = False    # True for BoD, False for ExCo
    immutable: bool = True


# ---------------------------------------------------------------------------
# NORMALIZED PERSON — emitted by Agent 3 (NLP), consumed by Agent 4 (Reconciler)
# ---------------------------------------------------------------------------
@dataclass
class NormalizedPerson:
    """A person record with title parsed, translated, and leveled."""
    name: str
    title_native: str
    title_en: str
    function: str             # one of 18 standard functions
    inferred_level: int       # 1..10
    region: str
    source_url: str
    source_type: str = "linkedin"
    company: str = ""
    legal_entity: Optional[str] = None
    country: Optional[str] = None
    matched_rule: Optional[str] = None  # which CSV row / rule resolved this
    inference_note: Optional[str] = None


# ---------------------------------------------------------------------------
# CANONICAL NODE — emitted by Agent 4 (Reconciler), consumed by Agent 5 (Renderer)
# ---------------------------------------------------------------------------
@dataclass
class CanonicalNode:
    """A node in the canonical organogram tree."""
    id: str
    name: str
    title_native: str
    title_en: str
    function: str
    level: int
    region: str
    country: Optional[str]
    legal_entity: Optional[str]
    reports_to_id: Optional[str]
    direct_reports_ids: list[str] = field(default_factory=list)
    source: str = ""
    source_type: str = ""
    inference_note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CanonicalOrganogram:
    """The complete canonical output — what Agent 5 renders from."""
    firm: str
    industry: str
    sub_industry: Optional[str]
    org_type: str
    archetype: str
    archetype_version: int
    as_of: str
    geography_scope: str
    client_archetype: str

    nodes: list[CanonicalNode] = field(default_factory=list)
    views: dict = field(default_factory=lambda: {
        "functional": [], "geographic": [], "legal_entity": []
    })
    legal_entity_graph: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "firm": self.firm,
            "industry": self.industry,
            "sub_industry": self.sub_industry,
            "org_type": self.org_type,
            "archetype": self.archetype,
            "archetype_version": self.archetype_version,
            "as_of": self.as_of,
            "geography_scope": self.geography_scope,
            "client_archetype": self.client_archetype,
            "views": self.views,
            "nodes": [n.to_dict() for n in self.nodes],
            "legal_entity_graph": self.legal_entity_graph,
        }
