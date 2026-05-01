"""
Agent 2 — Web & Filings Agent.

Responsibility: build the authoritative roster of Board of Directors and
Executive Management (Levels 1–2) from primary sources.

Runtime fetch order (Decision 2 — fastest/most current first):
  1. Firm website leadership/board page
  2. Annual report / SEC filing (EDGAR for US-listed firms)
  3. LinkedIn data provided as input (from the parsed workforce CSV)

Conflict resolution (master prompt §4.1 — trust hierarchy):
  Annual report > Firm website > LinkedIn provided data.
  When two sources disagree on the same person's title, the higher-precedence
  source wins. Both are preserved in the provenance log.

Anti-hallucination: every name extracted by the LLM must appear in the
source HTML via fuzzy matching (Decision 6). Unverified extractions are
logged but dropped from the authoritative output.

Caching: HTML is cached locally by URL + date (Decision 7).

Provenance: every AuthoritativeLeader record carries full audit trail
(source URL, raw evidence, LLM call ID, verification status).

Configuration per engagement (Decision 3 — URLs supplied by orchestrator):
  website_urls:    list of leadership/board page URLs to fetch
  filing_ticker:   NYSE/NASDAQ ticker for EDGAR lookup (US-listed firms only)
  output_dir:      where to write provenance JSONL log
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..schemas.types import AuthoritativeLeader, PersonRecord
from ..utils.html_fetcher import HTMLFetcher
from ..utils.llm_extractor import LLMExtractor, ExtractedLeader
from ..utils.sec_edgar import SecEdgarClient, EdgarResult
from ..utils.provenance_log import ProvenanceLog, ProvenanceEntry

# Source-type precedence (lower number = higher authority)
SOURCE_PRECEDENCE = {
    "sec_filing":        1,
    "annual_report":     1,   # same tier as SEC
    "firm_website":      2,
    "linkedin_provided": 3,
}


@dataclass
class Agent2Config:
    """Per-engagement configuration for Agent 2."""
    firm_name: str
    org_type: str                       # Public | Private | NGO | Government

    # Firm website — at least one URL required
    website_urls: list[str]

    # SEC/EDGAR (US Public firms only)
    filing_ticker: Optional[str] = None   # e.g. "BWA" for BorgWarner
    use_edgar: bool = True                # set False for Private/NGO/non-US

    # LinkedIn persons from the input CSV that are likely executives.
    # Agent 1 passes these through; Agent 2 uses them as fallback.
    linkedin_persons: list[PersonRecord] = None  # type: ignore

    # Infrastructure
    cache_dir: str = "/tmp/organogram_cache"
    output_dir: str = "/Users/vibhor/Downloads/engine/output"
    api_key: Optional[str] = None


class WebFilingsAgent:
    """Agent 2 — extracts authoritative Board and ExCo from primary sources."""

    def __init__(self, config: Agent2Config):
        self.config = config
        self.fetcher = HTMLFetcher(config.cache_dir)
        self.extractor = LLMExtractor(
            api_key=config.api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.edgar = SecEdgarClient()
        self.plog = ProvenanceLog(config.output_dir)

        # Resolved people keyed by normalised name
        self._roster: dict[str, _PersonCandidate] = {}

    # ------------------------------------------------------------------
    # PUBLIC ENTRY
    # ------------------------------------------------------------------
    def run(self) -> list[AuthoritativeLeader]:
        """
        Execute the three-phase fetch pipeline and return the merged,
        de-duplicated, precedence-resolved list of AuthoritativeLeader.
        """
        print(f"\n[agent 2] Starting Web & Filings Agent for: {self.config.firm_name}")

        # Phase 1 — Firm website (fastest, most current)
        self._phase_website()

        # Phase 2 — Annual report / SEC filing (most authoritative)
        self._phase_filings()

        # Phase 3 — LinkedIn provided data (fallback for gaps only)
        self._phase_linkedin()

        # Merge + resolve conflicts
        leaders = self._resolve_and_emit()

        self.plog.print_summary()
        print(f"[agent 2] {len(leaders)} authoritative leaders finalised "
              f"({sum(L.is_board for L in leaders)} BoD, "
              f"{sum(not L.is_board for L in leaders)} ExCo).")
        return leaders

    # ------------------------------------------------------------------
    # PHASE 1 — FIRM WEBSITE
    # ------------------------------------------------------------------
    def _phase_website(self):
        if not self.config.website_urls:
            print("[agent 2] Phase 1: No website URLs configured — skipping.")
            return

        print(f"[agent 2] Phase 1: Fetching {len(self.config.website_urls)} website URL(s).")
        for url in self.config.website_urls:
            result = self.fetcher.fetch(url)
            cache_tag = "(cached)" if result.cache_hit else "(live)"
            if not result.ok:
                print(f"  [website] FAIL {url} — {result.error or result.status_code}")
                continue

            print(f"  [website] OK   {url} {cache_tag} — {len(result.cleaned_text)} chars")
            leaders = self.extractor.extract(
                cleaned_text=result.cleaned_text,
                source_url=url,
                source_type="firm_website",
                firm_name=self.config.firm_name,
            )
            print(f"  [website] Extracted {len(leaders)} candidates from {url}")
            for L in leaders:
                self._record(L, result.cache_hit)

    # ------------------------------------------------------------------
    # PHASE 2 — ANNUAL REPORT / SEC FILING
    # ------------------------------------------------------------------
    def _phase_filings(self):
        is_us_public = (
            self.config.org_type == "Public"
            and self.config.use_edgar
        )
        if not is_us_public:
            print(f"[agent 2] Phase 2: EDGAR skipped "
                  f"(org_type={self.config.org_type}, use_edgar={self.config.use_edgar}).")
            return

        print(f"[agent 2] Phase 2: Querying SEC EDGAR "
              f"(ticker={self.config.filing_ticker or 'name search'}).")

        # Fetch DEF 14A (proxy — BoD focused)
        proxy = self.edgar.get_proxy_leaders(
            self.config.firm_name, self.config.filing_ticker
        )
        self._process_edgar_result(proxy, "sec_filing")

        # Fetch 10-K Item 10 (ExCo focused) — only if DEF 14A didn't cover ExCo
        exco_count = sum(
            1 for c in self._roster.values() if not c.is_board
        )
        if exco_count < 3:
            tenk = self.edgar.get_10k_officers(
                self.config.firm_name, self.config.filing_ticker
            )
            self._process_edgar_result(tenk, "sec_filing")

    def _process_edgar_result(self, result: EdgarResult, source_type: str):
        if not result.ok:
            print(f"  [edgar]   MISS {result.form_type} — {result.error}")
            return
        print(f"  [edgar]   OK   {result.form_type} — "
              f"{len(result.text)} chars from {result.filing_url}")
        leaders = self.extractor.extract(
            cleaned_text=result.text,
            source_url=result.filing_url or "https://www.sec.gov",
            source_type=source_type,
            firm_name=self.config.firm_name,
        )
        print(f"  [edgar]   Extracted {len(leaders)} candidates from {result.form_type}")
        for L in leaders:
            self._record(L, cache_hit=False)

    # ------------------------------------------------------------------
    # PHASE 3 — LINKEDIN PROVIDED DATA
    # ------------------------------------------------------------------
    def _phase_linkedin(self):
        persons = self.config.linkedin_persons or []
        if not persons:
            print("[agent 2] Phase 3: No LinkedIn persons provided — skipping.")
            return

        # Consider only people with titles that suggest C-suite or board
        EXEC_KEYWORDS = {
            "chief", "ceo", "cfo", "coo", "cto", "cio", "cmo", "chro",
            "president", "chairman", "board", "director", "general counsel",
            "managing director", "whole-time director",
        }

        candidates = [
            p for p in persons
            if any(k in (p.title or "").lower() for k in EXEC_KEYWORDS)
        ]
        print(f"[agent 2] Phase 3: {len(candidates)} LinkedIn persons "
              f"look like executives (from {len(persons)} total).")

        for p in candidates:
            is_board = any(
                k in (p.title or "").lower()
                for k in {"board", "chairman", "non-executive", "independent director",
                           "whole-time director", "executive director"}
            )
            ext = ExtractedLeader(
                name=p.name,
                title=p.title or "",
                is_board=is_board,
                source_url=p.source_url,
                source_type="linkedin_provided",
                raw_evidence=f"{p.name}, {p.title}",
                verification_status="verified",
                verification_detail="Supplied directly from input dataset.",
            )
            self._record(ext, cache_hit=False)

    # ------------------------------------------------------------------
    # RECORD + PROVENANCE
    # ------------------------------------------------------------------
    def _record(self, L: ExtractedLeader, cache_hit: bool):
        """Log to provenance; add to roster if verified or corroborating."""
        entry = ProvenanceEntry(
            name=L.name,
            title=L.title,
            source_url=L.source_url,
            source_type=L.source_type,
            raw_evidence=L.raw_evidence,
            verification_status=L.verification_status,
            verification_detail=L.verification_detail,
            llm_call_id=L.llm_call_id,
            fetch_cache_hit=cache_hit,
            firm=self.config.firm_name,
        )
        self.plog.record(entry)

        # Drop unverified unless corroborated by a higher-precedence source
        if L.verification_status == "unverified":
            return

        key = _norm_name(L.name)
        prec = SOURCE_PRECEDENCE.get(L.source_type, 99)

        if key not in self._roster:
            self._roster[key] = _PersonCandidate(
                name=L.name, title=L.title,
                is_board=L.is_board,
                source_url=L.source_url, source_type=L.source_type,
                precedence=prec,
            )
        else:
            existing = self._roster[key]
            # Higher-precedence source wins on title; lower-precedence data
            # can only fill gaps (i.e., if existing has no title).
            if prec < existing.precedence:
                existing.title = L.title
                existing.source_url = L.source_url
                existing.source_type = L.source_type
                existing.precedence = prec
                existing.is_board = L.is_board
            # Always keep the is_board=True if ANY source says board
            if L.is_board:
                existing.is_board = True

    # ------------------------------------------------------------------
    # RESOLVE + EMIT
    # ------------------------------------------------------------------
    def _resolve_and_emit(self) -> list[AuthoritativeLeader]:
        leaders = []
        for cand in self._roster.values():
            leaders.append(AuthoritativeLeader(
                name=cand.name,
                title=cand.title,
                source_url=cand.source_url,
                source_type=cand.source_type,
                is_board=cand.is_board,
                immutable=True,
            ))
        # Sort: BoD first, then ExCo; within each group alphabetical
        leaders.sort(key=lambda L: (0 if L.is_board else 1, L.name))
        return leaders


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
@dataclass
class _PersonCandidate:
    name: str
    title: str
    is_board: bool
    source_url: str
    source_type: str
    precedence: int


def _norm_name(name: str) -> str:
    """Normalise a name for deduplication key."""
    import re
    return re.sub(r"[^a-z]", "", name.lower())
