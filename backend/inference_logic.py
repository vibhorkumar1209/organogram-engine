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

# ── Fix 2: Bare department names as designations ─────────────────────────────
# A title that is ONLY a department/function keyword (e.g. "Sales", "Marketing",
# "Engineering", "Programme") contains no seniority signal and cannot indicate a
# department head or team lead. Such records are capped at IC level (L8+).
# We also remap the dept to the correct canonical name.
_BARE_DEPT_NAMES: dict[str, str] = {
    # key (lowercase) → canonical dept name
    "sales":              "Sales",
    "marketing":          "Marketing",
    "engineering":        "Engineering",
    "operations":         "Operations",
    "finance":            "Finance",
    "hr":                 "Human Resources",
    "human resources":    "Human Resources",
    "legal":              "Legal",
    "compliance":         "Compliance",
    "risk":               "Risk Management",
    "technology":         "Technology",
    "it":                 "Technology",
    "data":               "Data & Analytics",
    "analytics":          "Data & Analytics",
    "product":            "Product Management",
    "procurement":        "Procurement",
    "manufacturing":      "Manufacturing",
    "supply chain":       "Supply Chain",
    "logistics":          "Supply Chain",
    "audit":              "Internal Audit",
    "strategy":           "Strategy",
    "communications":     "Communications",
    "research":           "Research & Development",
    "programme":          "Strategy",       # PMO/Programme → Strategy
    "program":            "Strategy",
    "project":            "Operations",
    "support":            "Customer Experience",
    "administration":     "Operations",
    "admin":              "Operations",
    "general":            "Operations",
    "commercial":         "Sales & Commercial",
    "business development": "Sales",
    "customer service":   "Customer Experience",
    "customer success":   "Customer Success",
    "quality":            "Operations",
    "safety":             "Operations",
    "security":           "Operations",
}

# Seniority words — if ANY of these appear in the designation the title is NOT bare
_SENIORITY_WORDS: frozenset[str] = frozenset({
    "chief", "head", "director", "vp", "vice president", "svp", "evp",
    "president", "manager", "lead", "senior", "principal", "associate",
    "analyst", "officer", "executive", "specialist", "engineer", "consultant",
    "coordinator", "advisor", "partner", "secretary", "administrator",
    "superintendent", "controller", "ceo", "cfo", "cto", "coo", "cmo",
    "chro", "cio", "md", "managing", "general manager", "group",
})


def _bare_dept_check(designation: str) -> tuple[Optional[str], bool]:
    """
    Returns (canonical_dept, is_bare) for a designation.

    is_bare=True means the title is ONLY a department keyword — no seniority
    indicator present. The engine will cap such records at L8 (Staff IC).
    """
    title_l = designation.strip().lower()
    if not title_l:
        return None, False
    # Is any seniority word present?
    for sw in _SENIORITY_WORDS:
        if sw in title_l:
            return None, False
    # Does the whole title match a bare dept keyword?
    if title_l in _BARE_DEPT_NAMES:
        return _BARE_DEPT_NAMES[title_l], True
    return None, False

# ── Country-code → region mapping (shared with organogram v2 NLP agent) ──────
# Imported lazily so the module loads even if organogram package is absent.
def _get_country_code_to_region() -> dict[str, str]:
    try:
        from organogram.agents.nlp_agent import COUNTRY_CODE_TO_REGION
        return COUNTRY_CODE_TO_REGION
    except ImportError:
        return {}

_COUNTRY_CODE_TO_REGION: dict[str, str] = {}   # populated on first use


def _region_from_country_code(code: str) -> Optional[str]:
    """Return engine region string for an ISO-2 country code, or None."""
    global _COUNTRY_CODE_TO_REGION
    if not _COUNTRY_CODE_TO_REGION:
        _COUNTRY_CODE_TO_REGION = _get_country_code_to_region()
    return _COUNTRY_CODE_TO_REGION.get((code or "").strip().upper())


# ── Vendor level → engine layer mapping ─────────────────────────────────────
def _get_vendor_level_map() -> dict[str, int]:
    try:
        from organogram.utils.vendor_mapper import VENDOR_LEVEL_MAP
        return VENDOR_LEVEL_MAP
    except ImportError:
        return {}

_VENDOR_LEVEL_MAP: dict[str, int] = {}   # populated on first use


def _layer_from_vendor_level(vendor_level: str) -> Optional[int]:
    """Map a vendor JOB_LEVEL string to an engine layer int, or None."""
    global _VENDOR_LEVEL_MAP
    if not _VENDOR_LEVEL_MAP:
        _VENDOR_LEVEL_MAP = _get_vendor_level_map()
    return _VENDOR_LEVEL_MAP.get((vendor_level or "").strip().lower())


# ── Vendor function → engine function mapping ────────────────────────────────
def _get_vendor_function_map() -> dict[str, str]:
    try:
        from organogram.utils.vendor_mapper import VENDOR_FUNCTION_MAP
        return VENDOR_FUNCTION_MAP
    except ImportError:
        return {}

_VENDOR_FUNCTION_MAP: dict[str, str] = {}   # populated on first use


