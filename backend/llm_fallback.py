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
    "/en/about/leadership",
    "/en/about-us/leadership",
    "/en/who-we-are/leadership",
    "/global/about/leadership",
    "/global/governance/board-of-directors",
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
You are a corporate intelligence assistant.

You are given text from up to three sources about a company:
1. Pages scraped from the company's own website (most authoritative).
2. Search engine result snippets from DuckDuckGo (useful when the company site is \
   JavaScript-rendered and the scraped HTML is sparse).
3. Wikipedia article excerpt (reliable for board composition of public companies).

Extract the current Board of Directors and Executive Management / C-Suite team \
using ALL available context. Prefer explicit website content, but use search \
snippets and Wikipedia to fill gaps.

The website may present these under different headings such as:
- Board of Directors, Board of Trustees, Supervisory Board, Advisory Board
- Executive Committee, Operating Committee, Leadership Team, Management Committee,
  Senior Leadership Team, Group Management Board

Schema:
{
  "board": [
    {"name": "Full Name", "title": "Exact title (e.g. Chairman, Independent Director)"}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact C-Suite title (e.g. CEO, CFO, COO)"}
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
- Use names and titles as they appear in the source material.
- If the combined context does not contain leadership information, return \
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""

_SYSTEM_FROM_KNOWLEDGE = """\
You are a corporate intelligence assistant with knowledge of public companies.

Return the Board of Directors and C-Suite Executive Management for the named company \
based on your training knowledge. For well-known public companies return what you know \
even if it may be slightly dated — best-effort is strongly preferred over an empty result.

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
- board: Chairman, Non-executive directors, independent directors only.
  Do NOT include C-Suite executives in the board list.
- executives: Classic C-Suite — CEO, President, COO, CFO, CTO, CIO, CISO, CMO, \
  CHRO, CRO, CLO / General Counsel, Chief Strategy Officer, Chief Digital Officer, \
  Chief Commercial Officer, and Operating/Executive Committee members.
  Do NOT include VPs, SVPs, MDs, or Directors unless they sit on the committee.
- Return your best knowledge. Do NOT return empty just because data may be slightly dated.
- Only return {"board": [], "executives": []} if you have absolutely no knowledge of this company.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""


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

    # ── Step 1: scrape website + DDG snippets + Wikipedia ────────────────────
    web_text  = _fetch_leadership_text(domain) if domain else ""
    ddg_text  = _ddg_leadership_snippets(company_name)
    wiki_text = _scrape_wikipedia(company_name)

    logger.info(
        "Leadership context for '%s': web=%d chars, ddg=%d chars, wiki=%d chars",
        company_name, len(web_text), len(ddg_text), len(wiki_text),
    )

    # ── Step 2: if any context found, extract via Claude ─────────────────────
    if web_text or ddg_text or wiki_text:
        parts: list[str] = [f"Company: {company_name}"]
        if web_text:
            parts.append(f"[Website content]\n{web_text}")
        if ddg_text:
            parts.append(f"[Search result snippets]\n{ddg_text}")
        if wiki_text:
            parts.append(f"[Wikipedia excerpt]\n{wiki_text}")
        user_msg = "\n\n".join(parts)

        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=user_msg,
            label=f"{company_name} [web+ddg+wiki]",
        )
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _LEADERSHIP_CACHE[cache_key] = result
            return result
        logger.info(
            "Web+DDG+Wiki context returned no leaders for '%s' — "
            "falling back to LLM knowledge", company_name
        )

    # ── Step 3: LLM knowledge fallback ───────────────────────────────────────
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
