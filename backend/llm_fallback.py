"""
LLM company leadership enrichment — Board of Directors and Executive Management only.

Public function:

  llm_fetch_leadership(company_name, domain="")
    Uses Google Gemini 2.0 Flash with Google Search grounding to research and
    extract current Board of Directors and Executive Management for a company.

    Two-phase Gemini pipeline:
      Phase A — Research via google_search grounding: Gemini searches Google
                in real time and returns grounded text naming executives/directors
                with source citations.
      Phase B — Synthesis via structured JSON: Gemini extracts {board, executives}
                from Phase A text using responseMimeType: application/json.

    Falls back to Wikipedia + Claude Haiku when GEMINI_API_KEY is not set.

    Returns {"board": [...], "executives": [...]}  each item = {name, title}.
    Returns {"board": [], "executives": []} if no content is found.

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


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS  (Wikipedia only — all other scraping removed)
# ─────────────────────────────────────────────────────────────────────────────

_SEARCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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

        # ── Infobox extraction — Wikipedia infoboxes collapse to a single long
        # line containing "Key people" with the CEO/CFO.  Scan the first 200 lines
        # (infobox is always near the top) and extract the Key people substring.
        for line in lines[:200]:
            low = line.lower()
            if "key people" in low:
                # Grab from "Key people" to end of line, cap at 500 chars
                idx = low.find("key people")
                snippet = line[idx:idx + 500]
                # Trim at next financial/footer keyword
                for stop in ("revenue", "products", "website", "footnotes", "number of employees"):
                    si = snippet.lower().find(stop)
                    if 0 < si < 450:
                        snippet = snippet[:si]
                relevant.append(f"[Wikipedia Infobox] {snippet.strip()}")
                break

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


def _is_js_shell(html: str) -> bool:
    """
    Detect if a page is an empty JavaScript-rendered shell with no useful text.
    React/Next.js/Angular SPAs often return <div id="root"></div> with no content.
    Returns True when visible text is under 300 chars (from scraper.py).
    """
    # Remove scripts and styles to get what a human would actually see
    stripped = re.sub(
        r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    stripped = re.sub(
        r"<style[^>]*>.*?</style>", " ", stripped, flags=re.DOTALL | re.IGNORECASE
    )
    visible = re.sub(r"<[^>]+>", " ", stripped)
    visible = re.sub(r"\s+", " ", visible).strip()
    return len(visible) < 300


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPTS FOR LLM SYNTHESIS
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_FROM_WEB = """\
You are a corporate intelligence assistant. Extract every person explicitly \
named in the source text as a board member, executive, or senior leader.

Return ONE valid JSON object — no prose, no markdown:

{
  "board": [
    {"name": "Full Name", "title": "Exact title from source",
     "director_type": "Executive|Non-Executive|Independent|Nominee|unknown",
     "committees": [{"name": "Committee name", "role": "Chair|Member"}],
     "linkedin_url": "https://linkedin.com/in/... or null",
     "confidence": "HIGH|MEDIUM|LOW"}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact title",
     "function": "Finance|Technology|HR|Operations|Legal|Strategy|Sales|Marketing|Other",
     "scope": "Global|Regional|Country name|BU name or null",
     "linkedin_url": "https://linkedin.com/in/... or null",
     "confidence": "HIGH|MEDIUM|LOW"}
  ],
  "senior_leadership": [
    {"name": "Full Name", "title": "Exact title", "function_or_bu": "string",
     "linkedin_url": "https://linkedin.com/in/... or null",
     "confidence": "HIGH|MEDIUM|LOW"}
  ]
}

EXTRACTION TECHNIQUES — apply all:
1. JSON-LD / schema.org: Person blocks with name, jobTitle, sameAs (LinkedIn)
2. CSS heuristics: blocks with classes like person, member, leader, exec, bio, profile, card
3. Image alt text: name + title often encoded in alt attributes
4. H2/H3 headings: person name as heading followed by title in next element
5. List scanning: <ul>/<li> blocks with name + title patterns
6. LinkedIn URLs: scan ALL <a href> for "linkedin.com/in/" near a person's name
7. [JS Data] / [Structured Data] blocks: walk all name/jobTitle fields

CONFIDENCE:
HIGH — name and title found together on a named leadership page or JSON-LD
MEDIUM — name inferred from multiple signals (heading + nearby text)
LOW — name mentioned once, title uncertain

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
- "linkedin_url" = null if not found — do NOT construct URLs from names.
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
# GEMINI  (Google Search grounding + structured JSON synthesis)
# ─────────────────────────────────────────────────────────────────────────────

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_MODEL    = "gemini-2.0-flash"   # supports google_search grounding tool
# URL-path keywords that identify leadership / governance pages in search results
_GEMINI_URL_SIGNAL = {
    "board", "governance", "leadership", "director", "executive",
    "management", "investor", "officers", "oversight", "about",
}


