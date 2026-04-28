"""
Universal Organogram Engine - NLP Engine
=========================================
Loads per-industry YAML directories and uses fuzzy matching (rapidfuzz)
to parse designations, infer industry, classify seniority layers, and
extract departments.

Seniority Layer Scale (L0 = most senior, L10 = most junior):
  L0   Board / Non-Executive / Chairman
  L1   C-Suite (CEO, CFO, CTO, CHRO, CMO …)          ← docx L10
  L2   President / EVP / Managing Director            ← docx L9
  L3   SVP / General Manager / Country Head           ← docx L8–L9
  L4   VP / AVP / Senior Director / Head of           ← docx L8
  L5   Director / Head                                ← docx L7
  L6   Senior Manager / Associate Director / Deputy   ← docx L6
  L7   Manager / Team Lead / Project Manager          ← docx L5
  L8   Senior IC / Lead / Staff / Principal           ← docx L4
  L9   Analyst / Specialist / Associate / Engineer    ← docx L2–L3
  L10  Graduate / Intern / Junior / Trainee           ← docx L1

Department taxonomy (from Org Hierarchy reference):
  Generic cross-industry names only — no "Manufacturing & Operations",
  "Vehicle Sales", or "Upstream / E&P" — real companies don't label
  internal departments with industry-classification language.
  See dept_taxonomy.py for the full 4-level hierarchy.
"""

from __future__ import annotations

import re
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

try:
    from rapidfuzz import fuzz, process as rf_process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    logging.warning("rapidfuzz not installed – falling back to exact matching only")

try:
    from dept_taxonomy import build_universal_depts as _build_universal_depts
    _TAXONOMY_DEPTS = _build_universal_depts()
except Exception as _e:
    logging.warning(f"dept_taxonomy not loaded: {_e}. Falling back to inline UNIVERSAL_DEPTS.")
    _TAXONOMY_DEPTS = None

logger = logging.getLogger(__name__)

INDUSTRIES_DIR = Path(__file__).parent / "industries"

# ─────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────

@dataclass
class LayerDef:
    layer: int
    label: str
    canonical: list[str]
    patterns: list[str]
    abbreviations: dict[str, str]   # "CEO" -> "Chief Executive Officer"

@dataclass
class DeptSecondary:
    name: str
    keywords: list[str]

@dataclass
class DeptPrimary:
    primary: str
    keywords: list[str]
    secondaries: list[DeptSecondary]

