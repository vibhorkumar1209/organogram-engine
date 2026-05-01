"""
Rule loader. Reads the archetype JSON files and the region_overlay.csv,
exposes lookup methods used by Agent 3 (NLP) and Agent 4 (Reconciler).
"""
from __future__ import annotations
import csv
import json
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# INDUSTRY -> ARCHETYPE mapping (from master prompt §6.1)
# ---------------------------------------------------------------------------
INDUSTRY_TO_ARCHETYPE = {
    # Industrial Asset-Heavy
    "Automotive": "industrial_asset_heavy",
    "Aerospace & Defence": "industrial_asset_heavy",
    "Industrial Manufacturing — Discrete": "industrial_asset_heavy",
    "Industrial Manufacturing - Discrete": "industrial_asset_heavy",
    "IT Hardware": "industrial_asset_heavy",
    "Medical Devices": "industrial_asset_heavy",
    # Process
    "Energy (Oil & Gas)": "process_industries",
    "Mineral / Mining / Natural Resources": "process_industries",
    "Utilities": "process_industries",
    "Industrial Manufacturing — Process": "process_industries",
    "Industrial Manufacturing - Process": "process_industries",
    "Pharmaceuticals / Life Sciences": "process_industries",
    # Consumer Brand Goods
    "Consumer Products": "consumer_brand_goods",
    "Agriculture": "consumer_brand_goods",
    # Retail & Hospitality
    "Retail": "retail_hospitality",
    "Wholesale / Distribution": "retail_hospitality",
    "Hospitality / Travel": "retail_hospitality",
    "Consumer Services": "retail_hospitality",
    "Ecommerce": "retail_hospitality",
    # Banking
    "Retail Banking / Commercial Banking": "banking",
    # Capital Markets
    "Financial Markets / Capital Markets / Investments": "capital_markets",
    # Insurance
    "P&C Insurance": "insurance",
    "Life Insurance": "insurance",
    "Reinsurance": "insurance",
    "Healthcare Insurance (Payers)": "insurance",
    # Healthcare Delivery
    "Healthcare Providers": "healthcare_delivery",
    # Software & Telecom
    "Software": "software_telecom",
    "High Tech / Technology": "software_telecom",
    "Telecommunications": "software_telecom",
    "Media & Entertainment": "software_telecom",
    # Professional & IT Services
    "IT Services": "professional_it_services",
    "Business Services / Professional Services": "professional_it_services",
    # Public Sector & NGO
    "Public Sector & Government": "public_sector_ngo",
    "Non Profit / NGO": "public_sector_ngo",
    "Non-Profit / NGO": "public_sector_ngo",
    "Education": "public_sector_ngo",
    # Asset-Light
    "Real Estate": "asset_light_operations",
    "Supply Chain / Logistics": "asset_light_operations",
    "Transportation": "asset_light_operations",
    "Construction": "asset_light_operations",  # closest match
}


