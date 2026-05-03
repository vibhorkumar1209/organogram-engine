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
    # Board / C-suite tier
    "board":               1,
    "board member":        1,
    "chairman":            1,
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
    # Direct mappings
    "finance":             "Finance",
    "accounting":          "Finance",
    "treasury":            "Finance",
    "tax":                 "Finance",
    "human resources":     "HR",
    "hr":                  "HR",
    "people":              "HR",
    "talent":              "HR",
    "legal":               "Legal",
    "compliance":          "Compliance",
    "risk":                "Risk",
    "audit":               "Compliance",
    "it":                  "IT",
    "information technology": "IT",
    "technology":          "IT",
    "sales":               "Sales",
    "business development": "Sales",
    "marketing":           "Marketing",
    "communications":      "Marketing",
    "brand":               "Marketing",
    "operations":          "Operations",   # ambiguous — title disambiguates
    "engineering":         "Engineering",  # ambiguous — see _resolve_engineering
    "r&d":                 "R&D",
    "research":            "R&D",
    "research & development": "R&D",
    "research and development": "R&D",
    "product":             "Strategy",
    "product management":  "Strategy",
    "strategy":            "Strategy",
    "consulting":          "Strategy",
    "executive":           "Strategy",
    "general management":  "Strategy",
    "corporate development": "Strategy",
    "programme":           "Strategy",
    "program":             "Strategy",
    "programme management": "Strategy",
    "program management":  "Strategy",
    "pmo":                 "Strategy",
    "project management":  "Strategy",
    "transformation":      "Strategy",
    "supply chain":        "Supply Chain",
    "logistics":            "Supply Chain",
    "procurement":         "Procurement",
    "purchasing":          "Procurement",
    "sourcing":            "Procurement",
    "manufacturing":       "Manufacturing",
    "production":          "Manufacturing",
    "quality":             "Quality",
    "qa":                  "Quality",
    "quality assurance":   "Quality",
    "customer service":    "Customer Service",
    "customer success":    "Customer Service",
    "customer experience": "Customer Service",
    "customer support":    "Customer Service",
    "support":             "Customer Service",
    "corporate affairs":   "Corporate Affairs",
    "public relations":    "Corporate Affairs",
    "investor relations":  "Corporate Affairs",
    "government affairs":  "Corporate Affairs",

    # Industry-specific (fall through to engine taxonomy via archetype)
    "medical":             "Medical Affairs",
    "medical affairs":     "Medical Affairs",
    "regulatory":          "Compliance",
    "regulatory affairs":  "Compliance",
    "underwriting":        "Operations",
    "claims":              "Operations",
    "actuarial":           "Finance",
    "credit":              "Risk",
    "trading":             "Strategy",
    "investment":          "Strategy",
    "advisory":            "Strategy",
    "research analyst":    "Strategy",
    "data":                "IT",
    "data science":        "IT",
    "analytics":           "IT",
    "design":              "R&D",
    "ux":                  "R&D",
    "education":           "Operations",
    "academic":            "Operations",
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

    # Disambiguate "Engineering" — process industries treat as R&D
    if engine_function == "Engineering" and archetype_id == "process_industries":
        return "R&D"

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
