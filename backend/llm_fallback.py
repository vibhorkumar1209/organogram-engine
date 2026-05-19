"""
LLM company leadership enrichment — Board of Directors and Executive Management only.

Public function:

  llm_fetch_leadership(company_name, domain="")
    Scrapes the company's own website (leadership/about/team pages) and passes
    the extracted text to Claude for structured extraction.

    Two extraction strategies per page:
      1. JSON-LD structured data (<script type="application/ld+json">) —
         works even for JS-rendered SPAs that embed Person schema server-side.
      2. Plain-text strip of the rendered HTML.

    Returns {"board": [...], "executives": [...]}  each item = {name, title}.
    Returns {"board": [], "executives": []} if no domain is provided or no
    useful content is found — no LLM knowledge fallback.

The LLM is NOT used for title classification or seniority inference.
All NLP classification is fully deterministic (overlay → YAML → pattern → fallback).
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HTML UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json_ld(html: str) -> str:
    """
    Pull text from JSON-LD <script type="application/ld+json"> blocks.
    Many modern sites embed structured Person/Organization data here even
    when the visible page is JS-rendered.
    Returns a flat string of name/title pairs found, or "".
    """
    snippets: list[str] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE
    ):
        try:
            obj = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        # Flatten to list of dicts
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            # Person schemas
            if item.get("@type") in ("Person", "Employee"):
                name  = item.get("name", "")
                title = item.get("jobTitle", "")
                if name and title:
                    snippets.append(f"{name} — {title}")
            # OrganizationRole arrays
            members = item.get("member", item.get("employee", []))
            if isinstance(members, list):
                for m in members:
                    if isinstance(m, dict):
                        name  = m.get("name", "")
                        title = m.get("jobTitle", "")
                        if name and title:
                            snippets.append(f"{name} — {title}")
    return "\n".join(snippets)


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace. No external deps."""
    # Remove <script> and <style> blocks entirely (but keep ld+json — handled separately)
    html = re.sub(
        r"<script(?![^>]+application/ld\+json)[^>]*>.*?</script>",
        " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# Ordered by hit-rate: most company sites use one of the first few.
_LEADERSHIP_PATHS = [
    "/leadership",
    "/about/leadership",
    "/about-us/leadership",          # Morgan Stanley, many US banks
    "/about-us/our-leadership",
    "/management",
    "/about/management",
    "/executive-team",
    "/our-team",
    "/board-of-directors",
    "/about/board-of-directors",
    "/about-us/board-of-directors",
    "/governance/board-of-directors",
    "/investors/governance/board-of-directors",
    "/team",
    "/about",
    "/about-us",
    "/company/leadership",
    "/company/management",
    "/en/about/leadership",          # some multinational sites
    "/en/about-us/leadership",
]

_WEB_TIMEOUT = 8          # seconds per HTTP request
_MAX_PAGE_CHARS = 20_000  # chars to pass to Claude per page
_MAX_PAGES = 4            # allow up to 4 pages (BOD + exec committee may be separate)
_TOTAL_BUDGET = 40        # hard cap on total scraping time (seconds)

# Link patterns that indicate a sub-page with leadership content
_LEADERSHIP_LINK_RE = re.compile(
    r'href=["\']([^"\']*(?:'
    r'board[-_]of[-_]directors|board-of-directors'
    r'|operating[-_]committee|leadership[-_]team'
    r'|executive[-_]committee|management[-_]committee'
    r'|senior[-_]leadership|our[-_]leaders'
    r'|supervisory[-_]board|advisory[-_]board'
    r')[^"\']*)["\']',
    re.IGNORECASE,
)


def _fetch_leadership_text(domain: str) -> str:
    """
    Try common leadership/about URLs on *domain* and return concatenated
    plain-text content from successful responses (max _MAX_PAGES pages,
    hard-capped at _TOTAL_BUDGET seconds total).
    Returns "" if nothing useful is found or httpx is unavailable.
    """
    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available — skipping website scrape")
        return ""

    if not domain:
        return ""

    import time
    deadline = time.monotonic() + _TOTAL_BUDGET

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; OrgEngine/2.0; "
            "+https://github.com/vibhorkumar1209/organogram-engine)"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    base_candidates = [
        f"https://{domain}",
        f"https://www.{domain}",
    ]

    collected: list[str] = []
    tried: set[str] = set()

    for base in base_candidates:
        for path in _LEADERSHIP_PATHS:
            if time.monotonic() >= deadline:
                logger.debug(f"Website scrape budget exhausted for {domain}")
                break
            url = f"{base}{path}"
            if url in tried:
                continue
            tried.add(url)
            remaining = max(1, deadline - time.monotonic())
            timeout = min(_WEB_TIMEOUT, remaining)
            try:
                r = httpx.get(url, headers=headers,
                              timeout=timeout, follow_redirects=True)
                ct = r.headers.get("content-type", "")
                if r.status_code != 200 or "text/html" not in ct:
                    continue

                raw_html = r.text

                # Try structured JSON-LD first (works even for JS-heavy SPAs
                # that embed Person schema server-side)
                ld_text = _extract_json_ld(raw_html)

                # Plain-text strip
                text = _strip_html(raw_html)

                # Merge: prefer JSON-LD when visible text is sparse (SPA)
                if ld_text:
                    combined = f"{ld_text}\n\n{text[:_MAX_PAGE_CHARS]}"
                else:
                    combined = text[:_MAX_PAGE_CHARS]

                # A page is "useful" if either the visible text is substantial
                # OR we found JSON-LD person data
                if len(text) < 200 and not ld_text:
                    logger.debug(f"Page too thin ({len(text)} chars), skipping: {url}")
                    continue

                collected.append(f"[Page: {url}]\n{combined}")
                logger.debug(
                    f"Scraped leadership page: {url} "
                    f"({len(text)} chars text, {len(ld_text)} chars JSON-LD)"
                )

                # Discover sub-pages linked from this leadership page
                # (e.g. /board-of-directors, /operating-committee on same domain)
                if len(collected) < _MAX_PAGES:
                    for m in _LEADERSHIP_LINK_RE.finditer(raw_html):
                        href = m.group(1)
                        # Resolve relative links against the base
                        if href.startswith("http"):
                            linked_url = href
                        elif href.startswith("/"):
                            linked_url = f"{base}{href}"
                        else:
                            continue
                        if linked_url not in tried:
                            if time.monotonic() < deadline and len(collected) < _MAX_PAGES:
                                tried.add(linked_url)
                                remaining2 = max(1, deadline - time.monotonic())
                                try:
                                    r2 = httpx.get(
                                        linked_url, headers=headers,
                                        timeout=min(_WEB_TIMEOUT, remaining2),
                                        follow_redirects=True,
                                    )
                                    ct2 = r2.headers.get("content-type", "")
                                    if r2.status_code == 200 and "text/html" in ct2:
                                        ld2 = _extract_json_ld(r2.text)
                                        t2  = _strip_html(r2.text)
                                        if len(t2) >= 200 or ld2:
                                            c2 = f"{ld2}\n\n{t2[:_MAX_PAGE_CHARS]}" if ld2 else t2[:_MAX_PAGE_CHARS]
                                            collected.append(f"[Page: {linked_url}]\n{c2}")
                                            logger.debug(
                                                f"Scraped linked leadership page: {linked_url} "
                                                f"({len(t2)} chars text)"
                                            )
                                except Exception as exc2:
                                    logger.debug(f"Linked page scrape failed {linked_url}: {exc2}")

                if len(collected) >= _MAX_PAGES:
                    break
            except Exception as exc:
                logger.debug(f"Website scrape failed for {url}: {exc}")
                continue
        if len(collected) >= _MAX_PAGES or time.monotonic() >= deadline:
            break

    return "\n\n---\n\n".join(collected)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_FROM_WEB = """\
You are a corporate intelligence assistant.

You are given text scraped from one or more pages of a company's own website. \
Extract the current Board of Directors and Executive Management / C-Suite team.

The website may present these under different headings such as:
- Board of Directors, Board of Trustees, Supervisory Board, Advisory Board
- Executive Committee, Operating Committee, Leadership Team, Management Committee,
  Senior Leadership Team, Group Management Board

Schema:
{
  "board": [
    {"name": "Full Name", "title": "Exact title from the website"}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact C-Suite title from the website"}
  ]
}

Rules:
- board: Chairman, Non-Executive Directors, Independent Directors, \
  Supervisory Board members, Board of Trustees members. \
  Do NOT include executive directors who also hold a C-Suite role.
- executives: Members of the Operating Committee, Executive Committee, \
  Leadership Team, or Management Committee AND classic C-Suite — CEO, President, \
  COO, CFO, CTO, CIO, CISO, CMO, CHRO / Chief People Officer, CRO, \
  CLO / General Counsel, Chief Strategy Officer, Chief Digital Officer, \
  Chief Commercial Officer, and equivalent. \
  Do NOT include VPs, SVPs, MDs, or Directors unless they sit on \
  the Operating/Executive Committee explicitly listed on the website.
- Use the exact names and titles as they appear on the website.
- If the scraped text does not contain leadership information, return \
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""

_SYSTEM_FROM_KNOWLEDGE = """\
You are a corporate intelligence assistant with knowledge of public companies.

Return the CURRENT Board of Directors and C-Suite Executive Management for the \
named company, based on your training knowledge.

Schema:
{
  "board": [
    {"name": "Full Name", "title": "Board title (e.g. Independent Director, Chairman)"}
  ],
  "executives": [
    {"name": "Full Name", "title": "C-Suite title (e.g. CEO, CFO, COO)"}
  ]
}

Rules:
- board: Non-executive directors, independent directors, chairman only.
  Do NOT include C-Suite executives in the board list.
- executives: Members of the company's Operating Committee, Executive Committee, \
  Leadership Team, or Management Committee, plus classic C-Suite — CEO, President, \
  COO, CFO, CTO, CIO, CISO, CMO, CHRO, CRO, CLO / General Counsel, \
  Chief Strategy Officer, Chief Digital Officer, Chief Commercial Officer, and equivalent. \
  Do NOT include VPs, SVPs, MDs, or Directors unless they sit on the Operating/Executive Committee.
- Only include people you are highly confident are currently in role.
- If you have no reliable knowledge of this company's leadership, return \
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

_LEADERSHIP_CACHE: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def llm_fetch_leadership(company_name: str, domain: str = "") -> dict:
    """
    Fetch Board of Directors and Executive Management for a company.

    Priority:
      1. Scrape the company's own website via *domain* (e.g. "rmsindia.com").
         Tries common leadership/about/team page paths.
      2. Fall back to Claude's training-data knowledge of the company.

    Results are cached in-process (keyed on company_name+domain).

    Returns:
        {
            "board":      [{"name": str, "title": str}, ...],
            "executives": [{"name": str, "title": str}, ...],
        }
    """
    if not company_name or len(company_name.strip()) < 3:
        return {"board": [], "executives": []}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("LLM leadership skipped: ANTHROPIC_API_KEY not set")
        return {"board": [], "executives": []}

    cache_key = f"{company_name.strip().lower()}|{(domain or '').strip().lower()}"
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    # ── Strategy: website scrape first, fall back to LLM knowledge ──────────
    # Result always includes "_source": "web" | "ai" so callers can badge data.
    web_text = _fetch_leadership_text(domain) if domain else ""

    if web_text:
        logger.info(
            f"Scraping leadership for '{company_name}' from {domain} "
            f"({len(web_text)} chars)"
        )
        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=f"Company: {company_name}\n\nWebsite content:\n{web_text}",
            label=f"{company_name} [web]",
        )
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _LEADERSHIP_CACHE[cache_key] = result
            return result
        logger.info(
            f"Website scrape returned no leaders for '{company_name}' — "
            f"falling back to LLM knowledge"
        )

    # ── LLM knowledge fallback ───────────────────────────────────────────────
    logger.info(f"Using LLM knowledge for '{company_name}' leadership")
    result = _call_claude(
        system=_SYSTEM_FROM_KNOWLEDGE,
        user_msg=f"Company: {company_name}",
        label=f"{company_name} [knowledge]",
    )
    result["_source"] = "ai"
    _LEADERSHIP_CACHE[cache_key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(system: str, user_msg: str, label: str) -> dict:
    """Call Claude and parse the JSON leadership response."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model added them
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)

        data = json.loads(raw)
        result: dict = {
            "board":      _clean_list(data.get("board",      [])),
            "executives": _clean_list(data.get("executives", [])),
        }
        logger.info(
            f"Claude leadership ({label}): "
            f"{len(result['board'])} board, {len(result['executives'])} execs"
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning(f"Claude leadership JSON parse error ({label}): {exc}")
    except Exception as exc:
        logger.warning(f"Claude leadership failed ({label}): {exc}")

    return {"board": [], "executives": []}


def _clean_list(raw: list) -> list[dict]:
    """Validate and normalise a list of {name, title} dicts."""
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name  = str(item.get("name",  "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        if name and title and len(name.split()) >= 2:
            out.append({"name": name, "title": title})
    return out