@dataclass
class IndustryDirectory:
    id: str
    name: str
    sector: str
    detection_keywords: dict[str, list[str]]  # company/industry_hint/designation
    hierarchy: list[LayerDef]
    departments: list[DeptPrimary]

    # Pre-compiled patterns (filled after load)
    _compiled_patterns: dict[int, list[re.Pattern]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def compile(self):
        """Pre-compile all regex patterns for fast repeated use."""
        for ld in self.hierarchy:
            self._compiled_patterns[ld.layer] = [
                re.compile(p, re.IGNORECASE) for p in ld.patterns
            ]
        return self

    def get_layer_def(self, layer: int) -> Optional[LayerDef]:
        for ld in self.hierarchy:
            if ld.layer == layer:
                return ld
        return None

    @property
    def all_abbreviations(self) -> dict[str, str]:
        """Flat merged dict of all abbreviation expansions across all layers."""
        result: dict[str, str] = {}
        for ld in self.hierarchy:
            if isinstance(ld.abbreviations, dict):
                result.update(ld.abbreviations)
        return result


@dataclass
class ClassificationResult:
    layer: int
    confidence: float          # 0.0 – 1.0
    matched_industry: str      # industry id
    match_method: str          # "exact", "fuzzy", "pattern", "fallback"
    matched_title: str         # the title/pattern that triggered the match
    dept_primary: str
    dept_secondary: str
    dept_tertiary: str


# ─────────────────────────────────────────────────────────────────
# LOADER
# ─────────────────────────────────────────────────────────────────

class IndustryDirectoryLoader:
    """Loads all YAML files from the industries/ directory."""

    _cache: Optional[list[IndustryDirectory]] = None

    @classmethod
    def load_all(cls) -> list[IndustryDirectory]:
        if cls._cache is not None:
            return cls._cache

        dirs: list[IndustryDirectory] = []
        if not INDUSTRIES_DIR.exists():
            logger.warning(f"Industries directory not found: {INDUSTRIES_DIR}")
            return dirs

        for yaml_path in sorted(INDUSTRIES_DIR.glob("*.yaml")):
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                ind = cls._parse(raw)
                ind.compile()
                dirs.append(ind)
                logger.debug(f"Loaded industry: {ind.name} ({yaml_path.name})")
            except Exception as e:
                logger.error(f"Failed to load {yaml_path.name}: {e}")

        cls._cache = dirs
        logger.info(f"Loaded {len(dirs)} industry directories")
        return dirs

    @staticmethod
    def _parse(raw: dict) -> IndustryDirectory:
        # Parse hierarchy layers
        hierarchy: list[LayerDef] = []
        for lyr in raw.get("hierarchy", []):
            abbrevs = lyr.get("abbreviations", {})
            if not isinstance(abbrevs, dict):
                abbrevs = {}
            hierarchy.append(LayerDef(
                layer=int(lyr["layer"]),
                label=lyr.get("label", ""),
                canonical=[c for c in lyr.get("canonical", []) if c],
                patterns=lyr.get("patterns", []),
                abbreviations=abbrevs,
            ))

        # Parse departments
        departments: list[DeptPrimary] = []
        for dept in raw.get("departments", []):
            secondaries = [
                DeptSecondary(name=s["name"], keywords=s.get("keywords", []))
                for s in dept.get("secondaries", [])
            ]
            departments.append(DeptPrimary(
                primary=dept["primary"],
                keywords=dept.get("keywords", []),
                secondaries=secondaries,
            ))

        return IndustryDirectory(
            id=raw.get("id", "unknown"),
            name=raw.get("name", "Unknown"),
            sector=raw.get("sector", "Private"),
            detection_keywords=raw.get("detection_keywords", {}),
            hierarchy=hierarchy,
            departments=departments,
        )


# ─────────────────────────────────────────────────────────────────
# TITLE NORMALISER
# ─────────────────────────────────────────────────────────────────

# Common filler prefixes/suffixes that don't change the seniority level
_STRIP_MODIFIERS = re.compile(
    r"\b(group|global|regional|local|divisional|interim|acting|deputy|associate|junior|"
    r"assistant|senior|sr|jr|corporate|enterprise|principal|lead|uk|emea|apac|americas|"
    r"anz|latam|mena|india|europe|asia|africa)\b",
    re.IGNORECASE,
)

# Punctuation / redundant whitespace cleanup
_CLEAN_RE = re.compile(r"[,;|/\\]+")
_SPACES_RE = re.compile(r"\s{2,}")


class TitleNormaliser:
    """
    Cleans and expands a raw designation string.
    Steps:
      1. Expand known abbreviations from all industry directories
      2. Strip geographic/scope modifiers
      3. Lowercase + remove special chars
    """

    def __init__(self, abbreviations: dict[str, str]):
        # Build a sorted-by-length dict so longer abbreviations match first
        self._abbrevs = dict(
            sorted(abbreviations.items(), key=lambda kv: -len(kv[0]))
        )

    def expand(self, title: str) -> str:
        """Expand abbreviations e.g. 'Group CFO & COO' → 'Group Chief Financial Officer & Chief Operating Officer'"""
        result = title
        for abbr, full in self._abbrevs.items():
            # Word-boundary-aware substitution
            pattern = r'(?<![A-Za-z])' + re.escape(abbr) + r'(?![A-Za-z])'
            result = re.sub(pattern, full, result, flags=re.IGNORECASE)
        return result

    def normalise(self, title: str) -> str:
        """Full normalisation pipeline → lowercase stripped form."""
        if not title:
            return ""
        expanded = self.expand(title)
        cleaned = _CLEAN_RE.sub(" ", expanded)
        cleaned = _SPACES_RE.sub(" ", cleaned).strip().lower()
        return cleaned

    def strip_modifiers(self, title: str) -> str:
        """Remove scope/seniority modifiers to get base role."""
        stripped = _STRIP_MODIFIERS.sub("", title)
        return _SPACES_RE.sub(" ", stripped).strip()


def _build_global_normaliser(dirs: list[IndustryDirectory]) -> TitleNormaliser:
    merged: dict[str, str] = {}
    for d in dirs:
        merged.update(d.all_abbreviations)
    return TitleNormaliser(merged)


# ─────────────────────────────────────────────────────────────────
# INDUSTRY MATCHER
# ─────────────────────────────────────────────────────────────────

@dataclass
class IndustryScore:
    industry: IndustryDirectory
    score: float   # 0–100


class IndustryMatcher:
    """
    Scores a record against each industry directory using:
    - company name keywords
    - industry_hint keywords
    - designation keywords
    Returns the top-scoring industry (or None if below threshold).
    """

    THRESHOLD = 10.0   # minimum score to be considered a match

    def __init__(self, dirs: list[IndustryDirectory]):
        self._dirs = dirs

    def score(self, designation: str, company: str, industry_hint: str) -> list[IndustryScore]:
        des_l = designation.lower()
        co_l  = company.lower()
        hint_l = industry_hint.lower()

        def _kw_in(kw: str, text: str) -> bool:
            """Substring check with word-boundary guard for short keywords."""
            kl = kw.lower()
            if ' ' in kl or '-' in kl:
                return kl in text
            if len(kl) <= 3:
                return bool(re.search(r'(?<![a-z0-9])' + re.escape(kl) + r'(?![a-z0-9])', text))
            return kl in text

        scores: list[IndustryScore] = []
        for ind in self._dirs:
            s = 0.0
            dk = ind.detection_keywords

            # Company keywords
            for kw in dk.get("company", []):
                if _kw_in(kw, co_l):
                    s += 20.0

            # Industry hint keywords
            for kw in dk.get("industry_hint", []):
                if _kw_in(kw, hint_l):
                    s += 25.0

            # Designation keywords
            for kw in dk.get("designation", []):
                if _kw_in(kw, des_l):
                    s += 15.0

            if s > 0:
                scores.append(IndustryScore(industry=ind, score=s))

        scores.sort(key=lambda x: -x.score)
        return scores

    def best(
        self, designation: str, company: str, industry_hint: str
    ) -> Optional[IndustryDirectory]:
        results = self.score(designation, company, industry_hint)
        if results and results[0].score >= self.THRESHOLD:
            return results[0].industry
        return None


# ─────────────────────────────────────────────────────────────────
# LAYER CLASSIFIER
# ─────────────────────────────────────────────────────────────────

_FUZZY_THRESHOLD = 80   # rapidfuzz score threshold (0–100)
_FUZZY_PARTIAL_THRESHOLD = 75


class LayerClassifier:
    """
    Classifies a normalised designation into a seniority layer (0–10)
    using an industry directory. Falls back to a generic fallback
    dictionary when no industry is matched.
    """

    # Fallback layer rules — ordered highest-priority first (BOD → Entry Level)
    # Derived from ProTrail 354k-row title analysis. Rules checked top-to-bottom;
    # first match wins. Longer/more-specific phrases listed before short ones.
    FALLBACK_RULES: list[tuple[int, list[str]]] = [
        # Layer 0 — Board / Non-Executive / Trustees
        (0,  [
            "non-executive director", "non-exec director", "non-executive chairman",
            "non-executive chairwoman", "non executive director", "non-exec",
            "independent non-executive director", "independent non-executive",
            "independent director", "independent non-exec",
            "board of directors", "board member", "board director",
            "supervisory board", "board of trustees", "board of management",
            "board of governors", "board of commissioners",
            "chairman of the board", "chairwoman of the board",
            "lead independent director", "outside director",
            "trustee", "governor",
        ]),
        # Layer 1 — C-Suite / Founder / President / Managing Partner
        # (docx: L10 — "Chief [Function] Officer, President")
        (1,  [
            # Chief X Officer — full forms (longest first)
            "chief executive officer", "chief financial officer",
            "chief operating officer", "chief technology officer",
            "chief information officer", "chief information security officer",
            "chief risk officer", "chief compliance officer",
            "chief digital officer", "chief data officer",
            "chief marketing officer", "chief people officer",
            "chief human resources officer", "chief commercial officer",
            "chief revenue officer", "chief legal officer",
            "chief medical officer", "chief scientific officer",
            "chief strategy officer", "chief accounting officer",
            "chief product officer", "chief growth officer",
            "chief customer officer", "chief transformation officer",
            "chief innovation officer", "chief investment officer",
            "chief privacy officer", "chief analytics officer",
            "chief sustainability officer", "chief experience officer",
            # Head of legal function (GC = functional C-suite per docx)
            "general counsel", "group general counsel",
            # Founder / Co-Founder
            "co-founder & ceo", "founder & ceo", "co-founder and ceo",
            "founder and ceo", "co-founder", "founder",
            # President
            "president & ceo", "president and ceo",
            # Managing Partner / Senior Partner
            "group managing director", "managing partner", "senior partner",
            "chairman & ceo", "chairman and ceo",
        ]),
        # Layer 2 — MD / EVP / Executive Director / Group Head / Global Head
        (2,  [
            "executive vice president", "executive vp",
            "managing director", "group managing director",
            "executive director",
            "group director", "group head",
            "global head",
            "country ceo", "regional ceo", "divisional ceo",
            "divisional managing director", "divisional head",
            "president & managing director", "president and managing director",
            "global president", "group president",
            "principal director",
        ]),
        # Layer 3 — SVP / VP / General Manager / Country Head
        # (docx: L9 Senior Executive — SVP, EVP mapped to our L3)
        (3,  [
            "senior vice president", "svp", "first vice president",
            "vice president", "group vice president", "corporate vice president",
            "regional vice president", "global vice president",
            "general manager", "country head", "regional head",
            "business head", "divisional director",
        ]),
        # Layer 4 — VP-equivalent / Senior Director / Head of X
        # (docx: L8 Executive — VP, AVP; also Senior Director)
        (4,  [
            "associate vice president", "assistant vice president",
            "avp",
            "senior director", "senior managing director",
            "global director", "regional director", "area director",
            "director general", "group director",
            "head of ", "regional head of",
            "zonal director", "zonal head",
        ]),
        # Layer 5 — Director / Head
        # (docx: L7 Director)
        (5,  [
            "director", "head",
        ]),
        # Layer 6 — Senior Manager / Associate Director / Deputy
        # (docx: L6 Senior Management — Senior Manager, Associate Director)
        (6,  [
            "senior manager", "associate director", "deputy director",
            "deputy general manager", "deputy manager", "principal manager",
            "assistant director", "chapter lead", "chapter head",
            "cluster head", "group manager",
        ]),
        # Layer 7 — Manager / Team Lead / Project Manager
        # (docx: L5 Management — Manager, Group Lead, Senior Staff)
        (7,  [
            "manager", "team lead", "team leader", "group lead",
            "branch manager", "relationship manager", "portfolio manager",
            "senior officer", "senior executive",
            "engagement manager", "project manager", "programme manager",
        ]),
        # Layer 8 — Senior IC / Lead / Staff / Principal
        # (docx: L4 Lead/Staff — Lead, Staff, Principal)
        (8,  [
            "senior analyst", "senior specialist", "senior consultant",
            "senior associate", "senior officer", "senior engineer",
            "senior developer", "senior architect", "senior advisor",
            "staff engineer", "staff developer",
            "principal engineer", "principal architect", "principal analyst",
            "lead engineer", "lead developer", "lead analyst", "lead designer",
            "staff software engineer", "senior scientist",
            "senior data scientist", "senior data engineer",
            "senior product manager", "senior designer", "senior researcher",
        ]),
        # Layer 10 — Graduate / Intern / Trainee (checked before Layer 9)
        # (docx: L1-L2 Entry/Junior — Junior, Associate, Graduate, Intern)
        (10, [
            "graduate engineer", "graduate analyst", "graduate trainee",
            "graduate", "intern", "internship", "trainee", "apprentice",
            "placement student", "placement", "student",
            "junior analyst", "junior associate", "junior developer",
            "junior engineer", "junior consultant", "junior specialist",
            "entry level", "entry-level",
        ]),
        # Layer 9 — IC / Analyst / Associate / Specialist
        # (docx: L2-L3 Mid-Senior/Junior — Analyst, Associate, Specialist, Senior prefix)
        (9,  [
            "analyst", "associate", "officer", "consultant", "specialist",
            "engineer", "developer", "advisor", "adviser",
            "scientist", "architect", "designer", "researcher",
            "coordinator", "administrator",
        ]),
    ]

    def __init__(self, dirs: list[IndustryDirectory]):
        self._dirs = dirs
        self._normaliser = _build_global_normaliser(dirs)

    def classify(
        self,
        raw_designation: str,
        industry: Optional[IndustryDirectory] = None,
    ) -> tuple[int, float, str]:
        """
        Returns (layer, confidence, match_method).
        confidence is 0.0–1.0.
        """
        if not raw_designation:
            return 9, 0.1, "fallback"

        # 1. Expand abbreviations then normalise
        normalised = self._normaliser.normalise(raw_designation)

        source_dirs = [industry] if industry else self._dirs

        # 2. Pass 1 across ALL relevant industries: exact match only
        for ind in source_dirs:
            result = self._exact_match(normalised, ind)
            if result is not None:
                conf_mult = 1.0 if industry else 0.9
                layer, conf, method = result
                return layer, conf * conf_mult, method

        # 3. Pass 2 across ALL relevant industries: pattern match only
        for ind in source_dirs:
            result = self._pattern_match(normalised, ind)
            if result is not None:
                conf_mult = 1.0 if industry else 0.85
                layer, conf, method = result
                return layer, conf * conf_mult, method

        # 4. Pass 3 across ALL relevant industries: substring match (longest wins)
        best_layer = None
        best_len   = 0
        for ind in source_dirs:
            result = self._substring_match(normalised, ind)
            if result:
                layer, matched_len = result
                if matched_len > best_len:
                    best_len   = matched_len
                    best_layer = layer
        if best_layer is not None:
            conf_mult = 1.0 if industry else 0.82
            return best_layer, 0.88 * conf_mult, "substring"

        # 5. Fuzzy match across all directories
        if RAPIDFUZZ_AVAILABLE:
            best_fuzzy_layer = None
            best_fuzzy_score = 0.0
            for ind in source_dirs:
                sorted_layers = sorted(ind.hierarchy, key=lambda l: l.layer)
                for ld in sorted_layers:
                    for canonical in ld.canonical:
                        score = fuzz.token_set_ratio(normalised, canonical.lower())
                        if score > best_fuzzy_score:
                            best_fuzzy_score = score
                            best_fuzzy_layer = ld.layer
            if best_fuzzy_score >= _FUZZY_THRESHOLD and best_fuzzy_layer is not None:
                conf_mult = 1.0 if industry else 0.8
                return best_fuzzy_layer, best_fuzzy_score / 100.0 * 0.8 * conf_mult, "fuzzy"

        # 6. Fallback to generic rules
        return self._fallback(normalised)

    def _exact_match(
        self, normalised: str, ind: IndustryDirectory
    ) -> Optional[tuple[int, float, str]]:
        """Pass 1: exact match on normalised canonical titles."""
        sorted_layers = sorted(ind.hierarchy, key=lambda l: l.layer)
        for ld in sorted_layers:
            for canonical in ld.canonical:
                if canonical.lower() == normalised:
                    return ld.layer, 1.0, "exact"
        return None

    def _pattern_match(
        self, normalised: str, ind: IndustryDirectory
    ) -> Optional[tuple[int, float, str]]:
        """Pass 2: compiled regex patterns."""
        sorted_layers = sorted(ind.hierarchy, key=lambda l: l.layer)
        for ld in sorted_layers:
            patterns = ind._compiled_patterns.get(ld.layer, [])
            for pat in patterns:
                if pat.search(normalised):
                    return ld.layer, 0.85, "pattern"
        return None

    def _substring_match(
        self, normalised: str, ind: IndustryDirectory
    ) -> Optional[tuple[int, int]]:
        """
        Pass 3: longest canonical that is a whole-word substring of the normalised title.
        Returns (layer, match_length) or None.
        Uses word-boundary matching to avoid false positives like 'vp' inside 'avp',
        or 'md' inside 'cmd', etc.
        """
        sorted_layers = sorted(ind.hierarchy, key=lambda l: l.layer)
        best_layer = None
        best_len   = 0
        for ld in sorted_layers:
            for canonical in ld.canonical:
                c = canonical.lower()
                if len(c) < 2:
                    continue
                # Require word boundaries around the canonical string
                if re.search(r'(?<![a-z0-9])' + re.escape(c) + r'(?![a-z0-9])', normalised):
                    if len(c) > best_len:
                        best_len   = len(c)
                        best_layer = ld.layer
        if best_layer is not None:
            return best_layer, best_len
        return None

    # Keep for backwards compat (called nowhere now but keeps API clean)
    def _classify_from_directory(
        self, normalised: str, ind: IndustryDirectory
    ) -> Optional[tuple[int, float, str]]:
        result = self._exact_match(normalised, ind)
        if result:
            return result
        result = self._pattern_match(normalised, ind)
        if result:
            return result
        sub = self._substring_match(normalised, ind)
        if sub:
            return sub[0], 0.88, "substring"
        return None

    def _fallback(self, normalised: str) -> tuple[int, float, str]:
        """Generic rule-based fallback covering common titles across industries."""
        for layer, keywords in self.FALLBACK_RULES:
            for kw in keywords:
                if kw in normalised:
                    # For short keywords use word-boundary check
                    if len(kw) <= 5:
                        if re.search(r'(?<![a-z])' + re.escape(kw) + r'(?![a-z])', normalised):
                            return layer, 0.6, "fallback"
                    else:
                        return layer, 0.65, "fallback"

        return 9, 0.3, "fallback"


# ─────────────────────────────────────────────────────────────────
# DEPARTMENT EXTRACTOR
# ─────────────────────────────────────────────────────────────────

class DepartmentExtractor:
    """
    Extracts (dept_primary, dept_secondary, dept_tertiary) from a record
    by scoring designation + company text against industry department keywords.
    Includes a universal keyword table derived from 354k-row ProTrail data.
    """

    # ── Universal primary department taxonomy ─────────────────────────────
    # Loaded from dept_taxonomy.py (GTM Title Library + ProTrail + enterprise framework).
    # Falls back to a minimal inline list if the module is unavailable.
    # Each entry: (dept_primary, [keywords_for_primary], [(secondary, [kws]), ...])
    UNIVERSAL_DEPTS: list[tuple[str, list[str], list[tuple[str, list[str]]]]] = (
        _TAXONOMY_DEPTS if _TAXONOMY_DEPTS is not None else []
    )

    # Build fast lookup: keyword → (primary, secondary)
    _KW_INDEX: list[tuple[str, str, str]] = []  # (keyword, primary, secondary)

    def __init__(self, dirs: list[IndustryDirectory]):
        self._dirs = dirs
        if not DepartmentExtractor._KW_INDEX:
            DepartmentExtractor._build_index()

    @classmethod
    def _build_index(cls):
        idx = []
        for primary, p_kws, secondaries in cls.UNIVERSAL_DEPTS:
            for kw in p_kws:
                idx.append((kw.lower(), primary, ""))
            for sec_name, s_kws in secondaries:
                for kw in s_kws:
                    idx.append((kw.lower(), primary, sec_name))
        # Sort descending by keyword length so longer/more specific matches win
        idx.sort(key=lambda x: -len(x[0]))
        cls._KW_INDEX = idx

    def extract(
        self,
        designation: str,
        company: str,
        industry: Optional[IndustryDirectory] = None,
    ) -> tuple[str, str, str]:
        """Returns (primary, secondary, tertiary) department strings."""
        text = (designation + " " + company).lower()

        # 1. Try YAML industry directory first (highest specificity)
        source_dirs = [industry] if industry else self._dirs
        best_primary   = ""
        best_secondary = ""
        best_p_score   = 0
        best_s_score   = 0

        for ind in source_dirs:
            for dept in ind.departments:
                p_score = self._score_keywords(text, dept.keywords)
                if p_score > best_p_score:
                    best_p_score   = p_score
                    best_primary   = dept.primary
                    best_secondary = ""
                    best_s_score   = 0
                    for sec in dept.secondaries:
                        s_score = self._score_keywords(text, sec.keywords)
                        if s_score > best_s_score:
                            best_s_score   = s_score
                            best_secondary = sec.name

        # 2. Universal keyword index — canonical fallback / normaliser
        uni_primary, uni_secondary = self._universal_match(text)
        if not best_primary:
            # No YAML match at all — use universal
            if uni_primary:
                best_primary   = uni_primary
                best_secondary = uni_secondary
        elif industry is None:
            # No specific industry was identified: the YAML phase scanned ALL industries and
            # may have returned an industry-specific name (e.g. "People & HR" from banking
            # YAML, "Vehicle Sales" from automotive YAML).
            # Replace with universal canonical taxonomy names for consistency.
            # Do NOT fall back to the YAML secondary — it may be industry-specific garbage.
            if uni_primary:
                best_primary   = uni_primary
                best_secondary = uni_secondary   # uni_secondary may be "" — that's fine
            # else: keep best YAML result — at least it's a named category

        # Default
        if not best_primary:
            best_primary   = "General"
            best_secondary = "General"

        # 3. Tertiary from designation (empty secondary stays empty — structural engine handles it)
        tertiary = self._derive_tertiary(designation, best_secondary, best_primary)
        return best_primary, best_secondary, tertiary

    @classmethod
    def _universal_match(cls, text: str) -> tuple[str, str]:
        """Return (primary, secondary) from universal index.
        Uses word-boundary matching so short abbreviations like 'ar', 'er'
        don't falsely match inside longer words (e.g. 'software', 'officer').
        """
        for kw, primary, secondary in cls._KW_INDEX:
            # For keywords with special chars (spaces, &, /) use plain substring
            if any(c in kw for c in (' ', '&', '/', '-')):
                if kw in text:
                    return primary, secondary
            else:
                # Word-boundary check: keyword must not be bordered by a-z or digit
                pat = r'(?<![a-z0-9])' + re.escape(kw) + r'(?![a-z0-9])'
                if re.search(pat, text):
                    return primary, secondary
        return "", ""

    def extract_from_text(self, raw_dept_text: str, hint_primary: str = "") -> tuple[str, str, str]:
        """
        Classify a raw freeform department string (e.g. ProTrail 'Department' field).
        Returns (primary, secondary, tertiary).
        """
        if not raw_dept_text:
            return hint_primary, "", ""
        text = raw_dept_text.lower().strip()
        primary, secondary = self._universal_match(text)
        if not primary:
            primary = hint_primary or "General"
        tertiary = self._derive_tertiary(raw_dept_text, secondary or primary)
        return primary, secondary, tertiary

    @staticmethod
    def _score_keywords(text: str, keywords: list[str]) -> int:
        """Score how well a text matches a keyword list.
        Uses word-boundary matching for single-word keywords to avoid
        false positives (e.g. 'she' in 'Shell', 'it' in 'equity').
        Multi-word phrases use plain substring matching.
        """
        score = 0
        for kw in keywords:
            kl = kw.lower()
            # Multi-word or contains special chars: plain substring
            if ' ' in kl or any(c in kl for c in ('&', '/', '-')):
                if kl in text:
                    score += len(kl.split())
            else:
                # Single-word keyword: require word boundaries
                if re.search(r'(?<![a-z0-9])' + re.escape(kl) + r'(?![a-z0-9])', text):
                    score += 1
        return score

    @staticmethod
    def _derive_tertiary(designation: str, secondary: str, primary: str = "") -> str:
        """
        Derive a tertiary sub-department label from the designation text.
        Labels match the Org Hierarchy reference taxonomy (xlsx) so the tree
        shows real corporate sub-team names, not marketing jargon.
        Returns "" when nothing matches — structural engine skips the tier.
        """
        des_lower = designation.lower()

        # Ordered longest-keyword-first to reduce false positives.
        # Each entry: (sub-dept label, [trigger keywords])
        tiers: list[tuple[str, list[str]]] = [
            # ── Finance sub-departments ──────────────────────────────────────
            ("FP&A",               ["fp&a", "financial planning", "financial planning and analysis",
                                    "budgeting", "forecasting", "variance analysis", "capital allocation"]),
            ("Treasury",           ["treasury", "cash management", "forex", "fx hedging", "debt management",
                                    "liquidity", "foreign exchange"]),
            ("Internal Audit",     ["internal audit", "sox", "sox compliance", "audit committee",
                                    "operational audit"]),
            ("Control & Reporting",["financial control", "general ledger", "consolidation", "fixed assets",
                                    "management accounts", "financial reporting"]),
            ("Financial Operations",["accounts payable", "accounts receivable", "billing operations",
                                    "payroll processing", "ap ar", "procure to pay"]),
            # ── HR sub-departments ───────────────────────────────────────────
            ("Talent Acquisition", ["talent acquisition", "recruiting", "sourcing", "talent sourcing",
                                    "employer branding", "campus hiring", "executive search"]),
            ("Learning & Development", ["learning and development", "l&d", "training", "leadership development",
                                    "organisational development", "learning technology"]),
            ("People Operations",  ["people operations", "people ops", "hris", "employee records",
                                    "compensation", "benefits", "total rewards", "payroll"]),
            ("Employee Relations",  ["employee relations", "labour relations", "labor relations",
                                    "performance management", "de&i", "diversity", "inclusion"]),
            # ── Marketing sub-departments ────────────────────────────────────
            ("Performance Marketing", ["performance marketing", "paid social", "seo", "sem",
                                    "search engine", "ppc", "programmatic", "growth marketing"]),
            ("Product Marketing",  ["product marketing", "market research", "gtm strategy",
                                    "sales enablement", "go-to-market", "competitive intelligence"]),
            ("Brand & Creative",   ["brand", "creative", "content strategy", "pr", "public relations",
                                    "corporate communications", "graphic design"]),
            # ── Sales sub-departments ────────────────────────────────────────
            ("Sales Operations",   ["sales operations", "revenue operations", "revops", "crm",
                                    "sales enablement", "lead generation", "demand generation"]),
            ("Partnerships",       ["partnerships", "channel sales", "alliances", "affiliates",
                                    "channel management", "partner management"]),
            ("Direct Sales",       ["enterprise sales", "inside sales", "field sales", "account executive",
                                    "key accounts", "strategic accounts", "national accounts"]),
            # ── Engineering / IT sub-departments ────────────────────────────
            ("Software Development",["software development", "frontend", "backend", "fullstack",
                                    "mobile development", "devops", "sre", "site reliability"]),
            ("Data Science",        ["data science", "machine learning", "artificial intelligence",
                                    "deep learning", "nlp", "computer vision", "ai ml"]),
            ("Data Engineering",    ["data engineering", "data platform", "data pipeline", "etl",
                                    "data warehouse", "data lake", "analytics engineering"]),
            ("Cybersecurity",       ["cybersecurity", "cyber security", "infosec", "information security",
                                    "soc", "identity and access", "iam", "zero trust"]),
            ("Infrastructure",      ["infrastructure", "cloud", "cloud ops", "networking",
                                    "server management", "datacenter", "aws", "azure", "gcp"]),
            # ── Customer Success sub-departments ────────────────────────────
            ("Customer Onboarding", ["onboarding", "implementation", "professional services",
                                    "customer implementation"]),
            ("Technical Support",   ["technical support", "product support", "tier 2", "tier 3",
                                    "engineering support", "l2 support", "l3 support"]),
            ("Customer Support",    ["customer support", "help desk", "helpdesk", "tier 1",
                                    "service desk", "customer care"]),
            # ── Operations sub-departments ───────────────────────────────────
            ("Supply Chain",        ["supply chain", "procurement", "sourcing", "strategic sourcing",
                                    "vendor management", "category management"]),
            ("Logistics",           ["logistics", "warehousing", "distribution", "fleet management",
                                    "inbound logistics", "outbound logistics"]),
            ("Quality Assurance",   ["quality assurance", "quality control", "qa testing", "qc",
                                    "iso compliance", "six sigma", "ehs"]),
            # ── Product Management sub-departments ──────────────────────────
            ("UX & Design",         ["ux research", "user research", "user experience", "ui design",
                                    "interaction design", "design system", "ux ui"]),
            ("Product Strategy",    ["product roadmap", "product strategy", "product planning",
                                    "product analytics", "product operations"]),
            # ── Legal sub-departments ────────────────────────────────────────
            ("Regulatory Affairs",  ["regulatory affairs", "regulatory compliance", "gdpr", "hipaa",
                                    "finra", "regulatory reporting", "industry regulation"]),
            ("Intellectual Property",["intellectual property", "patents", "trademarks", "ip"]),
            ("Corporate Legal",     ["corporate legal", "general counsel", "litigation",
                                    "contract management", "m&a legal", "legal counsel"]),
            # ── Other ────────────────────────────────────────────────────────
            ("Research & Development", ["research", "r&d", "innovation", "laboratory", "clinical trials"]),
            ("Risk Management",     ["risk management", "enterprise risk", "credit risk",
                                    "market risk", "operational risk"]),
            ("Corporate Strategy",  ["corporate strategy", "strategic planning", "corporate development",
                                    "business strategy", "strategic initiatives"]),
        ]

        primary_lower   = primary.lower()
        secondary_lower = secondary.lower()

        for label, kws in tiers:
            # Skip if label mirrors the primary or secondary (no value added)
            if label.lower() == primary_lower or label.lower() == secondary_lower:
                continue
            for kw in kws:
                if ' ' in kw or any(c in kw for c in ('&', '/')):
                    if kw in des_lower:
                        return label
                else:
                    if re.search(r'(?<![a-z0-9])' + re.escape(kw) + r'(?![a-z0-9])', des_lower):
                        return label

        # No tertiary match — structural engine skips the node
        return ""


# ─────────────────────────────────────────────────────────────────
# REGION CLASSIFIER
# ─────────────────────────────────────────────────────────────────

# Keyword → canonical region label
REGION_MAP: dict[str, str] = {
    # Global / HQ
    "global": "Global HQ", "group": "Global HQ", "worldwide": "Global HQ",
    "international": "Global HQ", "corporate": "Global HQ",
    # EMEA / Europe
    "emea": "EMEA", "europe": "EMEA", "european": "EMEA",
    "eu": "EMEA", "eurozone": "EMEA", "mena": "EMEA",
    # UK specific
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "great britain": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "birmingham": "United Kingdom", "edinburgh": "United Kingdom",
    "leeds": "United Kingdom", "bristol": "United Kingdom",
    "liverpool": "United Kingdom", "glasgow": "United Kingdom",
    "sheffield": "United Kingdom", "cardiff": "United Kingdom",
    "belfast": "United Kingdom", "nottingham": "United Kingdom",
    "reading": "United Kingdom", "oxford": "United Kingdom",
    "cambridge": "United Kingdom", "coventry": "United Kingdom",
    # US
    "us": "North America", "usa": "North America",
    "united states": "North America", "america": "North America",
    "north america": "North America", "canada": "North America",
    "new york": "North America", "san francisco": "North America",
    "chicago": "North America", "boston": "North America",
    "los angeles": "North America", "seattle": "North America",
    "toronto": "North America", "vancouver": "North America",
    # APAC
    "apac": "Asia Pacific", "asia": "Asia Pacific",
    "asia pacific": "Asia Pacific", "anz": "Asia Pacific",
    "australia": "Asia Pacific", "new zealand": "Asia Pacific",
    "singapore": "Asia Pacific", "hong kong": "Asia Pacific",
    "japan": "Asia Pacific", "china": "Asia Pacific",
    "india": "Asia Pacific", "korea": "Asia Pacific",
    "sydney": "Asia Pacific", "melbourne": "Asia Pacific",
    "mumbai": "Asia Pacific", "bangalore": "Asia Pacific",
    "dubai": "Middle East",
    # LATAM
    "latam": "Latin America", "latin america": "Latin America",
    "brazil": "Latin America", "mexico": "Latin America",
    "colombia": "Latin America", "argentina": "Latin America",
    # Africa
    "africa": "Africa", "south africa": "Africa",
    "nigeria": "Africa", "kenya": "Africa", "ghana": "Africa",
    # Middle East
    "middle east": "Middle East", "gcc": "Middle East",
    "uae": "Middle East", "saudi": "Middle East", "saudi arabia": "Middle East",
    "qatar": "Middle East", "kuwait": "Middle East", "oman": "Middle East",
    "bahrain": "Middle East", "abu dhabi": "Middle East",
    "riyadh": "Middle East", "jeddah": "Middle East",
    # South Asia
    "india": "Asia Pacific", "mumbai": "Asia Pacific", "delhi": "Asia Pacific",
    "bangalore": "Asia Pacific", "bengaluru": "Asia Pacific",
    "hyderabad": "Asia Pacific", "chennai": "Asia Pacific", "pune": "Asia Pacific",
    "kolkata": "Asia Pacific", "ahmedabad": "Asia Pacific", "gurgaon": "Asia Pacific",
    "noida": "Asia Pacific", "pakistan": "Asia Pacific", "sri lanka": "Asia Pacific",
    "bangladesh": "Asia Pacific", "nepal": "Asia Pacific",
    # Southeast Asia
    "thailand": "Asia Pacific", "vietnam": "Asia Pacific", "malaysia": "Asia Pacific",
    "indonesia": "Asia Pacific", "philippines": "Asia Pacific", "myanmar": "Asia Pacific",
    "kuala lumpur": "Asia Pacific", "jakarta": "Asia Pacific", "bangkok": "Asia Pacific",
    "manila": "Asia Pacific",
    # Europe
    "germany": "EMEA", "france": "EMEA", "italy": "EMEA",
    "spain": "EMEA", "netherlands": "EMEA", "belgium": "EMEA",
    "sweden": "EMEA", "norway": "EMEA", "denmark": "EMEA",
    "finland": "EMEA", "switzerland": "EMEA", "austria": "EMEA",
    "poland": "EMEA", "portugal": "EMEA", "czechia": "EMEA",
    "czech republic": "EMEA", "hungary": "EMEA", "romania": "EMEA",
    "russia": "EMEA", "ukraine": "EMEA", "turkey": "EMEA",
    "israel": "EMEA", "egypt": "EMEA", "morocco": "EMEA",
    "berlin": "EMEA", "munich": "EMEA", "frankfurt": "EMEA",
    "paris": "EMEA", "amsterdam": "EMEA", "brussels": "EMEA",
    "zurich": "EMEA", "stockholm": "EMEA", "oslo": "EMEA",
    "copenhagen": "EMEA", "helsinki": "EMEA", "madrid": "EMEA",
    "barcelona": "EMEA", "milan": "EMEA", "rome": "EMEA",
    "warsaw": "EMEA", "prague": "EMEA", "budapest": "EMEA",
    "istanbul": "EMEA", "tel aviv": "EMEA", "cairo": "EMEA",
    # LATAM
    "brazil": "Latin America", "mexico": "Latin America",
    "colombia": "Latin America", "argentina": "Latin America",
    "chile": "Latin America", "peru": "Latin America",
    "venezuela": "Latin America", "ecuador": "Latin America",
    "uruguay": "Latin America", "paraguay": "Latin America",
    "sao paulo": "Latin America", "bogota": "Latin America",
    "buenos aires": "Latin America", "lima": "Latin America",
    "santiago": "Latin America", "mexico city": "Latin America",
    # Africa
    "africa": "Africa", "south africa": "Africa", "nigeria": "Africa",
    "kenya": "Africa", "ghana": "Africa", "ethiopia": "Africa",
    "tanzania": "Africa", "uganda": "Africa", "zimbabwe": "Africa",
    "zambia": "Africa", "namibia": "Africa", "botswana": "Africa",
    "johannesburg": "Africa", "cape town": "Africa", "nairobi": "Africa",
    "lagos": "Africa", "accra": "Africa", "abuja": "Africa",
    # North America extended
    "dallas": "North America", "houston": "North America",
    "atlanta": "North America", "miami": "North America",
    "washington": "North America", "washington dc": "North America",
    "denver": "North America", "phoenix": "North America",
    "san jose": "North America", "austin": "North America",
    "minneapolis": "North America", "detroit": "North America",
    "charlotte": "North America", "nashville": "North America",
    "philadelphia": "North America", "san diego": "North America",
    "calgary": "North America", "montreal": "North America", "ottawa": "North America",
    "mexico": "Latin America",
    # APAC extended
    "taiwan": "Asia Pacific", "hong kong": "Asia Pacific",
    "macau": "Asia Pacific", "new caledonia": "Asia Pacific",
    "papua new guinea": "Asia Pacific", "fiji": "Asia Pacific",
    "perth": "Asia Pacific", "brisbane": "Asia Pacific",
    "adelaide": "Asia Pacific", "auckland": "Asia Pacific",
    "wellington": "Asia Pacific", "christchurch": "Asia Pacific",
    "osaka": "Asia Pacific", "tokyo": "Asia Pacific",
    "seoul": "Asia Pacific", "busan": "Asia Pacific",
    "beijing": "Asia Pacific", "shanghai": "Asia Pacific",
    "shenzhen": "Asia Pacific", "guangzhou": "Asia Pacific",
    "chengdu": "Asia Pacific", "wuhan": "Asia Pacific",
}


class RegionClassifier:
    """Maps a raw location string to a canonical region."""

    @staticmethod
    def classify(location: str) -> str:
        if not location:
            return "Global HQ"
        loc = location.lower().strip()
        for kw, region in REGION_MAP.items():
            if kw in loc:
                return region
        return "Global HQ"


# ─────────────────────────────────────────────────────────────────
# SECTOR CLASSIFIER
# ─────────────────────────────────────────────────────────────────

# sector → keywords that appear in company / industry_hint / designation
# Extended with ProTrail company and industry data
SECTOR_KEYWORDS: dict[str, list[str]] = {
    "Automotive": [
        "auto", "automotive", "vehicle", "motor", "car", "truck", "fleet",
        "tyres", "tires", "mobility", "ev", "electric vehicle", "powertrain",
        "bmw", "volkswagen", "ford", "toyota", "stellantis", "jaguar", "land rover",
        "honda", "nissan", "renault", "valeo", "continental", "bosch automotive",
        "skf", "lear", "magna", "delphi", "aptiv", "autoliv", "brembo",
        "fiat", "peugeot", "citroën", "opel", "chrysler", "dodge", "jeep", "ram",
        "volvo cars", "maserati", "abarth", "alfa romeo", "lancia",
        "adas", "autonomous driving", "telematics", "connected car", "oem",
    ],
    "Govt": [
        "government", "ministry", "department of", "dept of", "public sector",
        "civil service", "local council", "borough", "council", "municipality",
        "nhs", "police", "fire service", "hmrc", "defra", "dvla", "cabinet office",
        "parliament", "senate", "congress", "agency", "authority",
        "federal", "state government", "county", "city of", "mayor",
        "department of transportation", "department of education",
        "department of health", "department of defense", "department of justice",
        "department of agriculture", "department of treasury", "department of labor",
        "office of the governor", "legislative", "judicial",
        "public administration", "public service", "crown", "his majesty",
        "her majesty", "rajya", "lok sabha",
    ],
    "NGO": [
        "ngo", "non-profit", "nonprofit", "charity", "foundation", "trust",
        "charitable", "social enterprise", "aid", "relief", "oxfam", "unicef",
        "red cross", "save the children", "voluntary", "not-for-profit",
        "association", "institute", "society", "fund", "endowment",
        "world bank", "united nations", "imf", "who", "ngos",
        "advocacy", "humanitarian", "development organization",
    ],
    "Startup": [
        "startup", "start-up", "start up", "seed stage", "series a", "series b",
        "series c", "venture", "disrupt", "platform", "saas", "fintech", "proptech",
        "legaltech", "healthtech", "edtech", "deeptech", "scaleup", "scale-up",
        "insurtech", "wealthtech", "regtech", "climatetech", "agritech",
    ],
    "Public": [
        "plc", "listed", "stock exchange", "nyse", "nasdaq", "ftse", "tsx",
        "public company", "shareholders", "traded", "ipo", "publicly traded",
        "s&p 500", "fortune 500", "sec filing", "annual report", "10-k",
        "corporation", "inc.", "incorporated",
    ],
    "Private": [
        "private", "ltd", "limited", "llc", "llp", "partnership", "pvt",
        "family office", "pe-backed", "private equity", "family-owned",
        "privately held", "closely held", "proprietorship",
    ],
}


class SectorClassifier:
    """Classifies a record into a sector based on company/designation/hint."""

    @staticmethod
    def classify(company: str, designation: str, industry_hint: str) -> str:
        text = (company + " " + designation + " " + industry_hint).lower()
        for sector, keywords in SECTOR_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    return sector
        return "Private"   # safe default


# ─────────────────────────────────────────────────────────────────
# TOP-LEVEL API FACADE
# ─────────────────────────────────────────────────────────────────

class NLPEngine:
    """
    Single entry point: loads directories once, exposes classify() method.
    """

    def __init__(self):
        self._dirs            = IndustryDirectoryLoader.load_all()
        self._normaliser      = _build_global_normaliser(self._dirs)
        self._industry_matcher = IndustryMatcher(self._dirs)
        self._layer_classifier = LayerClassifier(self._dirs)
        self._dept_extractor  = DepartmentExtractor(self._dirs)
        self._region_clf      = RegionClassifier()
        self._sector_clf      = SectorClassifier()

    def classify(
        self,
        designation: str,
        company: str = "",
        industry_hint: str = "",
        location: str = "",
    ) -> ClassificationResult:
        """
        Full pipeline: sector → industry → layer → department → region.
        Returns ClassificationResult with all fields populated.
        """
        # 1. Detect sector
        sector = self._sector_clf.classify(company, designation, industry_hint)

        # 2. Best matching industry directory
        industry = self._industry_matcher.best(designation, company, industry_hint)

        # 3. Layer classification
        layer, confidence, method = self._layer_classifier.classify(designation, industry)

        # 4. Department extraction
        dept_p, dept_s, dept_t = self._dept_extractor.extract(
            designation, company, industry
        )

        return ClassificationResult(
            layer=layer,
            confidence=confidence,
            matched_industry=industry.id if industry else "generic",
            match_method=method,
            matched_title=self._normaliser.normalise(designation),
            dept_primary=dept_p,
            dept_secondary=dept_s,
            dept_tertiary=dept_t,
        )

    def classify_region(self, location: str) -> str:
        return self._region_clf.classify(location)

    def classify_sector(self, company: str, designation: str, industry_hint: str) -> str:
        return self._sector_clf.classify(company, designation, industry_hint)

    def classify_dept_from_text(self, raw_dept: str, hint_primary: str = "") -> tuple[str, str, str]:
        """Classify a raw freeform department string (e.g. ProTrail 'Department' field)."""
        return self._dept_extractor.extract_from_text(raw_dept, hint_primary)

    @property
    def loaded_industries(self) -> list[str]:
        return [d.name for d in self._dirs]