def _gemini_discover_leadership_urls(
    company_name: str,
    api_key: str,
) -> tuple[list[str], str]:
    """
    Use Gemini 2.0 Flash with Google Search grounding to find Board of Directors
    and executive leadership page URLs for a company.

    Two targeted queries are submitted:
      1. Board of Directors — finds investor/governance subdomain pages, e.g.
         investor.onemainfinancial.com/governance/board-of-directors/default.aspx
      2. Executive leadership — finds about/leadership pages on the main domain

    Returns (urls, content):
      urls    — deduplicated list of grounding-source URLs with leadership signal
      content — Gemini's grounded text answer (may already name executives/titles)
    """
    try:
        import httpx
    except ImportError:
        return [], ""

    if not api_key or not company_name:
        return [], ""

    queries = [
        (f"Who are the current Board of Directors of {company_name}? "
         f"List each member's full name and title from the official corporate website."),
        (f"Who are the executive leadership team members of {company_name}? "
         f"List each executive's full name and title from the official company website."),
    ]

    all_urls:  list[str] = []
    all_texts: list[str] = []
    seen: set[str] = set()

    for query in queries:
        try:
            resp = httpx.post(
                f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": query}]}],
                    "tools":    [{"google_search": {}}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
                },
                timeout=45,
            )
            if not resp.is_success:
                logger.warning("Gemini search %d for '%s': %s",
                               resp.status_code, company_name, resp.text[:200])
                continue

            data = resp.json()
            for candidate in data.get("candidates", []):
                # Collect generated text (may already list names + titles)
                parts = candidate.get("content", {}).get("parts", [])
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                if text:
                    all_texts.append(text)

                # Collect grounding source URLs
                grounding = candidate.get("groundingMetadata", {})
                for chunk in grounding.get("groundingChunks", []):
                    url = chunk.get("web", {}).get("uri", "")
                    if url and url not in seen:
                        if any(kw in url.lower() for kw in _GEMINI_URL_SIGNAL):
                            seen.add(url)
                            all_urls.append(url)
                            logger.info("Gemini grounding URL for '%s': %s", company_name, url)

        except Exception as exc:
            logger.warning("Gemini query error for '%s': %s", company_name, exc)

    return all_urls, "\n\n---\n\n".join(all_texts)


_LINKEDIN_URL_RE = re.compile(
    r'https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%-]+(?:/[A-Za-z0-9_%-]*)?',
    re.IGNORECASE,
)


def _gemini_search_linkedin_batch(
    people: list[dict],
    company_name: str,
    api_key: str,
) -> dict[str, str]:
    """
    One Gemini google_search call to find LinkedIn profile URLs for a group of
    executives/directors at a company.

    people: [{"name": str, "title": str}, ...]
    Returns: {name_key: "https://www.linkedin.com/in/..."} for those found.
    name_key = first two words of name, lowercase letters only (matches _name_key).
    """
    try:
        import httpx
    except ImportError:
        return {}

    if not people or not api_key:
        return {}

    people_list = "\n".join(
        f"- {p['name']}{' (' + p['title'] + ')' if p.get('title') else ''}"
        for p in people[:25]
    )
    query = (
        f"Find LinkedIn profile URLs for these {company_name} executives and board members. "
        f"For each person listed below, provide their exact LinkedIn profile URL "
        f"(linkedin.com/in/...):\n{people_list}"
    )

    def _norm_key(name: str) -> str:
        words = re.sub(r"[^a-z ]", "", name.lower()).split()
        return " ".join(words[:2])

    try:
        resp = httpx.post(
            f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent",
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": query}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
            },
            timeout=45,
        )
        if not resp.is_success:
            logger.warning(
                "Gemini LinkedIn batch for '%s': HTTP %s", company_name, resp.status_code
            )
            return {}

        data = resp.json()
        result: dict[str, str] = {}

        for candidate in data.get("candidates", []):
            # ── Source 1: grounding chunk URIs (most reliable — direct LinkedIn URLs) ──
            grounding = candidate.get("groundingMetadata", {})
            for chunk in grounding.get("groundingChunks", []):
                url = chunk.get("web", {}).get("uri", "")
                if not url or "linkedin.com/in/" not in url.lower():
                    continue
                url_slug = url.split("/in/")[-1].split("/")[0].lower()
                for p in people:
                    key = _norm_key(p["name"])
                    if key in result:
                        continue
                    name_parts = key.split()
                    if (len(name_parts) >= 2
                            and name_parts[0] in url_slug
                            and name_parts[-1] in url_slug):
                        result[key] = url
                        logger.info("LinkedIn grounding chunk %s → %s", p["name"], url)
                    elif len(name_parts) >= 1 and name_parts[-1] in url_slug:
                        result[key] = url
                        logger.info("LinkedIn slug surname match %s → %s", p["name"], url)

            # ── Source 2: LinkedIn URLs embedded in generated text ─────────────────
            parts = candidate.get("content", {}).get("parts", [])
            text = " ".join(pt.get("text", "") for pt in parts if "text" in pt)
            for m in _LINKEDIN_URL_RE.finditer(text):
                url = m.group(0)
                context = text[max(0, m.start() - 200): m.start() + 200].lower()
                for p in people:
                    key = _norm_key(p["name"])
                    if key in result:
                        continue
                    name_parts = key.split()
                    if all(part in context for part in name_parts):
                        result[key] = url
                        logger.info("LinkedIn text context %s → %s", p["name"], url)

        logger.info(
            "Gemini LinkedIn batch for '%s': found %d / %d profiles",
            company_name, len(result), len(people),
        )
        return result

    except Exception as exc:
        logger.warning("Gemini LinkedIn batch failed for '%s': %s", company_name, exc)
        return {}


