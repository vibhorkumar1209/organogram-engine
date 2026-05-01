"""
SEC EDGAR client.

Fetches DEF 14A (proxy statement) and 10-K Item 10 for US-listed firms.
Returns cleaned text sections relevant to director and officer identification.

EDGAR full-text search API is public and free.
No API key required.

For non-US firms, logs "filing not available" and returns None — v1 scope.
"""
from __future__ import annotations
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup


EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions"
EDGAR_FILING_BASE = "https://www.sec.gov/Archives/edgar/data"
EDGAR_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

USER_AGENT = (
    "OrganogramEngine/1.0 (RefractOne; contact@refractone.com)"
)

# Per SEC EDGAR rate limit: no more than 10 requests per second
EDGAR_RATE_LIMIT = 0.15   # 150ms between requests

# Number of characters to extract around leadership mentions in 10-K
CONTEXT_WINDOW = 4000

# Sections of the proxy that typically contain director/officer info
PROXY_SECTION_MARKERS = [
    "INFORMATION ABOUT THE BOARD",
    "DIRECTOR NOMINEES",
    "DIRECTORS AND EXECUTIVE OFFICERS",
    "EXECUTIVE COMPENSATION",
    "NAMED EXECUTIVE OFFICERS",
    "BOARD OF DIRECTORS",
    "CORPORATE GOVERNANCE",
]


class EdgarResult:
    """Outcome of an EDGAR fetch."""

    def __init__(self, firm: str, ticker: Optional[str],
                 cik: Optional[str], form_type: str,
                 filing_url: Optional[str], text: str,
                 error: Optional[str] = None):
        self.firm = firm
        self.ticker = ticker
        self.cik = cik
        self.form_type = form_type
        self.filing_url = filing_url
        self.text = text
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)


