"""
Industry Classifier — maps company_name + email_domain to one of 37 canonical industries.

Strategy:
  1. DuckDuckGo web search for "{company_name} industry sector"
     → collect result snippets (no API key required)
  2. Optionally scrape the company's own homepage/about page (via email_domain)
  3. Pass all gathered text to Claude → classify into exactly one of the 37 industries

Falls back to pure LLM knowledge when web search/scraping is unavailable.
Results are cached in-process.

Reference: Global_Org_Hierarchy.xlsx — canonical industry list (37 industries)
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CANONICAL INDUSTRY LIST  (Global_Org_Hierarchy.xlsx)
# ─────────────────────────────────────────────────────────────────────────────

INDUSTRY_LIST: list[str] = [
    "Aerospace & Defence",
    "Agriculture",
    "Automotive",
    "Business Services / Professional Services",
    "Construction",
    "Consumer Products",
    "Consumer Services",
    "Ecommerce",
    "Education",
    "Energy (Oil & Gas)",
    "Financial Markets / Capital Markets / Investments",
    "Healthcare Insurance (Payers)",
    "Healthcare Providers",
    "High Tech / Technology",
    "Hospitality / Travel",
    "Industrial Manufacturing – Discrete",
    "Industrial Manufacturing – Process",
    "IT Hardware",
    "IT Services",
    "Life Insurance",
    "Media & Entertainment",
    "Medical Devices",
    "Mineral / Mining / Natural Resources",
    "Non Profit / NGO",
    "P&C Insurance",
    "Pharmaceuticals / Life Sciences",
    "Public Sector & Government",
    "Real Estate",
    "Reinsurance",
    "Retail",
    "Retail Banking / Commercial Banking",
    "Software",
    "Supply Chain / Logistics",
    "Telecommunications",
    "Transportation",
    "Utilities",
    "Wholesale / Distribution",
]

_INDUSTRY_SET: frozenset[str] = frozenset(INDUSTRY_LIST)

# ─────────────────────────────────────────────────────────────────────────────
# WEB SEARCH — DuckDuckGo HTML (no API key)
# ─────────────────────────────────────────────────────────────────────────────

_SEARCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_SEARCH_TIMEOUT  = 4    # seconds per HTTP request (kept short — runs sync during upload)
_SNIPPET_MAX     = 6_000  # max chars of search snippets to send to LLM


def _ddg_search(query: str) -> str:
    """
    Fetch plain-text snippets from DuckDuckGo HTML search.
    Returns "" on any failure.
    """
    try:
        import httpx
    except ImportError:
        return ""

    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _SEARCH_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=_SEARCH_TIMEOUT,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return ""
        html = resp.text
        # Extract result snippets — DuckDuckGo HTML uses class="result__snippet"
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            html, flags=re.DOTALL | re.IGNORECASE,
        )
        # Strip inner tags and normalise whitespace
        cleaned: list[str] = []
        for s in snippets[:10]:
            s = re.sub(r"<[^>]+>", " ", s)
            s = re.sub(r"\s+", " ", s).strip()
            if s:
                cleaned.append(s)
        return " | ".join(cleaned)[:_SNIPPET_MAX]
    except Exception as exc:
        logger.debug("DuckDuckGo search failed: %s", exc)
        return ""


def _homepage_text(domain: str) -> str:
    """Fetch and strip plain text from the company homepage/about page."""
    if not domain:
        return ""
    try:
        import httpx
    except ImportError:
        return ""

    for base in [f"https://{domain}", f"https://www.{domain}"]:
        for path in ["", "/about", "/about-us", "/company"]:
            url = f"{base}{path}"
            try:
                r = httpx.get(
                    url,
                    headers={"User-Agent": _SEARCH_UA},
                    timeout=_SEARCH_TIMEOUT,
                    follow_redirects=True,
                )
                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and "text/html" in ct:
                    text = re.sub(r"<[^>]+>", " ", r.text)
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > 200:
                        return text[:4_000]
            except Exception:
                continue
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# LLM CLASSIFICATION PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_CLASSIFY = """\
You are a corporate intelligence assistant that classifies companies into industries.

You will be given a company name and optional context (search snippets, website text).
Return EXACTLY one industry from the list below — the most specific match possible.

INDUSTRY LIST:
""" + "\n".join(f"- {i}" for i in INDUSTRY_LIST) + """

Rules:
- Return ONLY the exact industry string from the list above, nothing else.
- Choose the most specific industry that fits (e.g. "Software" beats "High Tech / Technology" \
for a pure-play SaaS company).
- If the company spans multiple industries, pick its primary / largest revenue segment.
- If you are not confident, return "Business Services / Professional Services".
- Do NOT explain, do NOT add punctuation, do NOT add quotes.
"""


def _llm_classify(company_name: str, context: str) -> str:
    """Call Claude to classify the company into one of the 37 industries."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        user_msg = f"Company: {company_name}"
        if context.strip():
            user_msg += f"\n\nContext from web search / company website:\n{context}"
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=_SYSTEM_CLASSIFY,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip().strip('"').strip("'")
        # Validate against list
        if raw in _INDUSTRY_SET:
            logger.info("Industry classified: '%s' → %s", company_name, raw)
            return raw
        # Fuzzy fallback — find the closest match by substring
        raw_l = raw.lower()
        for ind in INDUSTRY_LIST:
            if ind.lower() in raw_l or raw_l in ind.lower():
                logger.info("Industry fuzzy match: '%s' → %s", company_name, ind)
                return ind
        logger.warning("LLM returned unrecognised industry '%s' for '%s'", raw, company_name)
        return ""
    except Exception as exc:
        logger.warning("Industry LLM classification failed for '%s': %s", company_name, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

_INDUSTRY_CACHE: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def classify_industry(company_name: str, email_domain: str = "") -> str:
    """
    Classify *company_name* into one of the 37 canonical industries.

    Strategy (in order):
      1. DuckDuckGo web search for "{company_name} industry sector"
      2. Company homepage/about page (when email_domain is provided)
      3. Claude LLM knowledge fallback

    Returns one of the 37 industry strings, or "" if classification fails.
    Results are cached in-process per company name.
    """
    if not company_name or len(company_name.strip()) < 3:
        return ""

    cache_key = company_name.strip().lower()
    if cache_key in _INDUSTRY_CACHE:
        return _INDUSTRY_CACHE[cache_key]

    # ── Step 1: web search ──────────────────────────────────────────────
    search_query = f"{company_name} industry sector business"
    snippets = _ddg_search(search_query)
    logger.debug("DDG search for '%s': %d chars", company_name, len(snippets))

    # ── Step 2: company homepage (optional) ─────────────────────────────
    homepage = ""
    if email_domain:
        homepage = _homepage_text(email_domain)
        logger.debug("Homepage for %s: %d chars", email_domain, len(homepage))

    # ── Step 3: combine context and classify ────────────────────────────
    context_parts: list[str] = []
    if snippets:
        context_parts.append(f"Web search snippets:\n{snippets}")
    if homepage:
        context_parts.append(f"Company website text:\n{homepage[:2_000]}")
    context = "\n\n".join(context_parts)

    industry = _llm_classify(company_name, context)

    _INDUSTRY_CACHE[cache_key] = industry
    return industry
