"""
Vendor mapper. Translates the vendor database's JOB_FUNCTION and JOB_LEVEL
fields into the engine's 18-function and 1–10 level taxonomy.

Resolution policy (per design decision #1):
  - Trust the vendor classification first.
  - Validate against the rule library.
  - On conflict, prefer rule library and log the conflict.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# JOB_LEVEL mapping (per design decision #2)
# ---------------------------------------------------------------------------
# Vendor levels are categorical (e.g. "VP", "Director", "Staff").
# We map to a numeric 1..10 where 1 = CEO and 10 = entry/IC.
# Region/archetype overlays may still adjust this (e.g., capital_markets
# pushes VP to level 6).
VENDOR_LEVEL_MAP: dict[str, int] = {
    # Board / Non-Executive tier  (L0 — Board of Management)
    "board":               0,
    "board member":        0,
    "board director":      0,
    "non-executive":       0,
    "non executive":       0,
    "independent director": 0,
    "chairman":            0,

    # C-Suite tier  (L1 — Executive Management)
    "founder":             1,
    "owner":               1,
    "partner":             1,
    "cxo":                 1,
    "c-level":             1,
    "c-suite":             1,
    "executive":           1,

    # Senior leadership tier
    "evp":                 2,
    "executive vice president": 2,
    "svp":                 2,
    "senior vice president": 2,

    # VP tier (overridden by archetype in NLP agent — e.g., capital_markets)
    "vp":                  3,
    "vice president":      3,

    # Director tier
    "senior director":     4,
    "sr director":         4,
    "sr. director":        4,
    "director":            5,

    # Manager tier
    "senior manager":      6,
    "sr manager":          6,
    "sr. manager":         6,
    "manager":             7,
    "team lead":           8,
    "lead":                8,

    # Staff / IC tier
    "senior staff":        8,
    "senior ic":           8,
    "senior":              8,
    "staff":               9,
    "general staff":       9,
    "individual contributor": 9,
    "ic":                  9,
    "associate":           9,
    "analyst":             10,
    "entry":               10,
    "entry-level":         10,
    "entry level":         10,
    "intern":              10,
    "trainee":             10,
}


# ---------------------------------------------------------------------------
# JOB_FUNCTION mapping (per design decision #3)
# ---------------------------------------------------------------------------
# Vendor functions don't perfectly match the engine's 18 standard functions.
# Where ambiguous, we apply rules:
#   - "Operations" stays as Operations; let the title disambiguate to
#     Manufacturing or Supply Chain when those keywords appear in the title.
#   - "Engineering" defaults to "Engineering" except in the
#     process_industries archetype where it defaults to "R&D".
VENDOR_FUNCTION_MAP: dict[str, str] = {
    # Finance & Accounting
    "finance":                  "Finance & Accounting",
    "accounting":               "Finance & Accounting",
    "treasury":                 "Finance & Accounting",
    "tax":                      "Finance & Accounting",
    "audit":                    "Finance & Accounting",
    "actuarial":                "Finance & Accounting",
    "investor relations":       "Finance & Accounting",
    # Human Resources
    "human resources":          "Human Resources",
    "hr":                       "Human Resources",
    "people":                   "Human Resources",
    "people & culture":         "Human Resources",
    "talent":                   "Human Resources",
    # Legal, Risk & Compliance (merged per Global_Org_Hierarchy.xlsx)
    "legal":                    "Legal, Risk & Compliance",
    "compliance":               "Legal, Risk & Compliance",
    "regulatory":               "Legal, Risk & Compliance",
    "regulatory affairs":       "Legal, Risk & Compliance",
    "risk":                     "Legal, Risk & Compliance",
    "credit":                   "Legal, Risk & Compliance",
    # Information Technology (infra, apps, cybersecurity, data)
    "it":                       "Information Technology",
    "information technology":   "Information Technology",
    "technology":               "Information Technology",
    "data":                     "Information Technology",
    "data science":             "Information Technology",
    "analytics":                "Information Technology",
    "cybersecurity":            "Information Technology",
    "information security":     "Information Technology",
    # Engineering (software / platform / hardware development)
    "engineering":              "Engineering",
    "software":                 "Engineering",
    "software engineering":     "Engineering",
    "platform engineering":     "Engineering",
    "devops":                   "Engineering",
    # Sales & Business Development
    "sales":                    "Sales & Business Development",
    "business development":     "Sales & Business Development",
    "partnerships":             "Sales & Business Development",
    # Marketing
    "marketing":                "Marketing",
    "brand":                    "Marketing",
    # Corporate Communications & Public Affairs
    "communications":           "Corporate Communications & Public Affairs",
    "public relations":         "Corporate Communications & Public Affairs",
    "public affairs":           "Corporate Communications & Public Affairs",
    "government relations":     "Corporate Communications & Public Affairs",
    # Customer Success & Service
    "customer service":         "Customer Success & Service",
    "customer success":         "Customer Success & Service",
    "customer experience":      "Customer Success & Service",
    "customer support":         "Customer Success & Service",
    "support":                  "Customer Success & Service",
    # Operations
    "operations":               "Operations",
    "general management":       "Operations",
    "quality":                  "Operations",
    "quality assurance":        "Operations",
    "qa":                       "Operations",
    "education":                "Operations",
    "academic":                 "Operations",
    # Facilities, Real Estate & Workplace
    "facilities":               "Facilities, Real Estate & Workplace",
    "real estate":              "Facilities, Real Estate & Workplace",
    "workplace":                "Facilities, Real Estate & Workplace",
    # Insurance-specific (keep as standalone canonical primaries)
    "underwriting":             "Underwriting",
    "claims":                   "Claims",
    # Supply Chain
    "supply chain":             "Supply Chain",
    "logistics":                "Supply Chain",
    # Procurement
    "procurement":              "Procurement",
    "purchasing":               "Procurement",
    "sourcing":                 "Procurement",
    # Manufacturing
    "manufacturing":            "Manufacturing",
    "production":               "Manufacturing",
    # Product Management
    "product":                  "Product Management",
    "product management":       "Product Management",
    "design":                   "Product Management",
    "ux":                       "Product Management",
    # Strategy & Corporate Development
    "strategy":                 "Strategy & Corporate Development",
    "consulting":               "Strategy & Corporate Development",
    "programme":                "Strategy & Corporate Development",
    "program":                  "Strategy & Corporate Development",
    "programme management":     "Strategy & Corporate Development",
    "program management":       "Strategy & Corporate Development",
    "pmo":                      "Strategy & Corporate Development",
    "project management":       "Strategy & Corporate Development",
    "transformation":           "Strategy & Corporate Development",
    "executive":                "Strategy & Corporate Development",
    "corporate development":    "Strategy & Corporate Development",
    "advisory":                 "Strategy & Corporate Development",
    "investment":               "Investment Management",
    "trading":                  "Sales & Trading",
    "research analyst":         "Research & Development",
    # R&D
    "r&d":                      "Research & Development",
    "research":                 "Research & Development",
    "research & development":   "Research & Development",
    "research and development": "Research & Development",
    "innovation":               "Research & Development",
    # Industry-specific
    "medical":                  "Medical Affairs",
    "medical affairs":          "Medical Affairs",
}


# Title keywords that disambiguate the vendor's "Operations" function
OPERATIONS_DISAMBIGUATORS = {
    "Manufacturing": ["plant manager", "production", "shop floor",
                      "factory", "operator", "manufacturing",
                      "werksleiter", "kōjō-chō", "工場長"],
    "Supply Chain":  ["logistics", "warehouse", "distribution", "fulfillment",
                      "shipping", "supply chain"],
    "Quality":       ["quality", "qa "],
    "Procurement":   ["procurement", "buyer", "sourcing", "purchasing"],
}


@dataclass
class VendorClassification:
    """The output of mapping a vendor row to engine taxonomy."""
    function: Optional[str]
    level: Optional[int]
    raw_function: str
    raw_level: str


def map_vendor_function(
    vendor_function: str,
    vendor_persona: str,
    title: str,
    archetype_id: str,
) -> Optional[str]:
    """
    Map vendor JOB_FUNCTION + PERSONA + title -> engine function.
    Returns None if no confident mapping.
    """
    title_l = (title or "").lower()
    vf = (vendor_function or "").strip().lower()

    # Look up direct mapping
    engine_function = VENDOR_FUNCTION_MAP.get(vf)

    # Disambiguate "Operations"
    if engine_function == "Operations":
        for target, keywords in OPERATIONS_DISAMBIGUATORS.items():
            if any(k in title_l for k in keywords):
                return target

    # Facilities-titled people tagged as Engineering → Facilities, Real Estate & Workplace
    if engine_function == "Engineering":
        if any(k in title_l for k in ("facilit", "real estate", "workplace", "property")):
            return "Facilities, Real Estate & Workplace"

    # Process-industries Engineering → R&D
    if engine_function == "Engineering" and archetype_id == "process_industries":
        return "Research & Development"

    # IT-tagged facilities/real-estate roles → Facilities, Real Estate & Workplace
    if engine_function == "Information Technology":
        if any(k in title_l for k in ("facilit", "real estate", "workplace", "property")):
            return "Facilities, Real Estate & Workplace"

    return engine_function


def map_vendor_level(vendor_level: str, vendor_persona: str) -> Optional[int]:
    """
    Map vendor JOB_LEVEL string to engine numeric level.
    Falls back to PERSONA if JOB_LEVEL is unrecognised.
    """
    vl = (vendor_level or "").strip().lower()
    if vl in VENDOR_LEVEL_MAP:
        return VENDOR_LEVEL_MAP[vl]

    # Try persona as backup ("General Staff", "Senior Manager", etc.)
    vp = (vendor_persona or "").strip().lower()
    if vp in VENDOR_LEVEL_MAP:
        return VENDOR_LEVEL_MAP[vp]
    # Substring match for compound personas like "General Staff"
    for key, level in VENDOR_LEVEL_MAP.items():
        if key in vp:
            return level

    return None


def classify(
    vendor_function: str,
    vendor_level: str,
    vendor_persona: str,
    title: str,
    archetype_id: str,
) -> VendorClassification:
    """Top-level: turn vendor strings into engine-taxonomy values."""
    return VendorClassification(
        function=map_vendor_function(vendor_function, vendor_persona, title, archetype_id),
        level=map_vendor_level(vendor_level, vendor_persona),
        raw_function=vendor_function or "",
        raw_level=vendor_level or "",
    )