class SecEdgarClient:
    """Client for SEC EDGAR full-text search and filing retrieval."""

    def __init__(self):
        self._last_request = 0.0

    # ------------------------------------------------------------------
    # PUBLIC
    # ------------------------------------------------------------------
    def get_proxy_leaders(
        self, firm_name: str, ticker: Optional[str] = None
    ) -> EdgarResult:
        """
        Fetch the latest DEF 14A (proxy statement) for the firm.
        Returns cleaned text of the director/officer sections.
        """
        cik, actual_ticker = self._resolve_cik(firm_name, ticker)
        if not cik:
            return EdgarResult(firm_name, ticker, None, "DEF 14A", None,
                               "", f"CIK not found for '{firm_name}'.")

        filing_url, doc_url = self._latest_filing_doc(cik, "DEF 14A")
        if not doc_url:
            return EdgarResult(firm_name, ticker, cik, "DEF 14A", filing_url,
                               "", "No DEF 14A found in EDGAR.")

        text = self._extract_proxy_sections(doc_url)
        return EdgarResult(firm_name, actual_ticker, cik, "DEF 14A",
                           filing_url, text)

    def get_10k_officers(
        self, firm_name: str, ticker: Optional[str] = None
    ) -> EdgarResult:
        """
        Fetch Item 10 of the latest 10-K (Directors and Executive Officers).
        Returns cleaned text.
        """
        cik, actual_ticker = self._resolve_cik(firm_name, ticker)
        if not cik:
            return EdgarResult(firm_name, ticker, None, "10-K", None,
                               "", f"CIK not found for '{firm_name}'.")

        filing_url, doc_url = self._latest_filing_doc(cik, "10-K")
        if not doc_url:
            return EdgarResult(firm_name, ticker, cik, "10-K", filing_url,
                               "", "No 10-K found in EDGAR.")

        text = self._extract_10k_item10(doc_url)
        return EdgarResult(firm_name, actual_ticker, cik, "10-K",
                           filing_url, text)

    # ------------------------------------------------------------------
    # CIK RESOLUTION
    # ------------------------------------------------------------------
    def _resolve_cik(
        self, firm_name: str, ticker: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve company name or ticker to a 10-digit EDGAR CIK."""
        # Try ticker first (most reliable)
        if ticker:
            resp = self._get(
                f"https://efts.sec.gov/LATEST/search-index?q={quote(ticker)}"
                f"&dateRange=custom&startdt=2020-01-01&forms=10-K"
            )
            if resp:
                hits = resp.get("hits", {}).get("hits", [])
                if hits:
                    entity = hits[0].get("_source", {})
                    cik = str(entity.get("entity_id", "")).zfill(10)
                    return cik, ticker

        # Fall back to name search
        resp = self._get(
            "https://efts.sec.gov/LATEST/search-index?"
            f"q={quote(firm_name)}&dateRange=custom&startdt=2020-01-01&forms=DEF+14A"
        )
        if resp:
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                entity = hits[0].get("_source", {})
                cik = str(entity.get("entity_id", "")).zfill(10)
                actual_ticker = entity.get("period_of_report", ticker)
                return cik, actual_ticker

        return None, None

    # ------------------------------------------------------------------
    # FILING RETRIEVAL
    # ------------------------------------------------------------------
    def _latest_filing_doc(
        self, cik: str, form_type: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Return (filing_index_url, primary_document_url) for the latest
        filing of the given form type.
        """
        resp = self._get(
            f"{EDGAR_BROWSE_URL}?action=getcompany&CIK={cik}"
            f"&type={quote(form_type)}&dateb=&owner=include&count=5"
            f"&search_text=",
            is_html=True,
        )
        if resp is None:
            return None, None

        # Parse filing table
        soup = BeautifulSoup(resp, "html.parser")
        table = soup.find("table", class_="tableFile2")
        if not table:
            return None, None

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_type = cells[0].get_text(strip=True)
            if row_type == form_type:
                link = cells[1].find("a", href=True)
                if link:
                    idx_url = "https://www.sec.gov" + link["href"]
                    doc_url = self._filing_primary_doc(idx_url)
                    return idx_url, doc_url

        return None, None

    def _filing_primary_doc(self, idx_url: str) -> Optional[str]:
        """From a filing index page, get the URL of the primary document."""
        resp = self._get(idx_url, is_html=True)
        if resp is None:
            return None
        soup = BeautifulSoup(resp, "html.parser")
        table = soup.find("table", class_="tableFile")
        if not table:
            return None
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            doc_type = cells[3].get_text(strip=True)
            if doc_type in {"DEF 14A", "10-K", "10-K/A"}:
                link = cells[2].find("a", href=True)
                if link:
                    return "https://www.sec.gov" + link["href"]
        return None

    # ------------------------------------------------------------------
    # TEXT EXTRACTION
    # ------------------------------------------------------------------
    def _extract_proxy_sections(self, doc_url: str) -> str:
        """
        From a DEF 14A document, extract sections about directors/officers.
        Returns up to CONTEXT_WINDOW × len(markers) characters.
        """
        html = self._get(doc_url, is_html=True)
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        chunks = []
        text_upper = text.upper()
        for marker in PROXY_SECTION_MARKERS:
            pos = text_upper.find(marker)
            if pos != -1:
                chunk = text[max(0, pos - 200): pos + CONTEXT_WINDOW]
                chunks.append(chunk)

        if not chunks:
            # Fall back to the first CONTEXT_WINDOW × 2 chars of the document
            chunks = [text[:CONTEXT_WINDOW * 2]]

        combined = "\n\n---\n\n".join(chunks)
        return combined[:CONTEXT_WINDOW * 4]   # hard cap

    def _extract_10k_item10(self, doc_url: str) -> str:
        """Extract Item 10 (Directors and Executive Officers) from a 10-K."""
        html = self._get(doc_url, is_html=True)
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)

        text_upper = text.upper()
        start = text_upper.find("ITEM 10")
        if start == -1:
            start = text_upper.find("DIRECTORS AND EXECUTIVE OFFICERS")
        if start == -1:
            return text[:CONTEXT_WINDOW * 2]

        end = text_upper.find("ITEM 11", start)
        if end == -1:
            end = start + CONTEXT_WINDOW * 3

        return text[max(0, start - 100): end][:CONTEXT_WINDOW * 3]

    # ------------------------------------------------------------------
    # HTTP HELPER
    # ------------------------------------------------------------------
    def _get(self, url: str, is_html: bool = False):
        """Rate-limited GET. Returns parsed JSON dict or raw HTML string."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < EDGAR_RATE_LIMIT:
            time.sleep(EDGAR_RATE_LIMIT - elapsed)
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=20.0,
                follow_redirects=True,
            )
            self._last_request = time.monotonic()
            if resp.status_code == 200:
                return resp.text if is_html else resp.json()
            return None
        except Exception:
            return None
