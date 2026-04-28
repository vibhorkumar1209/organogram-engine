"""
Universal Organogram Engine - Inference Engine (NLP-powered)
Classifies raw person records using per-industry YAML directories
and NLP-enhanced designation parsing (abbreviation expansion,
fuzzy matching, regex patterns).

Hierarchy:
  Global HQ (root)
    └── Region (layer 0)
          └── Dept Primary (layer 1)
                └── Dept Secondary (layer 2)
                      └── Dept Tertiary (layer 3)
                            └── Person (layer 4–10 per seniority)

Layer scale (people nodes):
  0  Board of Directors / Non-Executive
  1  C-Suite / CEO / Founder / Managing Partner
  2  Executive Director / MD / EVP
  3  SVP / VP / General Manager
  4  Senior Director / Head of Business
  5  Director / Head
  6  Senior Manager / Associate Director
  7  Manager / Team Lead
  8  Senior Analyst / Specialist / Senior IC
  9  Analyst / Associate / IC
  10 Graduate / Intern / Entry Level
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from nlp_engine import NLPEngine, ClassificationResult

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# COMPOUND TITLE SPLITTING
# ─────────────────────────────────────────────────────────────────

# Tokens that indicate a string is a standalone seniority title (not a dept word)
_TITLE_TOKENS: frozenset[str] = frozenset({
    "ceo", "cfo", "coo", "cto", "cmo", "cso", "chro", "cpo", "cio",
    "cdo", "cro", "ccio", "caio", "cxo",
    "president", "chairman", "chairperson", "chairwoman",
    "director", "svp", "evp", "vp", "md", "officer",
    "manager", "partner", "founder", "principal",
    "deputy", "chief", "head", "lead", "supervisor",
    "superintendent", "coordinator",
})

_COMPOUND_SEPS = (" and ", " & ", " / ")


def _split_compound_title(designation: str) -> list[str]:
    """
    Split compound executive titles like "CEO and CHRO" → ["CEO", "CHRO"].
    Does NOT split functional phrases like "Head of Sales and Marketing".

    Rule: split only when BOTH sides contain at least one seniority token.
    """
    des = designation.strip()
    for sep in _COMPOUND_SEPS:
        if sep.lower() in des.lower():
            idx = des.lower().index(sep.lower())
            left  = des[:idx].strip()
            right = des[idx + len(sep):].strip()
            if not left or not right:
                continue
            left_words  = set(left.lower().split())
            right_words = set(right.lower().split())
            if left_words & _TITLE_TOKENS and right_words & _TITLE_TOKENS:
                return [left, right]
    return [des]

# Singleton NLP engine — initialised once, reused across all records
_NLP: Optional[NLPEngine] = None


def get_nlp() -> NLPEngine:
    global _NLP
    if _NLP is None:
        _NLP = NLPEngine()
        logger.info(f"NLP engine initialised with industries: {_NLP.loaded_industries}")
    return _NLP


# ─────────────────────────────────────────────────────────────────
# OUTPUT RECORD
# ─────────────────────────────────────────────────────────────────

@dataclass
class ClassifiedRecord:
    id: str
    full_name: str
    designation: str
    company: str
    linkedin_url: str
    location: str
    sector: str
    region: str
    layer: int
    dept_primary: str
    dept_secondary: str
    dept_tertiary: str
    # NLP provenance
    nlp_confidence: float = 0.0
    nlp_industry: str = "generic"
    nlp_method: str = "fallback"


# ─────────────────────────────────────────────────────────────────
# FIELD EXTRACTOR
# ─────────────────────────────────────────────────────────────────

# All known column aliases → canonical name
_FIELD_ALIASES: dict[str, str] = {
    # FirstName
    "firstname": "FirstName", "first_name": "FirstName", "first name": "FirstName",
    "given name": "FirstName", "givenname": "FirstName", "fname": "FirstName",
    # LastName
    "lastname": "LastName", "last_name": "LastName", "last name": "LastName",
    "surname": "LastName", "family name": "LastName", "lname": "LastName",
    # Full name
    "fullname": "FullName", "full name": "FullName", "name": "FullName",
    "full_name": "FullName", "contact name": "FullName", "person name": "FullName",
    "employee name": "FullName",
    # Designation
    "designation": "Designation", "title": "Designation", "job title": "Designation",
    "jobtitle": "Designation", "position": "Designation", "role": "Designation",
    "job role": "Designation", "current title": "Designation",
    "currenttitle": "Designation", "headline": "Designation",
    "current position": "Designation",
    # Company
    "company": "Company", "company name": "Company", "companyname": "Company",
    "organization": "Company", "organisation": "Company", "employer": "Company",
    "current company": "Company", "currentcompany": "Company",
    "account": "Company", "firm": "Company",
    # LinkedInURL
    "linkedinurl": "LinkedInURL", "linkedin url": "LinkedInURL",
    "linkedin": "LinkedInURL", "linkedin profile": "LinkedInURL",
    "profile url": "LinkedInURL", "profileurl": "LinkedInURL", "url": "LinkedInURL",
    # Location
    "location": "Location", "city": "Location", "country": "Location",
    "region": "Location", "office location": "Location",
    "officelocation": "Location", "geography": "Location", "geo": "Location",
    "based in": "Location", "basedin": "Location",
    # Industry hint
    "industry_hint": "Industry_Hint", "industry": "Industry_Hint",
    "sector": "Industry_Hint", "domain": "Industry_Hint",
    "vertical": "Industry_Hint", "industry hint": "Industry_Hint",
    "industryhint": "Industry_Hint",
    # ProTrail / structured ProfileLevel → treated as a strong industry/dept hint
    "profilelevel": "ProfileLevel", "profile level": "ProfileLevel",
    "profile_level": "ProfileLevel", "org": "ProfileLevel",
    # ProTrail raw Department field
    "department": "Department", "dept": "Department",
}


# ─────────────────────────────────────────────────────────────────
# PROTRAIL ProfileLevel → (dept_primary, industry_hint) mapping
# Derived from 354k-row ProTrail-OrgData analysis
# ─────────────────────────────────────────────────────────────────
_PROFILE_LEVEL_MAP: dict[str, tuple[str, str, int | None]] = {
    # ProfileLevel value → (dept_primary, industry_hint, forced_layer or None)
    "it org":                                               ("Information Technology", "technology", None),
    "information technology":                               ("Information Technology", "technology", None),
    "operations org":                                       ("Operations",             "generic",    None),
    "executive management":                                 ("Executive Management",   "generic",    1),
    "sales org":                                            ("Sales",                  "generic",    None),
    "finance org":                                          ("Finance",                "banking_finance", None),
    "marketing org":                                        ("Marketing",              "generic",    None),
    "hr org":                                               ("Human Resources",        "generic",    None),
    "human resources":                                      ("Human Resources",        "generic",    None),
    "board of directors":                                   ("Board of Directors",     "generic",    0),
    "board of director":                                    ("Board of Directors",     "generic",    0),
    "supervisory board":                                    ("Board of Directors",     "generic",    0),
    "board of trustees":                                    ("Board of Directors",     "generic",    0),
    "artificial intelligence and machine learning":         ("Data & AI",              "technology", None),
    "engineering, r&d":                                     ("Engineering & R&D",      "manufacturing", None),
    "design and engineering":                               ("Engineering & R&D",      "manufacturing", None),
    "engineering & construction, design, plant & manufacturing": ("Engineering & R&D", "manufacturing", None),
    "design and construction":                              ("Engineering & R&D",      "manufacturing", None),
    "procurement services":                                 ("Procurement & Supply Chain", "generic", None),
    "logistics and transportation":                         ("Logistics & Supply Chain", "generic",  None),
    "healthcare org":                                       ("Healthcare",             "healthcare", None),
    "manufacturing":                                        ("Manufacturing",          "manufacturing", None),
    "it services and consulting":                           ("IT Consulting",          "technology", None),
    "advanced driver assistance systems (adas)":            ("Engineering & R&D",      "automotive", None),
    "corporate operations group":                           ("Corporate Operations",   "generic",    None),
    "corporate development division":                       ("Corporate Development",  "generic",    2),
    "risk management & compliance":                         ("Risk & Compliance",      "banking_finance", None),
    "asset management":                                     ("Asset Management",       "banking_finance", None),
    "data analytics":                                       ("Data & Analytics",       "technology", None),
    "sustainability":                                       ("Sustainability",          "generic",    None),
    "legal & governance group":                             ("Legal",                  "generic",    None),
    "infrastructure & construction":                        ("Engineering & R&D",      "manufacturing", None),
    "media and entertainment":                              ("Marketing & Comms",      "generic",    None),
}

# Normalized lookup
_PROFILE_LEVEL_MAP_NORM: dict[str, tuple[str, str, int | None]] = {
    k.lower().strip(): v for k, v in _PROFILE_LEVEL_MAP.items()
}


def _lookup_profile_level(raw: str) -> tuple[str, str, int | None] | None:
    """Return (dept_primary, industry_hint, forced_layer) for a ProfileLevel string, or None."""
    if not raw:
        return None
    key = raw.lower().strip()
    if key in _PROFILE_LEVEL_MAP_NORM:
        return _PROFILE_LEVEL_MAP_NORM[key]
    # Partial match for long/variant values
    for k, v in _PROFILE_LEVEL_MAP_NORM.items():
        if k in key or key in k:
            return v
    return None


def _get(record: dict, *canonical_names: str) -> str:
    """Return first non-empty value matching any canonical field name."""
    # Try direct canonical key first
    for name in canonical_names:
        val = record.get(name, "")
        if val and str(val).strip() and str(val).strip().lower() not in ("nan", "none", "null"):
            return str(val).strip()

    # Try normalised key lookup
    for raw_key, raw_val in record.items():
        normalised_key = str(raw_key).lower().strip()
        canonical = _FIELD_ALIASES.get(normalised_key)
        if canonical in canonical_names:
            if raw_val and str(raw_val).strip().lower() not in ("nan", "none", "null"):
                return str(raw_val).strip()

    return ""


def _extract_name(record: dict) -> str:
    """Extract full name, handling separate First/Last or combined FullName."""
    first = _get(record, "FirstName")
    last  = _get(record, "LastName")
    if first or last:
        return f"{first} {last}".strip()

    full = _get(record, "FullName", "Name", "name")
    if full:
        return full

    return "Unknown"


# ─────────────────────────────────────────────────────────────────
# INFERENCE ENGINE
# ─────────────────────────────────────────────────────────────────

class InferenceEngine:
    """
    Classifies a list of raw person records into ClassifiedRecord objects.
    Uses the NLPEngine (with YAML industry directories) for all decisions.
    """

    def __init__(self):
        self.nlp = get_nlp()

    def classify_record(self, record: dict) -> ClassifiedRecord:
        """Classify a single raw record dict."""
        # Extract fields
        designation    = _get(record, "Designation")
        company        = _get(record, "Company")
        industry_hint  = _get(record, "Industry_Hint")
        location       = _get(record, "Location")
        linkedin_url   = _get(record, "LinkedInURL")
        full_name      = _extract_name(record)

        # ── ProTrail ProfileLevel: strongest available signal ──────────────
        profile_level  = _get(record, "ProfileLevel")
        raw_dept       = _get(record, "Department")   # freeform dept string
        pl_result      = _lookup_profile_level(profile_level)

        # Enrich industry_hint with ProfileLevel-derived hint
        if pl_result and not industry_hint:
            industry_hint = pl_result[1]

        # NLP classification (use enriched hint)
        result: ClassificationResult = self.nlp.classify(
            designation=designation,
            company=company,
            industry_hint=industry_hint,
            location=location,
        )

        # ── Apply ProfileLevel overrides (high-confidence structured data) ──
        dept_primary   = result.dept_primary
        dept_secondary = result.dept_secondary
        dept_tertiary  = result.dept_tertiary
        layer          = result.layer

        if pl_result:
            pl_dept, _pl_hint, pl_layer = pl_result
            # Override dept_primary with ProfileLevel-derived value when NLP
            # fell back to "General" or the ProfileLevel is more specific
            if result.dept_primary in ("General",) or pl_dept != "General":
                dept_primary = pl_dept
            # Override layer only when ProfileLevel forces a specific seniority
            if pl_layer is not None and result.match_method in ("fallback",):
                layer = pl_layer

        # ── Use raw Department field to refine secondary/tertiary ──────────
        if raw_dept and raw_dept.lower() not in ("nan", "none", ""):
            refined = self.nlp.classify_dept_from_text(raw_dept, dept_primary)
            # Only apply refined result when it resolves to the same primary dept
            # (or when refined has no primary opinion). Prevents cross-dept
            # contamination where e.g. "Sales Operations" text causes Finance
            # to appear as a secondary of Marketing.
            if refined[0] == dept_primary or not refined[0]:
                if refined[1] and not dept_secondary:
                    dept_secondary = refined[1]
                if refined[2] and not dept_tertiary:
                    dept_tertiary = refined[2]

        # Sector
        sector = self.nlp.classify_sector(company, designation, industry_hint)

        # Region
        region = self.nlp.classify_region(location)

        return ClassifiedRecord(
            id=str(uuid.uuid4()),
            full_name=full_name,
            designation=designation,
            company=company,
            linkedin_url=linkedin_url,
            location=location,
            sector=sector,
            region=region,
            layer=layer,
            dept_primary=dept_primary,
            dept_secondary=dept_secondary,
            dept_tertiary=dept_tertiary,
            nlp_confidence=result.confidence,
            nlp_industry=result.matched_industry,
            nlp_method=result.match_method,
        )

    def classify_all(self, records: list[dict]) -> list[ClassifiedRecord]:
        """
        Classify all records. Skips records with no name or designation.
        Compound designations like "CEO and CHRO" are split and produce
        two ClassifiedRecord entries so each title lands in its own dept.
        """
        classified: list[ClassifiedRecord] = []
        skipped = 0

        for i, rec in enumerate(records):
            try:
                designation = _get(rec, "Designation")
                titles = _split_compound_title(designation)

                if len(titles) > 1:
                    # Compound title — classify each part separately
                    for title in titles:
                        rec_copy = dict(rec)
                        rec_copy["Designation"] = title
                        cr = self.classify_record(rec_copy)
                        if cr.full_name not in ("Unknown", "") or cr.designation:
                            classified.append(cr)
                    continue

                cr = self.classify_record(rec)
                # Skip truly empty records
                if cr.full_name in ("Unknown", "") and not cr.designation:
                    skipped += 1
                    continue
                classified.append(cr)
            except Exception as e:
                logger.warning(f"Record {i} classification failed: {e}")
                skipped += 1

        logger.info(
            f"Classified {len(classified)} records "
            f"(skipped {skipped}) using {len(self.nlp.loaded_industries)} industry directories"
        )
        return classified


# ─────────────────────────────────────────────────────────────────
# CONVENIENCE FUNCTION
# ─────────────────────────────────────────────────────────────────

def classify_records(records: list[dict]) -> list[ClassifiedRecord]:
    """Module-level convenience wrapper."""
    engine = InferenceEngine()
    return engine.classify_all(records)