class RuleLibrary:
    """Loads archetype rule files + region overlay; exposes lookups."""

    def __init__(self, rules_dir: str | Path):
        self.rules_dir = Path(rules_dir)
        self.archetypes: dict[str, dict] = {}
        self.overlay_rows: list[dict] = []
        self._load()

    def _load(self):
        # Archetypes
        arch_dir = self.rules_dir / "archetypes"
        for f in arch_dir.glob("*_v*.json"):
            data = json.loads(f.read_text(encoding="utf-8"))
            self.archetypes[data["archetype_id"]] = data
        # Region overlay
        overlay_path = self.rules_dir / "region_overlay.csv"
        with overlay_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            self.overlay_rows = [
                r for r in reader
                if r.get("title_raw", "").strip()
                and not r["title_raw"].strip().startswith("#")
            ]

    # -----------------------------------------------------------------
    # Archetype resolution
    # -----------------------------------------------------------------
    def archetype_for_industry(self, industry: str) -> Optional[dict]:
        archetype_id = INDUSTRY_TO_ARCHETYPE.get(industry)
        if archetype_id is None:
            return None
        return self.archetypes.get(archetype_id)

    # -----------------------------------------------------------------
    # Title -> (level, function, normalized_title)
    # Lookup precedence: most specific to most general (master prompt §)
    # -----------------------------------------------------------------
    def lookup_title(
        self,
        title_raw: str,
        region: str,
        archetype_id: str,
        sub_industry: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Returns the first-matching overlay row, or None.
        Match order:
          1. (title, region, archetype, sub_industry)
          2. (title, region, archetype)
          3. (title, region, "any")
          4. (title, "Global", archetype)
          5. (title, "Global", "any")
        """
        title_l = title_raw.strip().lower()

        # Precompute filtered candidates by lowercased title (cheap)
        candidates = [r for r in self.overlay_rows
                      if r["title_raw"].strip().lower() == title_l
                      or (r.get("title_native") or "").strip().lower() == title_l]
        if not candidates:
            return None

        def match(rows, region_filter, arch_filter, subind_filter=None):
            for r in rows:
                if r["region"] != region_filter:
                    continue
                if r["archetype_id"] != arch_filter:
                    continue
                if subind_filter is not None and r.get("sub_industry", "") != subind_filter:
                    continue
                return r
            return None

        # 1. Most specific
        if sub_industry:
            hit = match(candidates, region, archetype_id, sub_industry)
            if hit: return hit
        # 2. Region + archetype
        hit = match(candidates, region, archetype_id)
        if hit: return hit
        # 3. Region + "any"
        hit = match(candidates, region, "any")
        if hit: return hit
        # 4. Global + archetype
        hit = match(candidates, "Global", archetype_id)
        if hit: return hit
        # 5. Global + "any"
        hit = match(candidates, "Global", "any")
        if hit: return hit
        return None

    # -----------------------------------------------------------------
    # Default cascade lookup — used when overlay misses.
    #
    # Match precedence:
    #   1. Exact match (title == ladder entry, case-insensitive)
    #   2. Token-set match (same set of meaningful words, e.g.
    #      "Finance Director" matches "Director Finance")
    #   3. Title is a substring of a ladder entry
    #   4. Ladder entry is a substring of the title (entry must be >= 8 chars)
    # -----------------------------------------------------------------
    _STOP = {"the", "a", "an", "of", "and", "&", "-", "—", "/"}

    def _tokens(self, s: str) -> set[str]:
        s = s.lower().replace("/", " ").replace("—", " ").replace("-", " ")
        return {t for t in s.split() if t and t not in self._STOP}

    # Seniority keywords that, if present in the title, mean the cascade
    # should prefer ladder entries that ALSO contain that keyword.
    # Prevents "Director of Software Engineering" matching "Engineer".
    # Listed longest-first so "senior director" wins over "director".
    _SENIORITY_KEYWORDS = [
        "chief executive officer", "chief financial officer",
        "chief operating officer", "chief technology officer",
        "chief marketing officer", "chief information officer",
        "chief human resources officer",
        "executive vice president", "senior vice president",
        "vice president",
        "senior director", "managing director", "director",
        "senior manager", "manager",
        "head of",
        "chief", "president", "evp", "svp", "vp",
        "ceo", "cfo", "coo", "cto", "cio", "cmo", "chro",
    ]

    @staticmethod
    def _is_word_match(needle: str, haystack: str) -> bool:
        """True if `needle` appears in `haystack` on word boundaries."""
        if needle not in haystack:
            return False
        # Quick check: tokens separated by spaces, dashes, slashes, parens
        sep_chars = " \t-/().,"
        idx = 0
        while True:
            pos = haystack.find(needle, idx)
            if pos == -1:
                return False
            before = haystack[pos - 1] if pos > 0 else " "
            after_idx = pos + len(needle)
            after = haystack[after_idx] if after_idx < len(haystack) else " "
            if before in sep_chars and after in sep_chars:
                return True
            idx = pos + 1

    def _seniority_in_title(self, title_l: str) -> Optional[str]:
        for kw in self._SENIORITY_KEYWORDS:
            if self._is_word_match(kw, title_l):
                return kw
        return None

    def cascade_lookup(
        self, title_raw: str, archetype_id: str
    ) -> Optional[dict]:
        archetype = self.archetypes.get(archetype_id)
        if not archetype:
            return None
        title_l = title_raw.strip().lower()
        if not title_l:
            return None
        title_tokens = self._tokens(title_raw)
        seniority = self._seniority_in_title(title_l)

        exact_hit = None
        token_hit = None
        substring_hit = None
        loose_hit = None

        for func, ladder in archetype["function_level_cascade"].items():
            for entry in ladder:
                norm_l = entry["title"].lower()
                norm_parts = [p.strip() for p in norm_l.split("/")]

                # If the title contains a seniority keyword, the ladder entry
                # must also contain it (on word boundaries) — otherwise we skip.
                if seniority and not self._is_word_match(seniority, norm_l):
                    continue

                # Tier 1: exact
                if title_l == norm_l or title_l in norm_parts:
                    if exact_hit is None:
                        exact_hit = {"function": func, "level": entry["level"],
                                     "normalized_title": entry["title"]}

                # Tier 2: token-set equality across either the full entry
                # or any '/'-separated alias
                entry_tokens = self._tokens(entry["title"])
                alias_token_sets = [self._tokens(p) for p in norm_parts]
                if title_tokens == entry_tokens or title_tokens in alias_token_sets:
                    if token_hit is None or len(entry["title"]) < len(token_hit["normalized_title"]):
                        token_hit = {"function": func, "level": entry["level"],
                                     "normalized_title": entry["title"]}

                # Tier 2.5: title tokens are a SUPERSET of ladder entry tokens.
                # Catches "Director of Software Engineering" (tokens=
                # {director, software, engineering}) matching ladder
                # "Engineering Director" (tokens={engineering, director}).
                # Skip trivial matches: ladder must have >= 2 tokens.
                if (token_hit is None
                        and len(entry_tokens) >= 2
                        and entry_tokens.issubset(title_tokens)):
                    token_hit = {"function": func, "level": entry["level"],
                                 "normalized_title": entry["title"]}

                # Tier 3: title substring
                if title_l in norm_l and len(title_l) >= 6:
                    if substring_hit is None or len(norm_l) < len(substring_hit["normalized_title"]):
                        substring_hit = {"function": func, "level": entry["level"],
                                         "normalized_title": entry["title"]}

                # Tier 4: ladder substring (entry >= 10 chars to suppress noise
                # like "Engineer" matching "Director of Software Engineering").
                if norm_l in title_l and len(norm_l) >= 10:
                    if loose_hit is None or len(norm_l) > len(loose_hit["normalized_title"]):
                        loose_hit = {"function": func, "level": entry["level"],
                                     "normalized_title": entry["title"]}

        return exact_hit or token_hit or substring_hit or loose_hit

    # -----------------------------------------------------------------
    # Atypical role check
    # -----------------------------------------------------------------
    def is_atypical(self, title: str, archetype_id: str) -> bool:
        archetype = self.archetypes.get(archetype_id)
        if not archetype:
            return False
        title_l = title.strip().lower()
        return any(r.lower() in title_l or title_l in r.lower()
                   for r in archetype.get("atypical_roles", []))