def _dept_from_vendor_function(vendor_function: str) -> Optional[str]:
    """Map a vendor JOB_FUNCTION string to an engine department string, or None."""
    global _VENDOR_FUNCTION_MAP
    if not _VENDOR_FUNCTION_MAP:
        _VENDOR_FUNCTION_MAP = _get_vendor_function_map()
    return _VENDOR_FUNCTION_MAP.get((vendor_function or "").strip().lower())

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
        """
        Classify a single raw record dict.

        Field priority (highest → lowest):
          Title      : Designation / JOB_TITLE  >  linkedin_headline
          Company    : Company  >  job_org_linkedin_url slug  >  email_domain
          Region     : job_country_code  >  country_code  >  Location text
          Function   : ProfileLevel  >  vendor_function (JOB_FUNCTION)  >  NLP
          Layer      : ProfileLevel  >  vendor_level (JOB_LEVEL)  >  NLP
          Industry   : Industry_Hint  >  linkedin_industry
        """
        # ── Title ─────────────────────────────────────────────────────────
        designation = _get(record, "Designation") or _get(record, "linkedin_headline") or ""

        # ── Company ───────────────────────────────────────────────────────
        company = _get(record, "Company") or ""
        if not company:
            org_url = _get(record, "job_org_linkedin_url") or ""
            if org_url:
                slug = org_url.rstrip("/").split("/")[-1]
                company = slug.replace("-", " ").title()
        if not company:
            email_dom = _get(record, "email_domain") or ""
            if email_dom:
                company = email_dom.split(".")[0].title()

        # ── Industry hint ─────────────────────────────────────────────────
        industry_hint = _get(record, "Industry_Hint") or _get(record, "linkedin_industry") or ""

        # ── Location (legacy text string) ─────────────────────────────────
        location = (_get(record, "Location")
                    or _get(record, "job_country")
                    or _get(record, "country_name") or "")

        linkedin_url = _get(record, "LinkedInURL") or ""
        full_name    = _extract_name(record)

        # ── ProTrail ProfileLevel ─────────────────────────────────────────
        profile_level = _get(record, "ProfileLevel")
        raw_dept      = _get(record, "Department")
        pl_result     = _lookup_profile_level(profile_level)

        if pl_result and not industry_hint:
            industry_hint = pl_result[1]

        # ── NLP classification ────────────────────────────────────────────
        result: ClassificationResult = self.nlp.classify(
            designation=designation,
            company=company,
            industry_hint=industry_hint,
            location=location,
        )

        dept_primary   = result.dept_primary
        dept_secondary = result.dept_secondary
        dept_tertiary  = result.dept_tertiary
        layer          = result.layer

        # ── Override 1: ProfileLevel (highest priority structured signal) ──
        if pl_result:
            pl_dept, _pl_hint, pl_layer = pl_result
            if result.dept_primary in ("General",) or pl_dept != "General":
                dept_primary = pl_dept
            if pl_layer is not None and result.match_method in ("fallback",):
                layer = pl_layer

        # ── Override 2: vendor_function (JOB_FUNCTION) ───────────────────
        # Applied when NLP had no rule match (fallback) OR dept is unresolved.
        vendor_function = _get(record, "vendor_function") or ""
        if vendor_function and (
            dept_primary in ("General", "Unclassified", "")
            or result.match_method == "fallback"
        ):
            mapped_fn = _dept_from_vendor_function(vendor_function)
            if mapped_fn:
                dept_primary = mapped_fn
                logger.debug(f"vendor_function override: '{vendor_function}' → {mapped_fn}")

        # ── Override 3: vendor_level (JOB_LEVEL) ─────────────────────────
        # Applied when NLP fell back to a low-confidence guess.
        vendor_level = _get(record, "vendor_level") or ""
        if vendor_level and result.match_method in ("fallback",):
            mapped_layer = _layer_from_vendor_level(vendor_level)
            if mapped_layer is not None:
                layer = mapped_layer
                logger.debug(f"vendor_level override: '{vendor_level}' → L{mapped_layer}")

        # ── Department refinement from free-text Department field ──────────
        if raw_dept and raw_dept.lower() not in ("nan", "none", ""):
            refined = self.nlp.classify_dept_from_text(raw_dept, dept_primary)
            if refined[0] == dept_primary or not refined[0]:
                if refined[1] and not dept_secondary:
                    dept_secondary = refined[1]
                if refined[2] and not dept_tertiary:
                    dept_tertiary = refined[2]

        # ── Fix 2: Bare dept name as designation → IC, not a leader ─────
        bare_dept, is_bare = _bare_dept_check(designation)
        if is_bare and bare_dept:
            dept_primary = bare_dept
            layer = max(layer, 8)   # floor at Staff/IC — never a head/manager
            logger.debug(
                f"Bare dept designation '{designation}' → {bare_dept} L{layer}"
            )

        # ── Sector ────────────────────────────────────────────────────────
        sector = self.nlp.classify_sector(company, designation, industry_hint)

        # ── Region — country code wins over text parsing ──────────────────
        # Priority: JOB_LOCATION_COUNTRY_CODE > COUNTRY_CODE > Location text
        region: Optional[str] = None
        for code_field in ("job_country_code", "country_code"):
            code = _get(record, code_field) or ""
            if code:
                region = _region_from_country_code(code)
                if region:
                    break
        if not region:
            region = self.nlp.classify_region(location)

        return ClassifiedRecord(
            id=str(uuid.uuid4()),
            full_name=full_name,
            designation=designation,
            company=company,
            linkedin_url=linkedin_url,
            location=location,
            sector=sector,
            region=region or "Global",
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
