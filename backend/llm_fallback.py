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

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT DATA EXTRACTION  (Next.js / React / Angular embedded JSON)
# ─────────────────────────────────────────────────────────────────────────────

def _walk_json_for_people(obj: object, depth: int = 0) -> list[str]:
    """
    Recursively walk a parsed JSON object looking for {name, title} pairs.
    Works for any nesting depth up to 6 levels.
    """
    if depth > 6:
        return []
    results: list[str] = []
    if isinstance(obj, dict):
        name  = str(obj.get("name")      or obj.get("fullName")    or
                    obj.get("personName") or obj.get("displayName") or "").strip()
        title = str(obj.get("title")     or obj.get("jobTitle")    or
                    obj.get("position")  or obj.get("role")        or
                    obj.get("designation") or "").strip()
        if name and title and len(name.split()) >= 2 and len(name) < 60:
            results.append(f"{name} — {title}")
        for v in obj.values():
            results.extend(_walk_json_for_people(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_walk_json_for_people(item, depth + 1))
    return results


def _extract_js_data(html: str) -> str:
    """
    Pull leadership data from JavaScript embedded in the page.

    Targets:
    - Next.js  __NEXT_DATA__ JSON blobs (most major financial sites use Next.js)
    - Generic  window.__INITIAL_STATE__  /  window.__APP_DATA__  patterns
    - Inline script blocks with arrays keyed on boardMembers / directors /
      executives / leadershipTeam / teamMembers
    - JSON-formatted data attributes on DOM elements

    Returns a string of "Name — Title" lines (may be empty).
    """
    candidates: list[str] = []

    # ── Next.js: <script id="__NEXT_DATA__" type="application/json">
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    if m:
        try:
            candidates.extend(_walk_json_for_people(json.loads(m.group(1))))
        except (json.JSONDecodeError, ValueError):
            pass

    # ── Generic window.__* state patterns in <script> blocks
    for pat in [
        r'window\.__(?:INITIAL_STATE|APP_DATA|DATA|STATE|PRELOADED_STATE)__\s*=\s*(\{.*?\});',
        r'var\s+(?:initialData|pageData|appData|stateData)\s*=\s*(\{.*?\});',
    ]:
        for m in re.finditer(pat, html, flags=re.DOTALL | re.IGNORECASE):
            try:
                candidates.extend(_walk_json_for_people(json.loads(m.group(1))))
            except (json.JSONDecodeError, ValueError):
                pass

    # ── Keyed arrays: "boardMembers":[...], "directors":[...] etc.
    _ARRAY_KEYS = (
        r'boardMembers?|directors?|executives?|leadership(?:Team)?'
        r'|teamMembers?|managementTeam|committeeMembers?'
    )
    for m in re.finditer(
        rf'"(?:{_ARRAY_KEYS})"\s*:\s*(\[.*?\])',
        html, flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            candidates.extend(_walk_json_for_people(json.loads(m.group(1))))
        except (json.JSONDecodeError, ValueError):
            pass

    # Deduplicate preserving order
    seen: set[str] = set()
    lines: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            lines.append(c)

    logger.debug("JS data extraction: %d name/title pairs found", len(lines))
    return "\n".join(lines[:80])


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


def _scrape_wikipedia(company_name: str) -> str:
    """
    Search Wikipedia for the company article and extract leadership sections.
    Wikipedia is static HTML and often has comprehensive board / exec tables
    for public companies.  Returns a plain-text excerpt (≤ 6 KB) or "".
    """
    try:
        import httpx
        from urllib.parse import quote_plus
    except ImportError:
        return ""

    try:
        # Step 1: opensearch to find the right article title
        search = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch", "search": company_name,
                "limit": 3, "format": "json",
            },
            timeout=4,
            headers={"User-Agent": _SEARCH_UA},
        )
        if search.status_code != 200:
            return ""
        payload = search.json()
        titles = payload[1] if len(payload) > 1 else []
        urls   = payload[3] if len(payload) > 3 else []
        if not titles:
            return ""

        page_url = urls[0] if urls else f"https://en.wikipedia.org/wiki/{quote_plus(titles[0])}"

        # Step 2: fetch the article HTML (mobile version — lighter, easier to strip)
        page_url_mobile = page_url.replace("en.wikipedia.org", "en.m.wikipedia.org")
        resp = httpx.get(
            page_url_mobile, timeout=6,
            headers={"User-Agent": _SEARCH_UA},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            resp = httpx.get(page_url, timeout=6,
                             headers={"User-Agent": _SEARCH_UA},
                             follow_redirects=True)
        if resp.status_code != 200:
            return ""

        text = _strip_html(resp.text)

        # Step 3: carve out the relevant sections
        _LEADERSHIP_KW = re.compile(
            r"^(board\s+of\s+directors?|board\s+members?|directors?|"
            r"executive\s+(?:team|officers?|management)|"
            r"leadership|senior\s+management|management\s+team)",
            re.IGNORECASE,
        )
        _STOP_KW = re.compile(
            r"^(history|products?|services?|finances?|controversy|see\s+also"
            r"|references|external\s+links|operations?|subsidiaries)",
            re.IGNORECASE,
        )

        lines = [l.strip() for l in text.split("\n") if l.strip()]
        relevant: list[str] = []
        in_section = False
        for line in lines:
            if _LEADERSHIP_KW.match(line):
                in_section = True
                relevant.append(f"=== {line} ===")
                continue
            if in_section:
                if _STOP_KW.match(line) and len(line) < 60:
                    in_section = False
                    continue
                relevant.append(line)
                if len(relevant) > 150:
                    break

        result = "\n".join(relevant[:120])[:6_000]
        if result:
            logger.debug(
                "Wikipedia extracted %d chars for '%s'", len(result), company_name
            )
        return result

    except Exception as exc:
        logger.debug("Wikipedia leadership scrape failed: %s", exc)
        return ""


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
    "/about-us/leadership",
    "/about-us/our-leadership",
    "/about-us/governance/board-of-directors",  # Morgan Stanley
    "/about-us/governance",
    "/management",
    "/about/management",
    "/executive-team",
    "/our-team",
    "/board-of-directors",
    "/about/board-of-directors",
    "/about-us/board-of-directors",
    "/governance/board-of-directors",
    "/governance/board",
    "/investors/governance/board-of-directors",
    "/investor-relations/governance/board-of-directors",
    "/investor-relations/corporate-governance",
    "/corporate-governance/board-of-directors",
    "/corporate-governance",
    "/who-we-are/leadership",
    "/who-we-are/board-of-directors",
    "/people/board",
    "/people/leadership",
    "/our-people/leadership",
    "/team",
    "/about",
    "/about-us",
    "/company/leadership",
    "/company/management",
    "/company/board-of-directors",
    # International / global company URL patterns
    "/en/about/leadership",
    "/en/about-us/leadership",
    "/en/who-we-are/leadership",
    "/en/about/governance",
    "/global/about/leadership",
    "/global/governance/board-of-directors",
    "/global/en/about/leadership",
    "/global/en/about/governance",           # Deloitte
    "/global/en/about/leadership.html",
    "/global/en/about/governance.html",
    "/gx/en/about/leadership",               # PwC global
    "/gx/en/about/governance",
    "/us/en/about/leadership",
    "/uk/en/about/leadership",
    "/worldwide/about/leadership",
    "/pages/about/leadership",
    "/pages/about-deloitte/articles/global-leadership",
    "/about/our-firm/leadership",
    "/about/firm-leadership",
    "/our-firm/leadership",
    "/our-company/leadership",
    "/our-company/board-of-directors",
    "/press-room/bios",
    "/media/bios",
]

_SEARCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_WEB_TIMEOUT = 7          # seconds per HTTP request
_MAX_PAGE_CHARS = 18_000  # chars to pass to Claude per page
_MAX_PAGES = 8            # up to 8 pages (BOD + exec + committee + individual profiles)
_TOTAL_BUDGET = 35        # hard cap on website scraping time (seconds)

# Link patterns that indicate a sub-page with leadership content
_LEADERSHIP_LINK_RE = re.compile(
    r'href=["\']([^"\']*(?:'
    r'board[-_]of[-_]directors?|board-of-directors?'
    r'|operating[-_]committee|leadership[-_]team'
    r'|executive[-_]committee|management[-_]committee'
    r'|senior[-_]leadership|our[-_]leaders?'
    r'|supervisory[-_]board|advisory[-_]board'
    r'|governance[-_/]board|corporate[-_]governance'
    r'|audit[-_]committee|nominations[-_]committee'
    r'|compensation[-_]committee|risk[-_]committee'
    r'|board[-_]members?|director[-_]profiles?'
    r'|executive[-_]profiles?|management[-_]team'
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

    # Try main site + www + IR subdomains (investor-relations pages often have
    # full governance/board listings even when the marketing site is JS-heavy)
    base_candidates = [
        f"https://{domain}",
        f"https://www.{domain}",
        f"https://ir.{domain}",
        f"https://investors.{domain}",
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

                # ── Three extraction layers for each page ──────────────────
                # 1. JavaScript embedded data (Next.js/__NEXT_DATA__, etc.)
                #    Works on modern SPAs that embed state in the HTML even
                #    though visible text is rendered by JS at runtime.
                js_text = _extract_js_data(raw_html)

                # 2. JSON-LD structured data (<script type="application/ld+json">)
                ld_text = _extract_json_ld(raw_html)

                # 3. Plain-text strip (works for static-HTML sites)
                text = _strip_html(raw_html)

                # Merge all layers; JS data and JSON-LD win over thin plain text
                parts_for_page: list[str] = []
                if js_text:
                    parts_for_page.append(f"[JS Data]\n{js_text}")
                if ld_text:
                    parts_for_page.append(f"[Structured Data]\n{ld_text}")
                # Include plain text only when substantial or no structured data
                if len(text) >= 400 or (not js_text and not ld_text):
                    parts_for_page.append(text[:_MAX_PAGE_CHARS])

                combined = "\n\n".join(parts_for_page) if parts_for_page else ""

                # A page is "useful" if any layer produced content
                if not combined.strip():
                    logger.debug(f"Page produced no content, skipping: {url}")
                    continue
                if len(text) < 200 and not js_text and not ld_text:
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
                                        js2 = _extract_js_data(r2.text)
                                        ld2 = _extract_json_ld(r2.text)
                                        t2  = _strip_html(r2.text)
                                        sub_parts: list[str] = []
                                        if js2: sub_parts.append(f"[JS Data]\n{js2}")
                                        if ld2: sub_parts.append(f"[Structured Data]\n{ld2}")
                                        if len(t2) >= 400 or (not js2 and not ld2):
                                            sub_parts.append(t2[:_MAX_PAGE_CHARS])
                                        c2 = "\n\n".join(sub_parts)
                                        if c2.strip():
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
You are a corporate intelligence assistant. Extract every person explicitly \
named in the source text as a board member, executive, or senior leader.

Return ONE valid JSON object — no prose, no markdown:

{
  "board": [
    {"name": "Full Name", "title": "Exact title from source",
     "director_type": "Executive|Non-Executive|Independent|Nominee|unknown",
     "committees": [{"name": "Committee name", "role": "Chair|Member"}]}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact title",
     "function": "Finance|Technology|HR|Operations|Legal|Strategy|Sales|Marketing|Other",
     "scope": "Global|Regional|Country name|BU name or null"}
  ],
  "senior_leadership": [
    {"name": "Full Name", "title": "Exact title", "function_or_bu": "string"}
  ]
}

PLACEMENT:
board — everyone listed under Board of Directors / Supervisory Board / Board \
of Trustees, regardless of title: Chairman, Vice-Chairman, Managing Director \
(when on board), Executive Director, Non-Executive Director, Independent \
Director, Nominee Director, Lead Director. Include committee memberships if \
mentioned.

executives — EVERY person explicitly listed as a member of the Executive \
Committee, Operating Committee, Management Committee, Executive Leadership \
Team, or C-Suite, regardless of their specific title. This includes (but is \
NOT limited to): CEO, President, COO, CFO, CTO, CIO, CISO, CMO, CHRO, CRO, \
CLO / General Counsel, Chief Strategy Officer, Chief Digital Officer, Chief \
Commercial Officer, Group President — AND ALSO any EVP, Senior EVP, or \
business head who appears on the same committee listing page. If the source \
says "Operating Committee" or "Executive Team" and lists 14 people, put ALL \
14 in executives.

senior_leadership — EVPs, SVPs, VPs, Business Heads, Country Heads, Plant \
Heads, Division Heads, Group Heads, Regional Heads named in the source who \
are NOT already listed in executives above.

RULES:
- Include ONLY people explicitly named in the text — do not infer or invent.
- Use exact spelling of names and titles as they appear.
- "committees" = [] if not mentioned. "function" = "Other" if unclear.
- "scope" = null if not stated. "director_type" = "unknown" if unclear.
- EXCLUDE former, retired, ex-, past, emeritus office-holders.
- DUAL ROLES: if one person holds both a board title AND an executive title \
(e.g. "Executive Chairman & CEO"), list them in BOTH board and executives.
- If a section has no people, return an empty array.
Return ONLY valid JSON. No explanation, no markdown, no code blocks."""

_SYSTEM_FROM_KNOWLEDGE = """\
You are a corporate intelligence assistant with knowledge of public companies.

Return the current Board of Directors and C-Suite Executive Management for the \
named company based on your training knowledge. Only include people currently \
serving — exclude anyone described as former, retired, ex-, or emeritus.

Schema:
{
  "board": [{"name": "Full Name", "title": "Board title"}],
  "executives": [{"name": "Full Name", "title": "C-Suite title"}]
}

board: Chairman, Non-executive directors, Independent directors only.
executives: CEO, COO, CFO, CTO, CIO, CMO, CHRO, CLO / General Counsel, \
Chief Strategy Officer, and Operating/Executive Committee members.

DUAL ROLES: If one person holds both a board title AND an executive title \
(e.g. "Executive Chairman & CEO", "Chairman and CEO"), include them in BOTH \
the board array (with their board title) AND the executives array (with their \
executive title). Do not omit them from either.

IMPORTANT: Only return names you are confident about. If you have no reliable \
knowledge of this company's current leadership, return {"board": [], "executives": []}.
Return ONLY valid JSON. No explanation, no markdown, no code blocks."""


# ─────────────────────────────────────────────────────────────────────────────
# DUCKDUCKGO SNIPPET SEARCH  (supplements direct scraping for JS-heavy sites)
# ─────────────────────────────────────────────────────────────────────────────

def _ddg_leadership_snippets(company_name: str) -> str:
    """
    Search DuckDuckGo for company leadership and return result snippets.

    Uses three strategies in order:
      1. DuckDuckGo HTML search (html.duckduckgo.com/html/) — most snippets
      2. DuckDuckGo Lite (lite.duckduckgo.com/lite/) — fallback if HTML blocked
      3. DuckDuckGo Instant Answer API — zero-click company summary

    Returns a pipe-joined string of up to 15 snippets (max 8 KB) or "".
    """
    try:
        import httpx
        from urllib.parse import quote_plus
    except ImportError:
        return ""

    # Four targeted queries: BOD-focused first, then exec team
    queries = [
        f'"{company_name}" board of directors chairman non-executive independent director',
        f'"{company_name}" board members list directors site:reuters.com OR site:bloomberg.com OR site:ft.com',
        f'"{company_name}" CEO CFO COO president chief executive operating committee',
        f'"{company_name}" executive management leadership team annual report',
    ]

    ddg_headers = {
        "User-Agent": _SEARCH_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }

    snippets: list[str] = []

    def _parse_ddg_html(html: str) -> list[str]:
        """Extract snippets from DDG HTML — handles both <a> and <div> wrapper."""
        found: list[str] = []
        # Primary pattern: class="result__snippet" on any element
        for m in re.finditer(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|div|span|td)>',
            html, flags=re.DOTALL | re.IGNORECASE
        ):
            s = re.sub(r"<[^>]+>", " ", m.group(1))
            s = re.sub(r"\s+", " ", s).strip()
            if s and len(s) > 30:
                found.append(s)
        # Fallback: any element with snippet-like class
        if not found:
            for m in re.finditer(
                r'class="[^"]*snippet[^"]*"[^>]*>(.*?)</(?:a|div|span|td)>',
                html, flags=re.DOTALL | re.IGNORECASE
            ):
                s = re.sub(r"<[^>]+>", " ", m.group(1))
                s = re.sub(r"\s+", " ", s).strip()
                if s and len(s) > 30:
                    found.append(s)
        return found

    def _parse_lite_html(html: str) -> list[str]:
        """Extract snippets from DDG Lite HTML (td.result-snippet)."""
        found: list[str] = []
        for m in re.finditer(
            r'class="result-snippet"[^>]*>(.*?)</(?:td|div|span)>',
            html, flags=re.DOTALL | re.IGNORECASE
        ):
            s = re.sub(r"<[^>]+>", " ", m.group(1))
            s = re.sub(r"\s+", " ", s).strip()
            if s and len(s) > 30:
                found.append(s)
        return found

    for query in queries:
        if len(snippets) >= 15:
            break
        enc = quote_plus(query)

        # Strategy 1: DDG HTML
        try:
            resp = httpx.get(
                f"https://html.duckduckgo.com/html/?q={enc}",
                headers=ddg_headers, timeout=5, follow_redirects=True,
            )
            if resp.status_code == 200:
                found = _parse_ddg_html(resp.text)
                snippets.extend(found)
                if found:
                    continue   # good result — move to next query
        except Exception as exc:
            logger.debug("DDG HTML search failed: %s", exc)

        # Strategy 2: DDG Lite (simpler HTML, less likely to be blocked)
        try:
            resp_lite = httpx.get(
                f"https://lite.duckduckgo.com/lite/?q={enc}",
                headers=ddg_headers, timeout=5, follow_redirects=True,
            )
            if resp_lite.status_code == 200:
                snippets.extend(_parse_lite_html(resp_lite.text))
        except Exception as exc:
            logger.debug("DDG Lite search failed: %s", exc)

    # Strategy 3: DDG Instant Answer API (company overview, often names key people)
    if len(snippets) < 5:
        try:
            ia_resp = httpx.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": f"{company_name} board of directors executives",
                    "format": "json",
                    "no_html": "1",
                    "skip_disambig": "1",
                },
                headers={"User-Agent": _SEARCH_UA},
                timeout=5,
            )
            if ia_resp.status_code == 200:
                ia = ia_resp.json()
                # AbstractText: paragraph from Wikipedia/Infobox
                abstract = ia.get("AbstractText", "").strip()
                if abstract and len(abstract) > 50:
                    snippets.append(f"[Overview] {abstract}")
                # RelatedTopics often list executives
                for topic in ia.get("RelatedTopics", [])[:10]:
                    if isinstance(topic, dict):
                        txt = topic.get("Text", "").strip()
                        if txt and len(txt) > 30:
                            snippets.append(txt)
        except Exception as exc:
            logger.debug("DDG Instant Answer failed: %s", exc)

    # Deduplicate preserving order
    seen_set: set[str] = set()
    unique: list[str] = []
    for s in snippets:
        key = s[:80]
        if key not in seen_set:
            seen_set.add(key)
            unique.append(s)

    result = " | ".join(unique[:15])[:8_000]
    logger.debug("DDG snippets for '%s': %d items, %d chars", company_name, len(unique), len(result))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PARALLEL.AI  (api.parallel.ai — web research agent, task-based)
# ─────────────────────────────────────────────────────────────────────────────

_PARALLEL_BASE            = "https://api.parallel.ai"
_PARALLEL_POLL_INTERVAL   = 4    # seconds between status polls
_PARALLEL_TASK_TIMEOUT    = 80   # default timeout (seconds)
_PARALLEL_TASK_TIMEOUT_BOD = 160 # debug shows ~160s needed for full governance research
_PARALLEL_TASK_TIMEOUT_EM  = 80  # leadership pages render faster
_PARALLEL_MAX_CHARS       = 50_000  # raised from 20K — large boards (14+ members) need more


def _parallel_run(query: str, api_key: str,
                  timeout: int = _PARALLEL_TASK_TIMEOUT) -> str:
    """
    Submit a research query to Parallel.AI, poll until done, return text.
    Returns "" on failure or timeout.
    """
    try:
        import httpx
    except ImportError:
        return ""

    import time

    hdrs = {"Content-Type": "application/json", "x-api-key": api_key}

    # ── Create task ───────────────────────────────────────────────────────────
    try:
        resp = httpx.post(
            f"{_PARALLEL_BASE}/v1/tasks/runs",
            headers=hdrs,
            json={"input": query, "processor": "base"},
            timeout=15,
        )
        if not resp.is_success:
            logger.warning("Parallel.AI task creation failed %d: %s",
                           resp.status_code, resp.text[:200])
            return ""
        run_id = resp.json()["run_id"]
        logger.info("Parallel.AI task created: %s", run_id)
    except Exception as exc:
        logger.warning("Parallel.AI create error: %s", exc)
        return ""

    # ── Poll status ───────────────────────────────────────────────────────────
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_PARALLEL_POLL_INTERVAL)
        try:
            st = httpx.get(f"{_PARALLEL_BASE}/v1/tasks/runs/{run_id}",
                           headers=hdrs, timeout=20)
            if not st.is_success:
                continue
            data = st.json()
            status = data.get("status", "")
            if status == "failed":
                logger.warning("Parallel.AI task failed: %s", run_id)
                return ""
            if status == "completed" or not data.get("is_active", True):
                # ── Fetch result ──────────────────────────────────────────────
                res = httpx.get(f"{_PARALLEL_BASE}/v1/tasks/runs/{run_id}/result",
                                headers=hdrs, timeout=30)
                if not res.is_success:
                    logger.warning("Parallel.AI result fetch failed %d", res.status_code)
                    return ""
                payload = res.json()
                content = (payload.get("output") or {}).get("content") or {}
                text = (
                    content.get("output", "") if isinstance(content, dict)
                    else (content if isinstance(content, str) else "")
                )
                logger.info("Parallel.AI result: %d chars for run %s", len(text), run_id)
                return text[:_PARALLEL_MAX_CHARS]
        except Exception as exc:
            logger.debug("Parallel.AI poll error: %s", exc)

    logger.warning("Parallel.AI timed out after %ds for run %s", timeout, run_id)
    return ""


def _extract_leadership_json(text: str) -> dict | None:
    """
    Robustly extract {"board": [...], "executives": [...]} from any text.

    Uses bracket-counting instead of regex so titles containing "]" (e.g.
    "Independent Director [NED]", "Chairman [elected 2022]") don't break
    parsing.  Tries:
      1. Full JSON parse of the whole text (after stripping markdown fences).
      2. Bracket-counted extraction of the first/largest {...} object.
      3. Key-by-key bracket extraction for "board" and "executives" arrays.
    Returns None when no parseable leadership data is found.
    """
    if not text:
        return None

    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    def _try_parse(s: str) -> dict | None:
        try:
            data = json.loads(s)
            if not isinstance(data, dict):
                return None
            board = _clean_list(data.get("board", []))
            execs = _clean_list(data.get("executives", []))
            if board or execs:
                return {"board": board, "executives": execs}
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    # 1. Try the whole text as-is
    result = _try_parse(cleaned)
    if result:
        return result

    # 2. Find outermost {...} object via bracket counting (handles ] in strings)
    depth = 0
    obj_start = -1
    candidates: list[str] = []
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and obj_start >= 0:
                candidates.append(cleaned[obj_start: i + 1])
    for cand in candidates:
        result = _try_parse(cand)
        if result:
            return result

    # 3. Key-by-key bracket extraction for arrays
    def _extract_array(src: str, key: str) -> list[dict]:
        pattern = re.compile(rf'"{key}"\s*:\s*\[')
        m = pattern.search(src)
        if not m:
            return []
        arr_start = m.end() - 1   # position of '['
        depth_a = 0
        for i, ch in enumerate(src[arr_start:], arr_start):
            if ch == "[":
                depth_a += 1
            elif ch == "]":
                depth_a -= 1
                if depth_a == 0:
                    try:
                        arr = json.loads(src[arr_start: i + 1])
                        return _clean_list(arr if isinstance(arr, list) else [])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        return []

    board = _extract_array(cleaned, "board")
    execs = _extract_array(cleaned, "executives")
    if board or execs:
        return {"board": board, "executives": execs}

    return None


def _parallel_fetch_leadership(company_name: str, domain: str,
                               api_key: str) -> dict | None:
    """
    Single Parallel.AI research query that finds both Board of Directors and
    Executive Management in one pass.

    Parallel.AI browses the live website, annual reports, and governance pages.
    The result is plain research text — Claude extracts the structured JSON.

    Returns {"board": [...], "executives": [...]} or None on total failure.
    """
    # Ensure domain has protocol so Parallel.AI can navigate directly
    raw_domain = (domain or "").rstrip("/")
    if raw_domain and not raw_domain.startswith("http"):
        raw_domain = f"https://{raw_domain}"

    site_hint = f"\n\nCompany website: {raw_domain}" if raw_domain else ""

    query = (
        f'Research the complete current leadership of "{company_name}".{site_hint}\n\n'
        f'Find and list ALL of the following. For each person write their full name '
        f'and exact title exactly as shown on the source. Label each section clearly.\n\n'
        f'SECTION 1 — BOARD OF DIRECTORS:\n'
        f'Check the corporate governance / investor relations section and most recent '
        f'annual report. List every current board member with their exact board title: '
        f'Chairman, Vice-Chairman, Managing Director, Executive Director, '
        f'Non-Executive Director, Independent Director, Nominee Director. '
        f'Also note any board committee memberships (Audit, Nomination & Remuneration, '
        f'CSR, Risk, Stakeholder Relations) and whether each person chairs or is a '
        f'member of that committee.\n\n'
        f'SECTION 2 — EXECUTIVE MANAGEMENT:\n'
        f'Check the leadership / about / executive team page and the Executive / '
        f'Operating / Management Committee listing. List every current C-suite executive: '
        f'CEO, COO, CFO, CTO, CIO, CMO, CHRO, General Counsel / CLO, '
        f'Chief Strategy Officer, Chief Digital Officer, Chief Commercial Officer, '
        f'and ALL other members of the executive or management committee. '
        f'Note their functional area and geographic / BU scope if stated.\n\n'
        f'SECTION 3 — SENIOR LEADERSHIP / FUNCTIONAL HEADS:\n'
        f'Check the full leadership / team page, annual report leadership chapter, '
        f'and any division/business-unit pages. List every SVP, EVP, VP, '
        f'Business Head, Country Head, Plant Head, Division Head, Group Head '
        f'named on the site. Note their function or business unit.\n\n'
        f'Exclude anyone described as former, retired, or ex-. '
        f'Be exhaustive — do not skip people.'
    )

    text = _parallel_run(query, api_key, timeout=_PARALLEL_TASK_TIMEOUT_BOD)  # use longer BOD timeout
    if not text:
        logger.info("Parallel.AI returned empty for '%s'", company_name)
        return None

    logger.info("Parallel.AI research for '%s': %d chars — passing to Claude",
                company_name, len(text))
    return {"_raw_text": text}


# ─────────────────────────────────────────────────────────────────────────────
# JINA AI READER  (r.jina.ai — JS-rendered pages → clean markdown)
# ─────────────────────────────────────────────────────────────────────────────

_JINA_MAX_PAGE_CHARS = 20_000   # Jina output is already clean — allow more chars
_JINA_PAGE_TIMEOUT   = 12       # Jina needs time to render JS (seconds)
_JINA_SEARCH_TIMEOUT = 10       # s.jina.ai search (seconds)


def _jina_fetch_page(url: str, jina_key: str,
                     timeout: float = _JINA_PAGE_TIMEOUT) -> str:
    """
    Fetch a single URL through the Jina Reader API (r.jina.ai).

    Jina pre-renders JavaScript before returning clean markdown, making it
    effective for React/Next.js leadership pages that return empty shells to
    a plain httpx request.  Returns "" on failure or thin content.
    """
    try:
        import httpx
    except ImportError:
        return ""
    try:
        resp = httpx.get(
            f"https://r.jina.ai/{url}",
            headers={
                "Authorization":   f"Bearer {jina_key}",
                "Accept":          "text/plain",
                "X-Return-Format": "markdown",
                "X-Timeout":       str(int(min(timeout, 10))),
                "X-No-Cache":      "false",          # use Jina's cache when possible
            },
            timeout=timeout + 2,                     # client timeout > server timeout
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.debug("Jina reader HTTP %d for %s", resp.status_code, url)
            return ""
        text = resp.text.strip()
        # Reject thin pages (404 shells, login walls, redirect stubs)
        if len(text) < 300:
            return ""
        lower = text.lower()
        # Skip obvious error/gate pages
        if any(kw in lower for kw in [
            "page not found", "404 error", "access denied",
            "sign in to continue", "login to view", "403 forbidden",
        ]):
            return ""
        logger.debug("Jina fetched %s (%d chars)", url, len(text))
        return text[:_JINA_MAX_PAGE_CHARS]
    except Exception as exc:
        logger.debug("Jina page fetch failed for %s: %s", url, exc)
        return ""


def _jina_fetch_leadership(domain: str, jina_key: str) -> str:
    """
    Use the Jina Reader API to scrape leadership pages from *domain*.

    Tries the same _LEADERSHIP_PATHS used by the raw-HTTP scraper, but via
    Jina so that JavaScript-rendered pages (React/Next.js/Angular SPAs) are
    pre-rendered before text extraction.

    Returns concatenated clean markdown from all useful pages, or "".
    """
    if not domain or not jina_key:
        return ""

    import time
    deadline = time.monotonic() + _TOTAL_BUDGET

    bases = [
        f"https://www.{domain}",
        f"https://{domain}",
        f"https://ir.{domain}",
        f"https://investors.{domain}",
    ]

    collected: list[str] = []
    tried: set[str] = set()

    for base in bases:
        for path in _LEADERSHIP_PATHS:
            if len(collected) >= _MAX_PAGES or time.monotonic() >= deadline:
                break
            url = f"{base}{path}"
            if url in tried:
                continue
            tried.add(url)
            remaining = max(2.0, deadline - time.monotonic())
            text = _jina_fetch_page(url, jina_key,
                                    timeout=min(_JINA_PAGE_TIMEOUT, remaining))
            if text:
                # Check the returned text has leadership signal before keeping it
                lower = text.lower()
                if any(t in lower for t in [
                    "director", "chairman", "chief", "ceo", "president",
                    "officer", "executive", "board", "governance", "management",
                ]):
                    collected.append(f"[Page: {url}]\n{text}")
                    logger.info("Jina leadership page: %s (%d chars)", url, len(text))
        if len(collected) >= _MAX_PAGES or time.monotonic() >= deadline:
            break

    return "\n\n---\n\n".join(collected)


def _jina_search_snippets(company_name: str, jina_key: str) -> str:
    """
    Use Jina's s.jina.ai web search to find leadership data.

    Returns clean text from the top search results (≤ 12 KB) or "".
    Supplements / replaces DuckDuckGo — Jina renders each result page so
    snippets are fuller than DDG's 200-char extracts.
    """
    if not jina_key:
        return ""

    try:
        import httpx
        from urllib.parse import quote
    except ImportError:
        return ""

    queries = [
        f"{company_name} board of directors chairman non-executive independent directors",
        f"{company_name} CEO CFO COO CTO executive management team C-suite leadership",
    ]

    parts: list[str] = []
    for query in queries:
        try:
            resp = httpx.get(
                f"https://s.jina.ai/{quote(query)}",
                headers={
                    "Authorization":   f"Bearer {jina_key}",
                    "Accept":          "text/plain",
                    "X-Return-Format": "text",
                },
                timeout=_JINA_SEARCH_TIMEOUT,
            )
            if resp.status_code == 200 and len(resp.text) > 100:
                parts.append(resp.text[:6_000])
                logger.debug("Jina search '%s': %d chars", query[:60], len(resp.text))
        except Exception as exc:
            logger.debug("Jina search failed for '%s': %s", query[:60], exc)

    result = "\n\n---\n\n".join(parts)[:12_000]
    logger.info("Jina search for '%s': %d chars total", company_name, len(result))
    return result


# URL pattern matching leadership-type pages in search results
_LEADERSHIP_URL_RE = re.compile(
    r'https?://[^\s\)\"\'<>\]\[]+(?:'
    r'leadership|board[-_]?of[-_]?directors?|executive[-_]?(?:team|committee|management)'
    r'|governance|about(?:[-_]us)?|management[-_]?team|our[-_]?(?:people|leaders?|team)'
    r'|who[-_]we[-_]are|our[-_]firm|operating[-_]committee|supervisory[-_]board'
    r')[^\s\)\"\'<>\]\[]*',
    re.IGNORECASE,
)


def _jina_search_and_fetch(company_name: str, domain: str, jina_key: str) -> str:
    """
    Discover the company's actual leadership page URLs via Jina Search,
    then fetch those pages via Jina Reader for full JS-rendered content.

    Solves the case where _LEADERSHIP_PATHS doesn't match the company's URL
    structure (e.g. Deloitte's /global/en/about/governance.html, PwC's
    /gx/en/about/leadership.html).

    Returns concatenated markdown from found pages (≤ 3 pages), or "".
    """
    if not jina_key:
        return ""

    try:
        import httpx
        from urllib.parse import quote
    except ImportError:
        return ""

    domain_filter = f" site:{domain}" if domain else ""
    queries = [
        f"{company_name} board of directors governance leadership page{domain_filter}",
        f"{company_name} executive management team leadership{domain_filter}",
    ]

    found_urls: list[str] = []
    seen_urls: set[str] = set()

    for query in queries:
        try:
            resp = httpx.get(
                f"https://s.jina.ai/{quote(query)}",
                headers={
                    "Authorization":   f"Bearer {jina_key}",
                    "Accept":          "text/plain",
                    "X-Return-Format": "text",
                },
                timeout=_JINA_SEARCH_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            text = resp.text
            for url in _LEADERSHIP_URL_RE.findall(text):
                url = url.rstrip(".,;)")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                # Prioritise URLs from the company's own domain
                if domain and domain in url:
                    found_urls.insert(0, url)
                else:
                    found_urls.append(url)
        except Exception as exc:
            logger.debug("Jina search-and-fetch query failed: %s", exc)

    if not found_urls:
        logger.debug("Jina search-and-fetch: no leadership URLs found for '%s'", company_name)
        return ""

    logger.info("Jina search-and-fetch: %d candidate URLs for '%s'", len(found_urls), company_name)
    collected: list[str] = []
    for url in found_urls[:6]:
        if len(collected) >= 3:
            break
        text = _jina_fetch_page(url, jina_key)
        if not text:
            continue
        lower = text.lower()
        if any(kw in lower for kw in [
            "director", "chairman", "chief", "ceo", "president",
            "officer", "executive", "board", "governance", "management",
        ]):
            collected.append(f"[Page: {url}]\n{text}")
            logger.info("Jina search-found page: %s (%d chars)", url, len(text))

    return "\n\n---\n\n".join(collected)


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

    Scraping priority (web-only, no AI knowledge fallback):
      0.  Parallel.AI research agent — browses the real site + annual reports,
          understands the question, returns structured name/title output.
          Activated when PARALLEL_API_KEY is set. Best overall quality.
      1a. Jina Reader — path-based (JINA_API_KEY, known _LEADERSHIP_PATHS).
      1b. Jina Search-and-Fetch — discovers actual leadership URLs via search.
      1c. Raw HTTP — httpx direct, static-HTML sites.
      2.  Search snippets: Jina Search → DuckDuckGo fallback.
      3.  Wikipedia static-HTML (always).

    Results are cached in-process (keyed on company_name + domain).

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

    parallel_key = os.environ.get("PARALLEL_API_KEY", "")
    jina_key     = os.environ.get("JINA_API_KEY", "")

    cache_key = f"{company_name.strip().lower()}|{(domain or '').strip().lower()}"
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    # ── Step 0: Parallel.AI research agent (primary — best quality) ──────────
    # Two concurrent queries (BOD + EM) — browses JS-rendered pages, annual
    # reports, and governance sections.  Returns JSON parsed directly (no Claude
    # extraction needed).  Falls back to raw text for the Claude path.
    web_text = ""
    if parallel_key:
        parallel_result = _parallel_fetch_leadership(company_name, domain, parallel_key)
        if parallel_result:
            # JSON parsed successfully → return immediately, skip Claude
            if parallel_result.get("board") or parallel_result.get("executives"):
                parallel_result["_source"] = "web"
                _LEADERSHIP_CACHE[cache_key] = parallel_result
                logger.info(
                    "Parallel.AI direct-JSON for '%s': %d board, %d execs",
                    company_name,
                    len(parallel_result.get("board", [])),
                    len(parallel_result.get("executives", [])),
                )
                return parallel_result
            # Raw text fallback (JSON parse failed) — pass to Claude below
            web_text = parallel_result.get("_raw_text", "")
            if web_text:
                logger.info(
                    "Parallel.AI raw-text fallback for '%s': %d chars",
                    company_name, len(web_text),
                )

    # ── Step 1a: Jina Reader — path-based (known URL patterns) ──────────────
    if not web_text and jina_key and domain:
        web_text = _jina_fetch_leadership(domain, jina_key)
        logger.info("Step 1a Jina path-based for '%s': %d chars", domain, len(web_text))

    # ── Step 1b: Jina Search-and-Fetch — discover actual leadership URLs ─────
    # Handles companies whose URL structure doesn't match _LEADERSHIP_PATHS
    # (e.g. Deloitte /global/en/about/governance.html, PwC /gx/en/about/leadership)
    if jina_key and not web_text:
        web_text = _jina_search_and_fetch(company_name, domain, jina_key)
        if web_text:
            logger.info("Step 1b Jina search-and-fetch for '%s': %d chars", company_name, len(web_text))

    # ── Step 1c: Raw HTTP fallback (when Jina unavailable or still empty) ────
    # Skip when Parallel.AI is configured — it already browsed the site (even if
    # it timed out). Raw HTTP on a JS-rendered SPA (Next.js/React) returns a
    # shell with no leadership content and wastes 35s of budget for nothing.
    if not web_text and domain and not parallel_key:
        logger.info("Falling back to raw HTTP for %s", domain)
        web_text = _fetch_leadership_text(domain)

    # ── Step 2: Search snippets ───────────────────────────────────────────────
    # Skip when Parallel.AI already returned authoritative research text.
    # Search snippets (DDG/Jina) mention employees in news context and add
    # noise — Claude picks up names that pass _strip_hallucinations but aren't
    # on the actual Operating Committee / Board.
    parallel_gave_web_text = bool(parallel_key and web_text)
    if parallel_gave_web_text:
        search_text = ""
        logger.info("Skipping search snippets — Parallel.AI web text is authoritative")
    elif jina_key:
        search_text = _jina_search_snippets(company_name, jina_key)
        if not search_text:
            search_text = _ddg_leadership_snippets(company_name)
    else:
        search_text = _ddg_leadership_snippets(company_name)

    # ── Step 3: Wikipedia (always — valuable for public companies) ───────────
    wiki_text = _scrape_wikipedia(company_name)

    src_label = (
        "parallel_raw" if (parallel_key and not jina_key and web_text)
        else "jina"     if (jina_key and web_text)
        else "raw_http" if web_text
        else "none"
    )
    logger.info(
        "Leadership context '%s': web=%d chars (%s), search=%d chars, wiki=%d chars",
        company_name, len(web_text), src_label, len(search_text), len(wiki_text),
    )

    # ── Step 4: Extract via Claude if any context found ───────────────────────
    if web_text or search_text or wiki_text:
        parts: list[str] = [f"Company: {company_name}"]
        if web_text:
            parts.append(f"[Website content]\n{web_text}")
        if search_text:
            src_label_str = "Jina Web Search" if jina_key else "Search result snippets"
            parts.append(f"[{src_label_str}]\n{search_text}")
        if wiki_text:
            parts.append(f"[Wikipedia excerpt]\n{wiki_text}")
        user_msg = "\n\n".join(parts)

        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=user_msg,
            label=f"{company_name} [{'jina' if jina_key else 'web'}+search+wiki]",
            source_text=user_msg,   # used to strip hallucinated names
        )
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _LEADERSHIP_CACHE[cache_key] = result
            return result
        logger.info(
            "Web+search+wiki context returned no leaders for '%s'", company_name
        )

    # ── Step 5: No knowledge fallback — web-sourced data only ───────────────
    # Knowledge fallback is disabled to prevent showing unverified executives.
    # Only people explicitly named in real web sources are shown.
    # NOTE: Do NOT cache empty results — allows retry on next upload in case
    # Parallel.AI was temporarily unavailable or timed out.
    logger.info("No web-sourced leaders found for '%s' — returning empty (not cached)", company_name)
    return {"board": [], "executives": [], "_source": "none"}


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_HALLUCINATION_CHECK_MIN_CHARS = 200  # apply name verification even for short sources

def _name_in_source(name: str, source_lower: str) -> bool:
    """
    Return True if the person's name is evidenced in the source text.

    Matching strategy (first match wins):
    1. Full name exact match: "charles scharf" in source → True
    2. All significant name parts appear individually: "charles" in source
       AND "scharf" in source → True (handles middle initials, order differences)
    3. Surname alone appears AND is ≥4 chars to reduce false positives
    """
    if not name or not source_lower:
        return False
    name_lower = name.lower()
    # 1. Exact full-name match
    if name_lower in source_lower:
        return True
    # 2. All significant parts (≥2 chars) present anywhere in source
    parts = [p for p in name_lower.split() if len(p) >= 2]
    if len(parts) >= 2 and all(p in source_lower for p in parts):
        return True
    # 3. Surname alone (≥4 chars) — fallback for initials like "C. Scharf"
    surname = name_lower.split()[-1] if name_lower.split() else ""
    if len(surname) >= 4 and surname in source_lower:
        return True
    return False


def _strip_hallucinations(result: dict, source_text: str) -> dict:
    """
    Remove any person whose name does not appear in the source text.
    Prevents Claude from fabricating plausible-sounding names when the source
    contains little or no real leadership data.

    Skipped entirely when source_text is shorter than _HALLUCINATION_CHECK_MIN_CHARS —
    thin sources can't reliably distinguish real extractions from hallucinations.
    """
    if not source_text or len(source_text) < _HALLUCINATION_CHECK_MIN_CHARS:
        if source_text:
            logger.debug(
                "Skipping hallucination check — source too thin (%d chars)", len(source_text)
            )
        return result
    src = source_text.lower()
    counts_before = sum(
        len(result.get(k, [])) for k in ("board", "executives", "senior_leadership")
    )
    for key in ("board", "executives", "senior_leadership"):
        result[key] = [
            p for p in result.get(key, [])
            if _name_in_source(p.get("name", ""), src)
        ]
    counts_after = sum(
        len(result.get(k, [])) for k in ("board", "executives", "senior_leadership")
    )
    removed = counts_before - counts_after
    if removed:
        logger.info("Stripped %d hallucinated names not found in source text", removed)
    return result


def _rich_to_flat(data: dict, source_text: str = "") -> dict:
    """
    Convert the rich 3-layer extraction schema to the flat {board, executives}
    format used by the rest of the pipeline.

    Mapping:
      board_of_directors → board  (with name + designation as title)
      executive_management → executives  (name + title)
      senior_leadership → also appended to executives so they appear in EM panel
      dual_role_individuals → logged only (already captured in both layers)
    """
    def _board_entry(b: dict) -> dict | None:
        name = str(b.get("name") or "").strip()
        title = str(b.get("designation") or b.get("title") or "").strip()
        if not name or not title or len(name.split()) < 2:
            return None
        if _is_retired(name, title):
            return None
        entry: dict = {"name": name, "title": title}
        # Carry through rich fields for the frontend to use optionally
        if b.get("director_type"):
            entry["director_type"] = b["director_type"]
        if b.get("committees"):
            entry["committees"] = b["committees"]
        if b.get("appointed"):
            entry["appointed"] = b["appointed"]
        if b.get("confidence"):
            entry["confidence"] = b["confidence"]
        return entry

    def _exec_entry(e: dict, title_key: str = "title") -> dict | None:
        name = str(e.get("name") or "").strip()
        title = str(e.get(title_key) or "").strip()
        if not name or not title or len(name.split()) < 2:
            return None
        if _is_retired(name, title):
            return None
        entry: dict = {"name": name, "title": title}
        for k in ("function", "function_or_bu", "reports_to", "scope", "confidence"):
            if e.get(k):
                entry[k] = e[k]
        return entry

    board = [e for b in data.get("board_of_directors", [])
             if (e := _board_entry(b)) is not None]
    execs = [e for ex in data.get("executive_management", [])
             if (e := _exec_entry(ex)) is not None]
    # Senior leadership goes into the executives panel (they're in-company leaders)
    senior = [e for sl in data.get("senior_leadership", [])
              if (e := _exec_entry(sl, "title")) is not None]
    execs = execs + senior

    result = {
        "board":            board,
        "executives":       execs,
        "senior_leadership": senior,          # kept separate too for frontend use
        "dual_roles":       data.get("dual_role_individuals", []),
        "data_gaps":        data.get("data_gaps", []),
    }

    # Post-extraction hallucination filter
    if source_text:
        result = _strip_hallucinations(result, source_text)

    return result


def _call_claude(system: str, user_msg: str, label: str,
                 source_text: str = "") -> dict:
    """
    Call Claude and parse the JSON leadership response.

    Handles:
    - New 3-array schema: {board, executives, senior_leadership}
    - Legacy 2-array schema: {board, executives}  (knowledge fallback)
    Always returns {"board": [...], "executives": [...], "senior_leadership": [...]}.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    _MODEL_CANDIDATES = [
        "claude-haiku-4-5-20251001",       # Claude Haiku 4.5 dated
        "claude-haiku-4-5",                # Claude Haiku 4.5 latest alias
        "claude-3-5-haiku-20241022",       # Claude 3.5 Haiku (always works)
    ]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = None
        last_exc: Exception | None = None
        for model_id in _MODEL_CANDIDATES:
            try:
                response = client.messages.create(
                    model=model_id,
                    max_tokens=6144,  # raised from 4096 — large boards need more output
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                )
                logger.debug("Claude call succeeded with model: %s", model_id)
                break
            except Exception as _me:
                last_exc = _me
                logger.debug("Model %s failed: %s — trying next", model_id, _me)
        if response is None:
            raise last_exc or RuntimeError("All Claude models failed")
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model added them
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)

        data = json.loads(raw)

        # ── New 3-array schema (web extraction) ──────────────────────────────
        if "board_of_directors" in data or "executive_management" in data:
            # Handle old rich-schema keys if model accidentally used them
            result = _rich_to_flat(data, source_text)
        else:
            # Standard schema: board / executives / senior_leadership
            def _enrich_board(items: list) -> list:
                out = []
                for b in items:
                    if not isinstance(b, dict):
                        continue
                    name  = str(b.get("name", "") or "").strip()
                    title = str(b.get("title", "") or b.get("designation", "") or "").strip()
                    if not name or not title or len(name.split()) < 2:
                        continue
                    if _is_retired(name, title):
                        continue
                    entry: dict = {"name": name, "title": title}
                    for k in ("director_type", "committees", "function",
                              "scope", "function_or_bu"):
                        if b.get(k):
                            entry[k] = b[k]
                    out.append(entry)
                return out

            board   = _enrich_board(data.get("board",            []))
            execs   = _enrich_board(data.get("executives",        []))
            senior  = _enrich_board(data.get("senior_leadership", []))
            result  = {
                "board":            board,
                "executives":       execs + senior,   # senior goes into EM panel
                "senior_leadership": senior,
            }
            if source_text:
                result = _strip_hallucinations(result, source_text)

        logger.info(
            "Claude extraction (%s): %d board, %d execs (%d senior)",
            label,
            len(result.get("board", [])),
            len(result.get("executives", [])),
            len(result.get("senior_leadership", [])),
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning("Claude JSON parse error (%s): %s", label, exc)
    except Exception as exc:
        logger.warning("Claude leadership failed (%s): %s", label, exc)

    return {"board": [], "executives": [], "senior_leadership": []}


_RETIRED_RE = re.compile(
    r"^\s*(?:former|ex[- ]|retired|late|emeritus|past\s+)",
    re.IGNORECASE,
)
_RETIRED_TITLE_RE = re.compile(
    r"\b(?:former|retired|emeritus|ex[- ](?:ceo|cfo|coo|cto|chairman|director|president))\b",
    re.IGNORECASE,
)


def _is_retired(name: str, title: str) -> bool:
    """Return True when the name or title signals a former/retired executive."""
    # Title starts with "Former …", "Ex-CEO", "Retired …", "Emeritus …", "Past …"
    if _RETIRED_RE.search(title):
        return True
    # Title contains "retired", "emeritus", "former" anywhere
    if _RETIRED_TITLE_RE.search(title):
        return True
    # Name itself prefixed: "Former CEO John Smith" style (LLM sometimes does this)
    if _RETIRED_RE.search(name):
        return True
    return False


def _clean_list(raw: list) -> list[dict]:
    """Validate, normalise, and de-retire a list of {name, title} dicts.
    Drops former / retired / emeritus / ex- executives — only current
    office-holders should appear in the org structure.
    """
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name  = str(item.get("name",  "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        if not name or not title or len(name.split()) < 2:
            continue
        if _is_retired(name, title):
            logger.debug("Skipping retired/former executive: %s — %s", name, title)
            continue
        out.append({"name": name, "title": title})
    return out
