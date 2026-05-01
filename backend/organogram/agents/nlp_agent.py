"""
Agent 3 — LinkedIn NLP Agent.

Responsibility: parse, translate, and normalize LinkedIn-sourced titles
into structured NormalizedPerson records.

Resolution order (per design decision #1 for vendor data):
  1. Vendor classification (JOB_FUNCTION + JOB_LEVEL) — primary signal
     when present, validated against the rule library
  2. Region overlay (CSV)
  3. Archetype cascade
  4. Unclassified

When the vendor classification disagrees with the rule library, the rule
library wins and the conflict is logged in the NormalizedPerson's
inference_note (so analysts can audit drift over time).

This agent is FULLY DETERMINISTIC. No LLM calls.
A DeepL/Google Translate hook is exposed but defaults to off.
"""
from __future__ import annotations
from typing import Callable, Optional
import unicodedata

from ..schemas.types import PersonRecord, NormalizedPerson
from ..utils.rule_loader import RuleLibrary
from ..utils import vendor_mapper


# ---------------------------------------------------------------------------
# Crude language detection.
# ---------------------------------------------------------------------------
def _has_cjk(s: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff'
               or '\u3040' <= c <= '\u30ff'
               or '\uac00' <= c <= '\ud7af'
               for c in s)


def _has_cyrillic(s: str) -> bool:
    return any('\u0400' <= c <= '\u04ff' for c in s)


def _has_arabic(s: str) -> bool:
    return any('\u0600' <= c <= '\u06ff' for c in s)


