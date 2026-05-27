"""
Universal Organogram Engine - Inference Engine
Classifies raw person records using the keyword-scoring classifier.

Primary signals  : job_title, linkedin_headline
Guidance signal  : job_function (soft tiebreaker, never authoritative)
Ignored          : department input field (per design decision)
Reference        : Global_Org_Hierarchy.xlsx   (16 canonical L0 departments)
                   Global_Designation_Hierarchy.xlsx (G0–G11 grade scale)

Input schema (new):
  id, full_name, job_title, job_function, city, department (ignored),
  title, email_domain, company_name, country_name, job_count,
  job_is_current, job_level, linkedin_connections_count, linkedin_url,
  linkedin_headline, job_org_linkedin_url

Layer scale (people nodes):
  0  Board / Non-Executive
  1  C-Suite / CEO / Founder
  2  EVP / Executive Director / MD
  3  SVP / Senior VP
  4  VP / Head of Function
  5  Senior Director / AVP
  6  Director
  7  Senior Manager / Associate Director
  8  Manager / Supervisor
  9  Senior IC / Lead / Staff
  10 IC / Analyst / Specialist / Graduate
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from classifier import classify as _classify_title, TitleClassification

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# BARE DEPARTMENT NAMES
# ─────────────────────────────────────────────────────────────────
# A title that is ONLY a department keyword (e.g. "Sales", "Engineering")
# has no seniority signal. Such records are capped at L8+ (IC/staff level).

_BARE_DEPT_NAMES: dict[str, str] = {
    "sales":                    "Sales & Business Development",
    "marketing":                "Marketing",
    "engineering":              "Engineering",
    "operations":               "Operations",
    "finance":                  "Finance & Accounting",
    "hr":                       "Human Resources",
    "human resources":          "Human Resources",
    "legal":                    "Legal, Risk & Compliance",
    "compliance":               "Legal, Risk & Compliance",
    "risk":                     "Legal, Risk & Compliance",
    "technology":               "Information Technology",
    "it":                       "Information Technology",
    "data":                     "Information Technology",
    "analytics":                "Information Technology",
    "product":                  "Product Management",
    "procurement":              "Procurement",
    "manufacturing":            "Manufacturing",
    "supply chain":             "Supply Chain",
    "logistics":                "Supply Chain",
    "audit":                    "Finance & Accounting",
    "strategy":                 "Strategy & Corporate Development",
    "communications":           "Corporate Communications & Public Affairs",
    "research":                 "Research & Development",
    "programme":                "Strategy & Corporate Development",
    "program":                  "Strategy & Corporate Development",
    "project":                  "Operations",
    "support":                  "Customer Success & Service",
    "administration":           "Operations",
    "admin":                    "Operations",
    "general":                  "Operations",
    "commercial":               "Sales & Business Development",
    "business development":     "Sales & Business Development",
    "customer service":         "Customer Success & Service",
    "customer success":         "Customer Success & Service",
    "quality":                  "Operations",
    "safety":                   "Operations",
    "security":                 "Information Technology",
    "facilities":               "Facilities, Real Estate & Workplace",
    "real estate":              "Facilities, Real Estate & Workplace",
    "workplace":                "Facilities, Real Estate & Workplace",
}

# Titles that indicate a former / retired / non-current role — skip these records
_FORMER_TITLE_RE = re.compile(
    r"^\s*(?:former|ex[- ]|retired|late\s+|past\s+|emeritus)"
    r"|"
    r"\b(?:former|retired|emeritus)\b",
    re.IGNORECASE,
)

_SENIORITY_WORDS: frozenset[str] = frozenset({
    "chief", "head", "director", "vp", "vice president", "svp", "evp",
    "president", "manager", "lead", "senior", "principal", "associate",
    "analyst", "officer", "executive", "specialist", "engineer", "consultant",
    "coordinator", "advisor", "partner", "secretary", "administrator",
    "superintendent", "controller", "ceo", "cfo", "cto", "coo", "cmo",
    "chro", "cio", "md", "managing", "general manager", "group",
})


def _bare_dept_check(designation: str) -> tuple[Optional[str], bool]:
    """Return (canonical_dept, is_bare). is_bare=True → cap layer at 8+."""
    title_l = designation.strip().lower()
    if not title_l:
        return None, False
    for sw in _SENIORITY_WORDS:
        if sw in title_l:
            return None, False
    if title_l in _BARE_DEPT_NAMES:
        return _BARE_DEPT_NAMES[title_l], True
    return None, False


# ─────────────────────────────────────────────────────────────────
# COMPOUND TITLE SPLITTING
# ─────────────────────────────────────────────────────────────────
# "CEO and CHRO" → ["CEO", "CHRO"]  (only when both sides are seniority tokens)
# "Head of Sales and Marketing" → stays as-is

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
    des = designation.strip()
    for sep in _COMPOUND_SEPS:
        if sep.lower() in des.lower():
            idx = des.lower().index(sep.lower())
            left  = des[:idx].strip()
            right = des[idx + len(sep):].strip()
            if not left or not right:
                continue
            if set(left.lower().split()) & _TITLE_TOKENS and set(right.lower().split()) & _TITLE_TOKENS:
                return [left, right]
    return [des]


# ─────────────────────────────────────────────────────────────────
# SECTOR CLASSIFICATION
# ─────────────────────────────────────────────────────────────────

_SECTOR_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Banking & Financial Services", [
        "bank", "financial", "finance", "investment", "capital", "asset management",
        "insurance", "wealth", "securities", "brokerage", "trading", "fund",
    ]),
    ("Technology", [
        "tech", "software", "saas", "cloud", "digital", "ai", "data",
        "analytics", "platform", "cyber", "semiconductor", "internet",
    ]),
    ("Healthcare & Life Sciences", [
        "health", "pharma", "biotech", "hospital", "medical", "clinical",
        "life science", "diagnostic", "therapeutics", "dental",
    ]),
    ("Professional Services", [
        "consulting", "advisory", "audit", "accounting", "legal", "law firm",
        "recruitment", "staffing",
    ]),
    ("Energy & Utilities", [
        "oil", "gas", "energy", "utility", "utilities", "power", "renewable",
        "mining", "petroleum",
    ]),
    ("Manufacturing & Industrials", [
        "manufacturing", "industrial", "automotive", "aerospace", "engineering",
        "chemical", "construction", "materials",
    ]),
    ("Retail & Consumer", [
        "retail", "consumer", "ecommerce", "e-commerce", "fmcg", "food",
        "beverage", "fashion", "luxury",
    ]),
    ("Telecommunications", [
        "telecom", "telecommunications", "mobile", "wireless", "broadband",
        "network provider",
    ]),
    ("Media & Entertainment", [
        "media", "entertainment", "publishing", "broadcasting", "streaming",
        "gaming", "sports",
    ]),
    ("Real Estate", [
        "real estate", "property", "reit", "realty", "facilities management",
    ]),
]


def _classify_sector(company: str, designation: str, industry_hint: str) -> str:
    combined = (company + " " + designation + " " + industry_hint).lower()
    for sector, keywords in _SECTOR_KEYWORDS:
        if any(k in combined for k in keywords):
            return sector
    return "General"


# ─────────────────────────────────────────────────────────────────
# REGION CLASSIFICATION
# ─────────────────────────────────────────────────────────────────

_COUNTRY_TO_REGION: dict[str, str] = {
    # North America
    "united states": "North America", "usa": "North America", "us": "North America",
    "canada": "North America", "mexico": "North America",
    # Europe
    "united kingdom": "Europe", "uk": "Europe", "gb": "Europe",
    "germany": "Europe", "france": "Europe", "netherlands": "Europe",
    "switzerland": "Europe", "sweden": "Europe", "norway": "Europe",
    "denmark": "Europe", "finland": "Europe", "belgium": "Europe",
    "austria": "Europe", "italy": "Europe", "spain": "Europe",
    "portugal": "Europe", "ireland": "Europe", "poland": "Europe",
    "czech republic": "Europe", "hungary": "Europe", "romania": "Europe",
    "greece": "Europe", "luxembourg": "Europe", "turkey": "Europe",
    # Middle East & Africa
    "united arab emirates": "Middle East & Africa", "uae": "Middle East & Africa",
    "saudi arabia": "Middle East & Africa", "qatar": "Middle East & Africa",
    "kuwait": "Middle East & Africa", "bahrain": "Middle East & Africa",
    "oman": "Middle East & Africa", "israel": "Middle East & Africa",
    "south africa": "Middle East & Africa", "nigeria": "Middle East & Africa",
    "kenya": "Middle East & Africa", "egypt": "Middle East & Africa",
    "ghana": "Middle East & Africa", "ethiopia": "Middle East & Africa",
    # Asia Pacific
    "india": "Asia Pacific", "in": "Asia Pacific",
    "china": "Asia Pacific", "cn": "Asia Pacific",
    "japan": "Asia Pacific", "jp": "Asia Pacific",
    "australia": "Asia Pacific", "au": "Asia Pacific",
    "singapore": "Asia Pacific", "sg": "Asia Pacific",
    "hong kong": "Asia Pacific", "hk": "Asia Pacific",
    "south korea": "Asia Pacific", "kr": "Asia Pacific",
    "indonesia": "Asia Pacific", "malaysia": "Asia Pacific",
    "thailand": "Asia Pacific", "vietnam": "Asia Pacific",
    "philippines": "Asia Pacific", "new zealand": "Asia Pacific",
    "taiwan": "Asia Pacific", "bangladesh": "Asia Pacific",
    "pakistan": "Asia Pacific",
    # Latin America
    "brazil": "Latin America", "br": "Latin America",
    "argentina": "Latin America", "colombia": "Latin America",
    "chile": "Latin America", "peru": "Latin America",
    "venezuela": "Latin America", "ecuador": "Latin America",
}


def _classify_region(country_name: str, city: str) -> str:
    combined = (country_name + " " + city).strip().lower()
    if not combined:
        return "Global"
    for key, region in _COUNTRY_TO_REGION.items():
        if key in combined:
            return region
    return "Global"


# ─────────────────────────────────────────────────────────────────
# FIELD EXTRACTION
# ─────────────────────────────────────────────────────────────────

# Maps any known column variant → canonical field name used in _get()
_FIELD_ALIASES: dict[str, str] = {
    # id
    "id": "id",
    # full_name (vendor new-schema field)
    "full_name": "FullName", "full name": "FullName", "fullname": "FullName",
    "name": "FullName", "contact name": "FullName", "person name": "FullName",
    "employee name": "FullName",
    # First / Last (legacy split)
    "firstname": "FirstName", "first_name": "FirstName", "first name": "FirstName",
    "given name": "FirstName", "givenname": "FirstName", "fname": "FirstName",
    "lastname": "LastName", "last_name": "LastName", "last name": "LastName",
    "surname": "LastName", "family name": "LastName", "lname": "LastName",
    # job_title (new primary signal)
    "job_title": "Designation", "job title": "Designation", "jobtitle": "Designation",
    "designation": "Designation", "title": "Designation",
    "position": "Designation", "role": "Designation",
    "current title": "Designation", "currenttitle": "Designation",
    "current position": "Designation",
    # linkedin_headline (secondary NLP signal)
    "linkedin_headline": "linkedin_headline", "headline": "linkedin_headline",
    "linkedin headline": "linkedin_headline",
    # job_function (soft tiebreaker — guidance only, never authoritative)
    "job_function": "job_function", "job function": "job_function",
    "function": "job_function",
    # job_level (LinkedIn level string — layer fallback)
    "job_level": "job_level", "job level": "job_level",
    "seniority": "job_level", "vendor_level": "job_level",
    # company_name (new field name)
    "company_name": "Company", "company": "Company", "companyname": "Company",
    "organization": "Company", "organisation": "Company",
    "employer": "Company", "firm": "Company",
    # email_domain (used for leadership injection and company fallback)
    "email_domain": "email_domain",
    # linkedin_url
    "linkedin_url": "LinkedInURL", "linkedin": "LinkedInURL",
    "linkedinurl": "LinkedInURL", "linkedin url": "LinkedInURL",
    "linkedin profile": "LinkedInURL", "profile url": "LinkedInURL",
    # job_org_linkedin_url (company fallback)
    "job_org_linkedin_url": "job_org_linkedin_url",
    # location fields
    "city": "city", "country_name": "country_name", "country": "country_name",
    "location": "Location", "country_code": "country_code",
    "job_country": "country_name", "job_city": "city",
    "job_location_country": "country_name",
    "job_location_country_code": "country_code",
    "job_location_city": "city",
    # misc new fields (passed through, not used in classification)
    "job_count": "job_count", "job_is_current": "job_is_current",
    "linkedin_connections_count": "linkedin_connections_count",
    # industry hint
    "industry_hint": "Industry_Hint", "industry": "Industry_Hint",
    "sector": "Industry_Hint", "linkedin_industry": "Industry_Hint",
    # department — accepted in alias table so the field appears in records,
    # but _classify_record() NEVER reads it for classification.
    "department": "department", "dept": "department",
}


def _get(record: dict, *canonical_names: str) -> str:
    """Return first non-empty value matching any canonical field name."""
    for name in canonical_names:
        val = record.get(name, "")
        if val and str(val).strip() and str(val).strip().lower() not in ("nan", "none", "null"):
            return str(val).strip()
    for raw_key, raw_val in record.items():
        normalised_key = str(raw_key).lower().strip()
        canonical = _FIELD_ALIASES.get(normalised_key)
        if canonical in canonical_names:
            if raw_val and str(raw_val).strip().lower() not in ("nan", "none", "null"):
                return str(raw_val).strip()
    return ""


def _extract_name(record: dict) -> str:
    first = _get(record, "FirstName")
    last  = _get(record, "LastName")
    if first or last:
        return f"{first} {last}".strip()
    full = _get(record, "FullName")
    if full:
        return full
    return "Unknown"


# ─────────────────────────────────────────────────────────────────
# OUTPUT RECORD (ClassifiedRecord — unchanged interface for structural_engine)
# ─────────────────────────────────────────────────────────────────

@dataclass
class ClassifiedRecord:
    id: str
    full_name: str
    designation: str
    company: str
    linkedin_url: str
    location: str
    country: str          # ISO / plain country name (e.g. "United Kingdom", "India")
    sector: str
    region: str
    layer: int
    dept_primary: str
    dept_secondary: str
    dept_tertiary: str
    nlp_confidence: float = 0.0
    nlp_industry: str = "generic"
    nlp_method: str = "fallback"


# ─────────────────────────────────────────────────────────────────
# INFERENCE ENGINE
# ─────────────────────────────────────────────────────────────────

class InferenceEngine:
    """
    Classifies raw person records into ClassifiedRecord objects.

    Uses the keyword-scoring classifier (classifier.py) with:
      - job_title + linkedin_headline as primary NLP signals
      - job_function as a soft tiebreaker only
      - department field is completely ignored
    """

    def __init__(self, industry: str = "") -> None:
        # One of the 37 canonical industries from Global_Org_Hierarchy.xlsx.
        # When set, passed to classifier.classify() for industry-aware layer
        # rules and department-scoring boosts.
        self.industry = industry

    def classify_record(self, record: dict) -> ClassifiedRecord:
        # ── Extract primary NLP signals ───────────────────────────────────
        job_title  = _get(record, "Designation") or ""
        headline   = _get(record, "linkedin_headline") or ""
        job_function = _get(record, "job_function") or ""
        job_level  = _get(record, "job_level") or ""

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

        # ── Location ──────────────────────────────────────────────────────
        city         = _get(record, "city") or ""
        country_name = _get(record, "country_name") or ""
        location     = _get(record, "Location") or city or country_name

        # ── LinkedIn URL ──────────────────────────────────────────────────
        linkedin_url = _get(record, "LinkedInURL") or ""

        # ── Full name ─────────────────────────────────────────────────────
        full_name = _extract_name(record)

        # ── Record ID ─────────────────────────────────────────────────────
        rec_id = _get(record, "id") or str(uuid.uuid4())

        # ── Effective title for classification ────────────────────────────
        effective_title = job_title or headline

        # ── Core classification via classifier.py ─────────────────────────
        # department field intentionally not passed — ignored per design
        result: TitleClassification = _classify_title(
            job_title=effective_title,
            linkedin_headline=headline,
            job_function=job_function,
            job_level=job_level,
            industry=self.industry,
        )

        dept_primary   = result.dept_primary
        dept_secondary = result.dept_secondary
        dept_tertiary  = ""
        layer          = result.layer

        # ── Bare dept name safety net ─────────────────────────────────────
        # When the designation is *only* a dept keyword with no seniority
        # signal (e.g. bare "Sales"), cap the layer at 8+ (never a leader).
        if effective_title:
            bare_dept, is_bare = _bare_dept_check(effective_title)
            if is_bare and bare_dept:
                dept_primary = bare_dept
                layer = max(layer, 8)
                logger.debug(
                    "Bare dept designation '%s' → %s L%d",
                    effective_title, bare_dept, layer,
                )

        # ── Industry hint for sector ──────────────────────────────────────
        industry_hint = _get(record, "Industry_Hint") or ""

        # ── Sector ────────────────────────────────────────────────────────
        sector = _classify_sector(company, effective_title, industry_hint)

        # ── Region ────────────────────────────────────────────────────────
        region = _classify_region(country_name, city)

        return ClassifiedRecord(
            id=rec_id,
            full_name=full_name,
            designation=effective_title,
            company=company,
            linkedin_url=linkedin_url,
            location=location,
            country=country_name,     # plain country name from source record
            sector=sector,
            region=region,
            layer=layer,
            dept_primary=dept_primary,
            dept_secondary=dept_secondary,
            dept_tertiary=dept_tertiary,
            nlp_confidence=result.confidence,
            nlp_industry=self.industry or "generic",
            nlp_method=result.method,
        )

    def classify_all(
        self,
        records: list[dict],
        industry: str = "",
    ) -> list[ClassifiedRecord]:
        """
        Classify all records. Skips empty records.
        Compound designations like "CEO and CHRO" produce two entries.

        *industry* overrides the instance's industry when provided.
        """
        if industry:
            self.industry = industry
        classified: list[ClassifiedRecord] = []
        skipped = 0

        for i, rec in enumerate(records):
            try:
                # ── Skip former / retired / non-current employees ─────────
                job_is_current = str(_get(rec, "job_is_current") or "").strip().lower()
                if job_is_current in ("false", "0", "no", "n"):
                    skipped += 1
                    continue

                designation = _get(rec, "Designation") or _get(rec, "linkedin_headline") or ""

                # Skip titles that explicitly signal a former/retired role
                if designation and _FORMER_TITLE_RE.search(designation):
                    logger.debug("Skipping former/retired record: %s", designation)
                    skipped += 1
                    continue

                titles = _split_compound_title(designation)

                if len(titles) > 1:
                    for t in titles:
                        rec_copy = dict(rec)
                        rec_copy["Designation"] = t
                        cr = self.classify_record(rec_copy)
                        if cr.full_name not in ("Unknown", "") or cr.designation:
                            classified.append(cr)
                    continue

                cr = self.classify_record(rec)
                if cr.full_name in ("Unknown", "") and not cr.designation:
                    skipped += 1
                    continue
                classified.append(cr)

            except Exception as e:
                logger.warning("Record %d classification failed: %s", i, e)
                skipped += 1

        logger.info("Classified %d records (skipped %d)", len(classified), skipped)
        return classified


def classify_records(records: list[dict]) -> list[ClassifiedRecord]:
    """Module-level convenience wrapper."""
    return InferenceEngine().classify_all(records)