def _gemini_fetch_leadership(
    company_name: str,
    domain: str,
    api_key: str,
) -> dict:
    """
    Two-phase Gemini pipeline for structured leadership extraction.

    Phase A — Research (google_search grounding):
        Two grounded queries (BOD + exec team) return a grounded text corpus
        with source citations from Google Search results.

    Phase B — Synthesis (responseMimeType: application/json):
        The Phase A corpus is fed to Gemini without grounding; Gemini extracts
        a structured JSON {board, executives, senior_leadership} object.

    Returns {"board": [...], "executives": [...], "senior_leadership": [...]}
    or {} on failure.
    """
    try:
        import httpx
    except ImportError:
        return {}

    if not api_key or not company_name:
        return {}

    # ── Phase A: Research via Google Search grounding ─────────────────────────
    _urls, grounded_text = _gemini_discover_leadership_urls(company_name, api_key)

    if not grounded_text:
        logger.warning("Gemini Phase A returned no content for '%s'", company_name)
        return {}

    logger.info("Gemini Phase A for '%s': %d chars", company_name, len(grounded_text))

    # ── Phase B: Structured JSON synthesis ────────────────────────────────────
    domain_hint = f" (domain: {domain})" if domain else ""
    synthesis_prompt = (
        f"Extract the Board of Directors and Executive Management from the research "
        f"text below about {company_name}{domain_hint}.\n\n"
        f"IMPORTANT: Only include people explicitly named in the research text — "
        f"do NOT use training knowledge to add names not found in the text.\n\n"
        f"[Research text — sourced from Google Search]\n{grounded_text[:14_000]}"
    )

    try:
        resp = httpx.post(
            f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent",
            params={"key": api_key},
            json={
                "system_instruction": {"parts": [{"text": _SYSTEM_FROM_WEB}]},
                "contents": [{"parts": [{"text": synthesis_prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 4096,
                    "responseMimeType": "application/json",
                },
            },
            timeout=60,
        )
        if not resp.is_success:
            logger.warning(
                "Gemini Phase B for '%s': HTTP %s — %s",
                company_name, resp.status_code, resp.text[:300],
            )
            return {}

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return {}

        parts = candidates[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts if "text" in p).strip()

        if not raw:
            return {}

        # Strip markdown fences if Gemini added them despite responseMimeType
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",           "", raw, flags=re.MULTILINE)

        parsed = json.loads(raw)

        # Handle rich schema (board_of_directors / executive_management keys)
        if "board_of_directors" in parsed or "executive_management" in parsed:
            result = _rich_to_flat(parsed, grounded_text)
        else:
            board  = _clean_list(parsed.get("board",            []), is_board=True)
            execs  = _clean_list(parsed.get("executives",        []))
            senior = _clean_list(parsed.get("senior_leadership", []))
            result = {
                "board":             board,
                "executives":        execs + senior,
                "senior_leadership": senior,
            }
            result = _strip_hallucinations(result, grounded_text)

        logger.info(
            "Gemini Phase B for '%s': %d board, %d execs (%d senior)",
            company_name,
            len(result.get("board", [])),
            len(result.get("executives", [])),
            len(result.get("senior_leadership", [])),
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning("Gemini Phase B JSON parse error for '%s': %s", company_name, exc)
    except Exception as exc:
        logger.warning("Gemini Phase B failed for '%s': %s", company_name, exc)

    return {}


# ─────────────────────────────────────────────────────────────────────────────
# CACHE + PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

_LEADERSHIP_CACHE: dict[str, dict] = {}


def llm_fetch_leadership(company_name: str, domain: str = "") -> dict:
    """
    Fetch Board of Directors and Executive Management for a company.

    Pipeline:
      1. Gemini 2.0 Flash with Google Search grounding (primary).
         Phase A: two grounded queries research BOD + exec team via live Google Search.
         Phase B: Gemini synthesises grounded text into structured JSON.
         Requires GEMINI_API_KEY env var.

      2. Wikipedia + Claude Haiku (fallback when GEMINI_API_KEY is absent or
         Gemini returns no results).
         Requires ANTHROPIC_API_KEY env var.

    Results are cached in-process (keyed on company_name + domain).

    Returns:
        {
            "board":      [{"name": str, "title": str, ...}, ...],
            "executives": [{"name": str, "title": str, ...}, ...],
            "_source":    "web" | "none",
        }
    """
    if not company_name or len(company_name.strip()) < 3:
        return {"board": [], "executives": []}

    cache_key = f"{company_name.strip().lower()}|{(domain or '').strip().lower()}"
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    gemini_key    = os.environ.get("GEMINI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # ── Step 1: Gemini primary (Google Search grounding + JSON synthesis) ─────
    if gemini_key:
        logger.info("Step 1 Gemini for '%s'", company_name)
        result = _gemini_fetch_leadership(company_name, domain, gemini_key)
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _LEADERSHIP_CACHE[cache_key] = result
            return result
        logger.info("Gemini returned no leaders for '%s' — falling back to Wikipedia", company_name)
    else:
        logger.debug("GEMINI_API_KEY not set — skipping Gemini for '%s'", company_name)

    # ── Step 2: Wikipedia + Claude Haiku fallback ─────────────────────────────
    if not anthropic_key:
        logger.debug(
            "Leadership skipped for '%s': no GEMINI_API_KEY and no ANTHROPIC_API_KEY",
            company_name,
        )
        return {"board": [], "executives": [], "_source": "none"}

    wiki_text = _scrape_wikipedia(company_name)
    if wiki_text:
        logger.info("Step 2 Wikipedia+Claude for '%s': %d chars", company_name, len(wiki_text))
        user_msg = f"Company: {company_name}\n\n[Wikipedia excerpt]\n{wiki_text}"
        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=user_msg,
            label=f"{company_name} [wiki]",
            source_text=wiki_text,
        )
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _LEADERSHIP_CACHE[cache_key] = result
            return result

    # ── No result — don't cache so next upload can retry ─────────────────────
    logger.info("No leaders found for '%s' — returning empty (not cached)", company_name)
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
        # Do NOT apply _is_retired to board members — their title describes their
        # external career (e.g. "Retired CEO, SunTrust Banks"). They are active
        # directors at the company being researched.
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
            def _enrich_items(items: list, check_retired: bool = True) -> list:
                out = []
                for b in items:
                    if not isinstance(b, dict):
                        continue
                    name  = str(b.get("name", "") or "").strip()
                    title = str(b.get("title", "") or b.get("designation", "") or "").strip()
                    if not name or not title or len(name.split()) < 2:
                        continue
                    # Board members' titles describe their OTHER career
                    # (e.g. "Retired CEO, SunTrust Banks") — they are ACTIVE
                    # WF directors. Only apply _is_retired to executives where
                    # "Former CEO" means they left the company being researched.
                    if check_retired and _is_retired(name, title):
                        continue
                    entry: dict = {"name": name, "title": title}
                    for k in ("director_type", "committees", "function",
                              "scope", "function_or_bu", "linkedin_url",
                              "confidence"):
                        if b.get(k):
                            entry[k] = b[k]
                    out.append(entry)
                return out

            board   = _enrich_items(data.get("board",            []), check_retired=False)
            execs   = _enrich_items(data.get("executives",        []), check_retired=True)
            senior  = _enrich_items(data.get("senior_leadership", []), check_retired=True)
            result  = {
                "board":             board,
                "executives":        execs + senior,   # senior goes into EM panel
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


def _clean_list(raw: list, is_board: bool = False) -> list[dict]:
    """Validate, normalise, and de-retire a list of {name, title} dicts.
    Drops former / retired / emeritus / ex- executives — only current
    office-holders should appear in the org structure.

    is_board=True skips the _is_retired check: board members' titles describe
    their external career (e.g. "Retired CEO, SunTrust Banks") — they are
    active directors at the company being researched.
    """
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name  = str(item.get("name",  "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        if not name or not title or len(name.split()) < 2:
            continue
        if not is_board and _is_retired(name, title):
            logger.debug("Skipping retired/former executive: %s — %s", name, title)
            continue
        entry: dict = {"name": name, "title": title}
        for k in ("linkedin_url", "confidence", "director_type", "committees",
                  "function", "scope", "function_or_bu"):
            if item.get(k):
                entry[k] = item[k]
        out.append(entry)
    return out