def _has_diacritics(s: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", s))


def detect_script(title: str) -> str:
    if _has_cjk(title): return "cjk"
    if _has_cyrillic(title): return "cyrillic"
    if _has_arabic(title): return "arabic"
    if _has_diacritics(title): return "latin_diacritics"
    return "latin_plain"


# ---------------------------------------------------------------------------
# Region inference.
# ---------------------------------------------------------------------------
COMPANY_COUNTRY_HINTS = {
    "GmbH": "Germany", "AG": "Germany",
    "S.A.": "France", "SAS": "France", "SARL": "France",
    "K.K.": "Japan", "株式会社": "Japan",
    "Pvt Ltd": "India", "Limited": "UK",
    "LLC": "USA", "Inc.": "USA", "Inc": "USA", "Corp.": "USA",
    "Pte Ltd": "Singapore",
    "S.p.A.": "Italy", "S.r.l.": "Italy",
    "B.V.": "Netherlands",
    # African legal forms
    "(Pty) Ltd": "Africa", "Pty Ltd": "Africa",
    "S.A.R.L.": "Africa",   # common in Francophone Africa
    "SARL": "Africa",        # Francophone Africa (overrides France when no other signal)
    "OOO": "Russia", "АО": "Russia", "ПАО": "Russia",  # Russian legal forms
}

# ISO country code -> engine region name
COUNTRY_CODE_TO_REGION = {
    # Americas
    "US": "USA", "CA": "Canada", "BR": "Brazil", "MX": "Mexico",
    # Europe
    "GB": "UK", "UK": "UK",
    "DE": "Germany", "FR": "France", "IT": "Italy",
    "CH": "Switzerland",
    "NL": "Netherlands", "BE": "Belgium", "ES": "Spain", "PT": "Portugal",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PL": "Poland", "CZ": "Czech Republic", "HU": "Hungary", "RO": "Romania",
    "AT": "Austria", "IE": "Ireland", "GR": "Greece",
    # Asia-Pacific
    "JP": "Japan", "KR": "South Korea", "CN": "China",
    "IN": "India", "SG": "Singapore", "AU": "Australia",
    "ID": "Indonesia", "MY": "Malaysia", "TH": "Thailand",
    "VN": "Vietnam", "PH": "Philippines",
    "NZ": "Australia",   # treat NZ same as AU for overlay purposes
    "HK": "China",       # Hong Kong → China overlay
    "TW": "China",       # Taiwan → China overlay
    # GCC / Middle East
    "AE": "GCC", "SA": "GCC", "QA": "GCC", "KW": "GCC", "BH": "GCC", "OM": "GCC",
    "YE": "GCC", "JO": "GCC", "LB": "GCC", "IQ": "GCC",
    # Africa — sub-Saharan
    "ZA": "Africa",   # South Africa
    "NG": "Africa",   # Nigeria
    "KE": "Africa",   # Kenya
    "GH": "Africa",   # Ghana
    "TZ": "Africa",   # Tanzania
    "ET": "Africa",   # Ethiopia
    "UG": "Africa",   # Uganda
    "ZM": "Africa",   # Zambia
    "ZW": "Africa",   # Zimbabwe
    "MZ": "Africa",   # Mozambique
    "AO": "Africa",   # Angola
    "CM": "Africa",   # Cameroon
    "CI": "Africa",   # Côte d'Ivoire
    "SN": "Africa",   # Senegal
    "RW": "Africa",   # Rwanda
    "BW": "Africa",   # Botswana
    "MU": "Africa",   # Mauritius
    # Africa — North (Arabic-speaking)
    "EG": "Africa",   # Egypt
    "DZ": "Africa",   # Algeria
    "MA": "Africa",   # Morocco
    "TN": "Africa",   # Tunisia
    "LY": "Africa",   # Libya
    "SD": "Africa",   # Sudan
    # Russia
    "RU": "Russia",
    # CIS / Russian-language corporate sphere
    "KZ": "Russia",   # Kazakhstan
    "BY": "Russia",   # Belarus
    "UZ": "Russia",   # Uzbekistan
    "TM": "Russia",   # Turkmenistan
    "TJ": "Russia",   # Tajikistan
    "KG": "Russia",   # Kyrgyzstan
    "AZ": "Russia",   # Azerbaijan (Russian widely used in business)
    "AM": "Russia",   # Armenia (Russian widely used in business)
}


def infer_region(person: PersonRecord, default: str = "USA") -> str:
    """
    Infer region from (in priority order):
      1. JOB_LOCATION_COUNTRY_CODE  (most authoritative — vendor field)
      2. JOB_LOCATION_COUNTRY name
      3. legacy `geography` field
      4. native script of the title
      5. company suffix (LLC, GmbH, etc.)
      6. default
    """
    # 1. JOB_LOCATION_COUNTRY_CODE
    if person.job_country_code:
        region = COUNTRY_CODE_TO_REGION.get(person.job_country_code.upper())
        if region:
            return region

    # 2. JOB_LOCATION_COUNTRY  (full country name)
    if person.job_country:
        c = person.job_country.strip()
        # Some normalization for common variants
        if c.lower() in {"united states", "us", "usa"}: return "USA"
        if c.lower() in {"united kingdom", "uk", "britain"}: return "UK"
        if c in COUNTRY_CODE_TO_REGION.values():
            return c
        return c  # take as-is; archetype lookup may not match but country still records

    # 3. Legacy geography field
    if person.geography:
        g = person.geography.strip()
        if g.lower() in {"us", "u.s.", "united states", "usa"}: return "USA"
        if g.lower() in {"uk", "u.k.", "united kingdom", "britain"}: return "UK"
        return g

    # 4. Script-based inference
    script = detect_script(person.title or "")
    if script == "cjk":
        if any('\u3040' <= c <= '\u30ff' for c in person.title): return "Japan"
        if any('\uac00' <= c <= '\ud7af' for c in person.title): return "South Korea"
        return "China"

    # 5. Company suffix
    if person.company:
        for suffix, country in COMPANY_COUNTRY_HINTS.items():
            if suffix in person.company:
                return country

    return default


# ---------------------------------------------------------------------------
# Translation hook.
# ---------------------------------------------------------------------------
def identity_translate(text: str, target_lang: str = "en") -> str:
    return text


# ---------------------------------------------------------------------------
# The NLP Agent
# ---------------------------------------------------------------------------
class LinkedInNLPAgent:
    """Agent 3 — deterministic title normalization."""

    UNCLASSIFIED_FUNCTION = "Unclassified"
    UNCLASSIFIED_LEVEL = 99

    def __init__(
        self,
        rules: RuleLibrary,
        archetype_id: str,
        sub_industry: Optional[str] = None,
        default_region: str = "USA",
        translator: Callable[[str, str], str] = identity_translate,
    ):
        self.rules = rules
        self.archetype_id = archetype_id
        self.sub_industry = sub_industry
        self.default_region = default_region
        self.translator = translator

    def normalize(self, person: PersonRecord) -> NormalizedPerson:
        title_native = person.title or ""
        region = infer_region(person, self.default_region)

        # ── Step A: get rule-library opinion (overlay first, then cascade) ──
        overlay_hit = self.rules.lookup_title(
            title_native, region, self.archetype_id, self.sub_industry
        )
        title_en = title_native
        translated = False
        if not overlay_hit and detect_script(title_native) != "latin_plain":
            title_en = self.translator(title_native, "en")
            translated = True
            if title_en and title_en != title_native:
                overlay_hit = self.rules.lookup_title(
                    title_en, region, self.archetype_id, self.sub_industry
                )

        cascade_hit = None
        if not overlay_hit:
            cascade_hit = self.rules.cascade_lookup(
                title_en or title_native, self.archetype_id
            )

        rule_function = (overlay_hit or cascade_hit or {}).get("function")
        rule_level = (overlay_hit or cascade_hit or {}).get("level")
        if isinstance(rule_level, str) and rule_level:
            try:
                rule_level = int(rule_level)
            except ValueError:
                rule_level = None
        rule_normalized_title = (overlay_hit or cascade_hit or {}).get("normalized_title")

        # ── Step B: get vendor opinion (if present) ──
        vendor_classification = None
        if person.vendor_function or person.vendor_level or person.vendor_persona:
            vendor_classification = vendor_mapper.classify(
                person.vendor_function or "",
                person.vendor_level or "",
                person.vendor_persona or "",
                title_native,
                self.archetype_id,
            )

        # ── Step C: reconcile vendor opinion vs rule opinion (decision #1: rule wins on conflict) ──
        function, level, normalized_title, matched, note = self._reconcile(
            rule_function, rule_level, rule_normalized_title,
            overlay_hit, cascade_hit,
            vendor_classification,
            title_native, title_en,
        )

        # ── Step D: package ──
        country = self._country_from_region(region, person)

        return NormalizedPerson(
            name=person.name,
            title_native=title_native,
            title_en=normalized_title or title_en or title_native,
            function=function,
            inferred_level=level,
            region=region,
            source_url=person.source_url,
            source_type="linkedin",
            company=person.company,
            legal_entity=person.subsidiary or person.company,
            country=country,
            matched_rule=matched,
            inference_note=note,
        )

    # ------------------------------------------------------------------
    def _reconcile(
        self,
        rule_function, rule_level, rule_normalized_title,
        overlay_hit, cascade_hit,
        vendor: Optional[vendor_mapper.VendorClassification],
        title_native: str, title_en: str,
    ) -> tuple[str, int, str, Optional[str], Optional[str]]:
        """
        Return (function, level, normalized_title, matched_rule, note).

        Resolution policy:
          - If rule library has BOTH function and level -> use rule library.
            If vendor disagrees, log the conflict.
          - If rule library has only partial info, fill the gaps from vendor.
          - If rule library is silent and vendor has BOTH -> use vendor.
          - Otherwise -> Unclassified.
        """
        rule_has_full = rule_function is not None and rule_level is not None
        vendor_has_full = (vendor is not None
                           and vendor.function is not None
                           and vendor.level is not None)

        # Rule library is decisive
        if rule_has_full:
            function = rule_function
            level = rule_level
            normalized_title = rule_normalized_title or title_en or title_native
            matched = self._matched_rule_label(overlay_hit, cascade_hit)
            note = None

            # Special case: when the rule match came from a CASCADE substring/loose
            # hit on a bare common word (e.g., "Manager", "Director", "Engineer"),
            # the rule's function attribution is unreliable — it just picked the
            # first ladder containing that word. If vendor has a concrete function,
            # prefer the vendor's function and keep the rule's level.
            cascade_only = overlay_hit is None and cascade_hit is not None
            generic_titles = {"manager", "director", "engineer", "analyst",
                              "associate", "specialist", "coordinator", "lead",
                              "senior manager", "senior director"}
            title_is_generic = title_native.strip().lower() in generic_titles

            if (cascade_only and title_is_generic
                    and vendor is not None and vendor.function is not None
                    and vendor.function != rule_function):
                function = vendor.function
                # Keep rule level (more accurate than vendor level usually)
                note = (
                    f"Generic title '{title_native}'; cascade picked "
                    f"function '{rule_function}' arbitrarily. "
                    f"Vendor function '{vendor.function}' used instead."
                )
                return function, level, normalized_title, matched + "+vendor-fn", note

            # Conflict logging — only flag MEANINGFUL disagreements:
            #   - function name differs, OR
            #   - level differs by 2 or more
            if vendor is not None:
                fn_disagree = (vendor.function and vendor.function != rule_function)
                lvl_disagree_big = (vendor.level
                                    and abs(vendor.level - rule_level) >= 2)
                if fn_disagree or lvl_disagree_big:
                    note = (
                        f"Vendor classification ({vendor.raw_function}/L{vendor.level}) "
                        f"conflicts with rule library ({rule_function}/L{rule_level}); "
                        f"rule library used per design decision."
                    )
            return function, level, normalized_title, matched, note

        # Rule library partial — fill gaps from vendor
        if rule_function is not None or rule_level is not None:
            function = rule_function or (vendor.function if vendor else None) or self.UNCLASSIFIED_FUNCTION
            level = rule_level or (vendor.level if vendor else None) or self.UNCLASSIFIED_LEVEL
            normalized_title = rule_normalized_title or title_en or title_native
            matched = self._matched_rule_label(overlay_hit, cascade_hit) + "+vendor"
            note = "Hybrid: rule library + vendor classification merged."
            return function, level, normalized_title, matched, note

        # Rule library silent, vendor has full
        if vendor_has_full:
            return (
                vendor.function,
                vendor.level,
                title_en or title_native,
                f"vendor:{vendor.raw_function}|{vendor.raw_level}",
                "Resolved by vendor classification (rule library silent).",
            )

        # Vendor partial only
        if vendor is not None and (vendor.function or vendor.level):
            function = vendor.function or self.UNCLASSIFIED_FUNCTION
            level = vendor.level or self.UNCLASSIFIED_LEVEL
            return (
                function, level,
                title_en or title_native,
                f"vendor-partial:{vendor.raw_function}|{vendor.raw_level}",
                "Resolved by partial vendor classification (rule library silent).",
            )

        # Nothing matched
        return (
            self.UNCLASSIFIED_FUNCTION,
            self.UNCLASSIFIED_LEVEL,
            title_en or title_native,
            None,
            "Unresolved — no overlay, cascade, or vendor classification available.",
        )

    @staticmethod
    def _matched_rule_label(overlay_hit, cascade_hit) -> str:
        if overlay_hit:
            return f"overlay:{overlay_hit['title_raw']}|{overlay_hit['region']}|{overlay_hit['archetype_id']}"
        if cascade_hit:
            return "cascade"
        return "none"

    @staticmethod
    def _country_from_region(region: str, person: PersonRecord) -> Optional[str]:
        """
        Country tag for the geographic view. Prefer the explicit JOB_LOCATION_COUNTRY
        when present; otherwise use the inferred region if it's a country.
        Normalize common variants (United States -> USA, etc.).
        """
        # Canonical normalization map
        country_norm = {
            "united states": "USA", "us": "USA", "u.s.": "USA", "u.s.a.": "USA",
            "united kingdom": "UK", "u.k.": "UK", "britain": "UK",
            "south korea": "South Korea", "republic of korea": "South Korea",
        }

        candidate = person.job_country or region
        if not candidate:
            return None
        norm = country_norm.get(candidate.lower().strip(), candidate)

        if norm in {
            # Americas
            "USA", "Canada", "Brazil", "Mexico",
            # Europe
            "UK", "Germany", "France", "Italy", "Switzerland", "Netherlands",
            "Belgium", "Spain", "Portugal", "Sweden", "Norway", "Denmark",
            "Finland", "Poland", "Czech Republic", "Hungary", "Romania",
            "Austria", "Ireland", "Greece",
            # Asia-Pacific
            "India", "Japan", "China", "South Korea", "Singapore", "Australia",
            "Indonesia", "Malaysia", "Thailand", "Vietnam", "Philippines",
            # GCC
            "UAE", "Saudi Arabia", "Qatar", "Kuwait", "Bahrain", "Oman",
            # Africa — sub-Saharan
            "South Africa", "Nigeria", "Kenya", "Ghana", "Tanzania",
            "Ethiopia", "Uganda", "Zambia", "Zimbabwe", "Mozambique",
            "Angola", "Cameroon", "Côte d'Ivoire", "Senegal", "Rwanda",
            "Botswana", "Mauritius",
            # Africa — North
            "Egypt", "Algeria", "Morocco", "Tunisia", "Libya", "Sudan",
            # Russia / CIS
            "Russia", "Kazakhstan", "Belarus", "Uzbekistan",
            "Turkmenistan", "Tajikistan", "Kyrgyzstan", "Azerbaijan", "Armenia",
        }:
            return norm
        # If we don't recognize it but the field is non-empty, keep the original
        return norm

    def normalize_all(self, persons: list[PersonRecord]) -> list[NormalizedPerson]:
        return [self.normalize(p) for p in persons]
