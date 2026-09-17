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
# HARVEST BUDGETS
#
# Leadership enrichment runs in a FastAPI background task (see api_server's
# _run_enrichment), not on the upload request path, so these are sized for
# recall — missing a director listed on the company site is far worse than
# spending another few seconds crawling.
# ─────────────────────────────────────────────────────────────────────────────

_MAX_PAGES            = int(os.environ.get("ORGANOGRAM_LEADERSHIP_MAX_PAGES",  "30"))
_MAX_HARVEST_CHARS    = int(os.environ.get("ORGANOGRAM_LEADERSHIP_MAX_CHARS","110000"))
_PER_PAGE_CHARS       = 12_000   # per-page visible-text cap fed to the LLM
_SYNTHESIS_CHUNK      = 18_000   # chars per Phase B synthesis call
_MAX_SYNTHESIS_CHUNKS = 6        # hard ceiling on Phase B calls per company
_MAX_BIO_LINKS        = 40       # bio/detail pages followed per company
_HARVEST_DEADLINE_S   = int(os.environ.get("ORGANOGRAM_LEADERSHIP_DEADLINE_S", "180"))

# ─────────────────────────────────────────────────────────────────────────────
# HTML UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT DATA EXTRACTION  (Next.js / React / Angular embedded JSON)
# ─────────────────────────────────────────────────────────────────────────────

_NAME_KEYS = (
    "name", "fullname", "personname", "displayname", "fullName",
    "membername", "employeename", "title_name", "person",
)
_TITLE_KEYS = (
    "title", "jobtitle", "position", "role", "designation", "jobrole",
    "positiontitle", "subtitle", "job", "function", "post",
)


def _walk_json_for_people(obj: object, depth: int = 0) -> list[str]:
    """
    Recursively walk a parsed JSON object looking for name/title pairs.

    Key lookup is case-insensitive and covers the many spellings CMSes use
    (jobTitle, positionTitle, designation, subtitle …) — a leadership grid
    rendered from JSON is only useful to us if we recognise its field names.
    Walks to depth 8; deeply-nested page props on Next.js sites routinely bury
    the people array 6-7 levels down.
    """
    if depth > 8:
        return []
    results: list[str] = []
    if isinstance(obj, dict):
        lowered = {str(k).lower(): v for k, v in obj.items()}

        def _first(keys: tuple[str, ...]) -> str:
            for k in keys:
                v = lowered.get(k.lower())
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        name  = _first(_NAME_KEYS)
        title = _first(_TITLE_KEYS)
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
        r'boardMembers?|boardOfDirectors|directors?|executives?|officers?'
        r'|leadership(?:Team)?|seniorLeadership|executiveTeam|executiveCommittee'
        r'|teamMembers?|team|managementTeam|management|committeeMembers?'
        r'|people|persons?|members?|profiles?|bios?|staff'
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
    return "\n".join(lines[:250])


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
        # Flatten to list of dicts, expanding @graph containers (very common —
        # WordPress/Yoast and most enterprise CMSes wrap everything in @graph,
        # which the old top-level-only scan walked straight past).
        items = obj if isinstance(obj, list) else [obj]
        expanded: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            expanded.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                expanded.extend(g for g in graph if isinstance(g, dict))

        for item in expanded:
            # Person schemas
            if item.get("@type") in ("Person", "Employee"):
                name  = item.get("name", "")
                title = item.get("jobTitle", "")
                if name and title:
                    snippets.append(f"{name} — {title}")
            # Organization person arrays — member/employee/founder/director all
            # carry leadership on real corporate sites.
            for key in ("member", "members", "employee", "employees",
                        "founder", "founders", "director", "alumni"):
                people = item.get(key)
                if isinstance(people, dict):
                    people = [people]
                if not isinstance(people, list):
                    continue
                for m in people:
                    if not isinstance(m, dict):
                        continue
                    # OrganizationRole indirection: {"@type":"OrganizationRole",
                    # "roleName":"Chair","member":{Person}}
                    role_name = str(m.get("roleName") or "").strip()
                    inner = m.get("member") if isinstance(m.get("member"), dict) else m
                    name  = str(inner.get("name", "") or "").strip()
                    title = str(inner.get("jobTitle", "") or role_name or "").strip()
                    if name and title:
                        snippets.append(f"{name} — {title}")

    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for s in snippets:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# ATTRIBUTE-ONLY SIGNALS  (names that never survive tag stripping)
# ─────────────────────────────────────────────────────────────────────────────

_PERSONISH_RE = re.compile(
    r"^[A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){1,4}$"
)
_ATTR_NOISE_RE = re.compile(
    r"\b(logo|icon|banner|photo of|image|picture|thumbnail|arrow|search|menu"
    r"|close|play|video|download|linkedin|twitter|facebook)\b",
    re.IGNORECASE,
)


def _extract_attr_names(html: str) -> str:
    """
    Pull person names out of img alt / aria-label / title attributes.

    Photo-grid leadership pages routinely put the person's name ONLY in the
    image alt text ("Jane Okonjo, Chief Financial Officer") — _strip_html drops
    every tag, so those people were previously invisible to the extractor.
    Returns one candidate per line, or "".
    """
    hits: list[str] = []
    for m in re.finditer(
        r'<(?:img|a|div|span)[^>]+(?:alt|aria-label|title)=["\']([^"\']{5,140})["\']',
        html, flags=re.IGNORECASE,
    ):
        val = re.sub(r"\s+", " ", m.group(1)).strip()
        if not val or _ATTR_NOISE_RE.search(val):
            continue
        # "Name, Title" / "Name - Title" / bare "Name"
        head = re.split(r"\s*[,–—|]\s*|\s+-\s+", val, maxsplit=1)[0].strip()
        if _PERSONISH_RE.match(head):
            hits.append(val)

    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return "\n".join(out[:150])


def _extract_page_signals(html: str, url: str) -> str:
    """
    Run every extractor over one page and return a labelled block for the LLM.

    Returns "" when the page yields nothing usable. Structured data is listed
    before the visible text so it survives any downstream truncation.
    """
    parts: list[str] = []
    json_ld   = _extract_json_ld(html)
    js_data   = _extract_js_data(html)
    attr_data = _extract_attr_names(html)
    text      = _strip_html(html)

    if json_ld:
        parts.append(f"[Structured Data from {url}]\n{json_ld}")
    if js_data:
        parts.append(f"[JS Data from {url}]\n{js_data}")
    if attr_data:
        parts.append(f"[Image/label names from {url}]\n{attr_data}")
    if text and len(text) > 200:
        parts.append(f"[Page: {url}]\n{text[:_PER_PAGE_CHARS]}")
    return "\n".join(parts)


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
_GEMINI_MODEL    = "gemini-2.5-flash"   # grounding via googleSearch tool
# URL-path keywords that identify leadership / governance pages in search results
_GEMINI_URL_SIGNAL = {
    # English
    "board", "governance", "leadership", "director", "executive",
    "management", "investor", "officers", "oversight", "about",
    "team", "people", "who-we-are", "whoweare", "our-company", "company",
    "committee", "senior", "bio", "profile", "bod", "trustees", "council",
    # Non-English corporate-governance conventions
    "vorstand", "aufsichtsrat", "geschaeftsleitung", "direccion", "directorio",
    "junta", "consejo", "conseil", "administration", "dirigeants",
    "direzione", "consiglio", "bestuur", "styrelse", "ledelse", "yonetim",
}


# Common leadership/governance URL paths tried when a domain is known.
# Covers English, Spanish, and major regional URL conventions.
_LEADERSHIP_FETCH_PATHS = [
    # ── Generic English paths ──────────────────────────────────────────────────
    "/leadership",
    "/about/leadership",
    "/about-us/leadership",
    "/about/board-of-directors",
    "/governance/board-of-directors",
    "/governance",
    "/corporate-governance/board-of-directors",
    "/corporate-governance",
    "/about-us/governance",
    "/about/corporate/governance",
    "/board-of-directors",
    "/investors/governance/board-of-directors",
    "/investor-relations/governance/board-of-directors",
    "/about/management",
    "/executive-team",
    # ── Common alternates missed by the original list ─────────────────────────
    "/leadership-team",
    "/our-leadership",
    "/about/our-leadership",
    "/company/leadership",
    "/management-team",
    "/senior-management",
    "/about-us/board-of-directors",
    "/about-us/management",
    "/about/executive-committee",
    "/executive-committee",
    "/our-team",
    "/team",
    "/about/team",
    "/about/people",
    "/our-people",
    "/who-we-are",
    "/about-us/who-we-are",
    "/investors/corporate-governance",
    "/investors/governance",
    "/company/board-of-directors",
    # ── en/ locale prefix (Latin American, European companies) ─────────────────
    "/en/our-company/ethics-and-corporate-governance/",
    "/en/our-company/governance/",
    "/en/our-company/board-of-directors/",
    "/en/our-company/leadership/",
    "/en/sustainability/stakeholders/board-of-directors/",
    "/en/about/leadership",
    "/en/about-us/leadership",
    "/en/about/governance",
    "/en/governance/board-of-directors",
    "/en/corporate-governance",
    # ── IR subdomains are tried separately (see _ir_leadership_urls) ───────────
]

_IR_SUBDOMAINS   = ["investor", "ir", "investors"]
_IR_GOV_PATHS    = [
    "/governance/board-of-directors",
    "/governance/board-of-directors/default.aspx",
    "/governance",
    "/corporate-governance/board-of-directors",
    "/corporate-governance",
    "/en/governance/board-of-directors",
]


_FETCH_HEADERS = {
    "User-Agent": _SEARCH_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en,es;q=0.8",
}

# Anchor hrefs worth following from a leadership index page down to a bio page.
_BIO_LINK_SIGNAL = re.compile(
    r"/(?:bio|bios|biography|profile|profiles|people|person|team|our-team"
    r"|leadership|leader|management|executive|executives|officer|officers"
    r"|board|director|directors|governance|member|members)(?:[/-]|$)",
    re.IGNORECASE,
)


# Pages that describe the past, not the present. A 2019 press release names
# its executives as current, so nothing in the text marks them as former —
# the URL is the only reliable signal that the page is dated.
_ARCHIVAL_URL_RE = re.compile(
    r"/(?:news|newsroom|press|press-releases?|media|media-centre|media-center"
    r"|blog|stories|articles?|archive|archives|events?|speeches?"
    r"|annual-reports?|sec-filings?|earnings|quarterly-earnings"
    r"|19\d\d|20\d\d)(?:[/-]|$)",
    re.IGNORECASE,
)


def _is_archival(url: str) -> bool:
    """True when a URL's path marks it as dated/archived content."""
    from urllib.parse import urlparse
    return bool(_ARCHIVAL_URL_RE.search(urlparse(url).path))


class _Harvester:
    """
    Budgeted page collector shared by the static-path crawl and the
    grounding-URL fetch, so both paths honour ONE page/char budget and run the
    same extraction (structured data + embedded JSON + attribute names + text).

    Previously each path kept its own smaller budget and its own copy of the
    extraction logic, which meant a long leadership page could exhaust the
    budget on boilerplate before the real board page was ever fetched.
    """

    def __init__(self) -> None:
        import time
        self.blocks: list[str] = []
        self.seen: set[str] = set()
        self.bio_queue: list[str] = []
        self.canonical_blocks: list[str] = []
        self.fetched: list[str] = []          # diagnostics: what was actually read
        self.canonical_urls: list[str] = []
        self.chars = 0
        self.deadline = time.monotonic() + _HARVEST_DEADLINE_S
        self._expired = False
        self.dead_hosts: dict[str, int] = {}   # host → consecutive connect failures

    @property
    def exhausted(self) -> bool:
        import time
        if self.chars >= _MAX_HARVEST_CHARS or len(self.blocks) >= _MAX_PAGES:
            return True
        # Wall-clock stop: an unresponsive site can burn the full timeout on
        # every guessed path, and this runs inside a request-scoped background
        # task — it must not run unbounded.
        if time.monotonic() >= self.deadline:
            if not self._expired:
                self._expired = True
                logger.info("Harvest deadline (%ds) reached after %d pages",
                            _HARVEST_DEADLINE_S, len(self.blocks))
            return True
        return False

    def fetch(self, url: str, follow_bios: bool = False) -> bool:
        """
        Fetch one URL and harvest it. True when it yielded content.

        Bio links are QUEUED, not followed inline: a single leadership page can
        link 20 bios, and draining them immediately would exhaust the budget
        before the board-of-directors page is ever requested. Index pages are
        fetched first; drain_bios() spends whatever is left.
        """
        if self.exhausted or url in self.seen:
            return False
        try:
            import httpx
            from urllib.parse import urlparse
        except ImportError:
            return False

        host = urlparse(url).netloc.lower()
        # A host that never connects (no www record, no IR subdomain) would
        # otherwise cost a full timeout on every remaining guessed path.
        if self.dead_hosts.get(host, 0) >= 2:
            return False

        # Dated pages name the executives of their own era as if current.
        if _is_archival(url):
            logger.debug("Skipping archival URL: %s", url)
            return False

        self.seen.add(url)
        try:
            resp = httpx.get(url, headers=_FETCH_HEADERS, timeout=6,
                             follow_redirects=True)
        except Exception as exc:
            self.dead_hosts[host] = self.dead_hosts.get(host, 0) + 1
            logger.debug("Fetch %s: %s", url, exc)
            return False
        self.dead_hosts.pop(host, None)
        if resp.status_code != 200:
            return False

        html = resp.text
        block = _extract_page_signals(html, url)
        # A JS shell with no structured data is genuinely empty — but one that
        # ships __NEXT_DATA__ or JSON-LD carries the whole leadership roster,
        # and the old code discarded those pages before extracting anything.
        if not block:
            return False
        if _is_js_shell(html) and "[Page:" in block and "[JS Data" not in block \
                and "[Structured Data" not in block:
            return False

        self.blocks.append(block)
        # Pages reached as a leadership INDEX (a known governance path, a
        # sitemap leadership URL, a Gemini grounding hit) are the company's own
        # current-roster pages. Names appearing there are corroborated as
        # current; names found only on deeper pages are not.
        self.fetched.append(url)
        if follow_bios:
            self.canonical_blocks.append(block)
            self.canonical_urls.append(url)
        self.chars += len(block)
        logger.info("Harvested %s (%d chars, %d/%d pages)",
                    url, len(block), len(self.blocks), _MAX_PAGES)

        if follow_bios:
            for bio_url in _bio_links(html, url):
                if bio_url not in self.seen and bio_url not in self.bio_queue:
                    self.bio_queue.append(bio_url)
        return True

    def drain_bios(self) -> None:
        """Fetch queued bio/detail pages until the budget runs out."""
        fetched = 0
        while self.bio_queue and not self.exhausted and fetched < _MAX_BIO_LINKS:
            if self.fetch(self.bio_queue.pop(0)):
                fetched += 1
        if fetched:
            logger.info("Followed %d bio/detail pages", fetched)

    def text(self) -> str:
        return "\n\n".join(self.blocks)

    def canonical_text(self) -> str:
        """Text from leadership index pages only — the current-roster evidence."""
        return "\n\n".join(self.canonical_blocks)


def _bio_links(html: str, base_url: str) -> list[str]:
    """
    Same-host links from a leadership page that look like individual bio pages.

    Card-grid leadership pages often show only a photo and a name, with the
    title living on the person's own bio page — following these is the
    difference between capturing 4 executives and capturing all 22.
    """
    from urllib.parse import urljoin, urlparse

    base = urlparse(base_url)
    base_host = base.netloc.lower()
    # Title-case nav labels ("Our History", "Core Values") are shaped exactly
    # like names, so only mine name-shaped anchors from pages that are
    # themselves leadership/board/team pages. Elsewhere, the path must say so.
    # …and only when the link sits UNDER that page: a bio lives at
    # /leadership/jane-okonjo, whereas the sidebar's "Press Releases" points off
    # to /news-events/. Without this, governance pages drag in their whole nav.
    allow_person_anchors = bool(_BIO_LINK_SIGNAL.search(base.path))
    base_dir = base.path.rstrip("/")
    out: list[str] = []
    seen: set[str] = set()

    for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\']([^>]*)>(.*?)</a>',
                         html, flags=re.DOTALL | re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        anchor = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(3))).strip()

        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != base_host:
            continue
        if re.search(r"\.(?:pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|mp4)$",
                     parsed.path, re.IGNORECASE):
            continue

        # Either the path looks like a bio/team route, or the anchor is a
        # person's name whose slug appears in the URL (the common "Jane Okonjo"
        # → /leadership/jane-okonjo card link). The slug check matters: plain
        # title-case nav labels ("Our History", "Core Values") also look like
        # names, and without it they burn the fetch budget on boilerplate.
        if _BIO_LINK_SIGNAL.search(parsed.path) or (
            allow_person_anchors
            and parsed.path.startswith(f"{base_dir}/")
            and _PERSONISH_RE.match(anchor)
            and not _NAV_WORD_RE.search(anchor)
            and _slug_matches_name(parsed.path, anchor)
        ):
            if absolute not in seen and not _is_archival(absolute):
                seen.add(absolute)
                out.append(absolute)
    return out


def _slug_matches_name(path: str, anchor: str) -> bool:
    """True when the URL's last segment is built from the anchor's name words."""
    slug = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if not slug or "-" not in slug:
        return False
    words = [w for w in re.sub(r"[^a-z ]", " ", _ascii_fold(anchor.lower())).split()
             if len(w) > 1]
    if len(words) < 2:
        return False
    return sum(1 for w in words if w in slug) >= 2


# Words that never appear in a person's name but are common in Title Case nav
# labels — a cheap second guard on the name-shaped-anchor heuristic.
_NAV_WORD_RE = re.compile(
    r"\b(?:our|the|and|of|us|we|home|about|history|values?|vision|mission|code"
    r"|conduct|policy|policies|principles?|report|reports?|news|careers?|contact"
    r"|overview|strategy|sustainability|responsibility|explore|learn|more|view"
    r"|all|read|team|group|company|global|annual|investor|investors|privacy"
    r"|terms|cookies?|accessibility|sitemap|search|login|share)\b",
    re.IGNORECASE,
)

_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

# Sitemaps list every page on the site, so filtering them needs STRONG signals
# only. The looser _GEMINI_URL_SIGNAL set (which includes "about", "company",
# "investor") matches most corporate URLs and would fill the budget with
# boilerplate like /about/our-history before any board page was fetched.
_SITEMAP_URL_SIGNAL = {
    "board", "governance", "leadership", "director", "executive", "officers",
    "management", "committee", "trustees", "our-people", "our-team",
    "management-team", "executive-team", "leadership-team", "who-we-are",
    "vorstand", "aufsichtsrat", "geschaeftsleitung", "direccion", "directorio",
    "junta", "consejo", "conseil", "dirigeants", "consiglio", "bestuur",
    "styrelse", "ledelse", "yonetim",
}


def _sitemap_leadership_urls(domain: str, limit: int = 12) -> list[str]:
    """
    Mine the site's sitemap.xml for leadership/governance URLs.

    Guessed paths only find conventional URL layouts; the sitemap finds the
    real ones (/en-gb/corporate/our-board, /group/gestion/comite-executif …)
    on sites that don't follow any convention.
    """
    try:
        import httpx
    except ImportError:
        return []
    if not domain:
        return []

    roots = [f"https://www.{domain}/sitemap.xml", f"https://{domain}/sitemap.xml"]
    found: list[str] = []
    seen: set[str] = set()
    child_budget = 3

    def _scan(url: str, allow_children: bool) -> None:
        nonlocal child_budget
        try:
            r = httpx.get(url, headers=_FETCH_HEADERS, timeout=6, follow_redirects=True)
        except Exception:
            return
        if r.status_code != 200 or "<loc" not in r.text.lower():
            return
        locs = _SITEMAP_LOC_RE.findall(r.text)
        child_sitemaps: list[str] = []
        for loc in locs:
            low = loc.lower()
            if low.endswith(".xml") or "sitemap" in low.rsplit("/", 1)[-1]:
                child_sitemaps.append(loc)
                continue
            if len(found) >= limit or loc in seen:
                continue
            if any(kw in low for kw in _SITEMAP_URL_SIGNAL):
                seen.add(loc)
                found.append(loc)
        if allow_children:
            for child in child_sitemaps:
                if child_budget <= 0 or len(found) >= limit:
                    break
                child_budget -= 1
                _scan(child, allow_children=False)

    for root in roots:
        if found:
            break
        _scan(root, allow_children=True)

    if found:
        logger.info("Sitemap for '%s': %d leadership URLs", domain, len(found))
    return found[:limit]


def _fetch_static_leadership(domain: str, harvester: "_Harvester | None" = None) -> str:
    """
    Crawl the company's own site for leadership/governance pages.

    Order (most-specific first, so the budget is spent where the roster is):
      1. Known leadership/governance paths on www.{domain} and {domain}
      2. Leadership URLs mined from sitemap.xml
      3. IR subdomains (investor./ir./investors.)

    Leadership pages also have their bio/detail links followed one level deep.
    Returns the combined harvest block, or "".
    """
    if not domain:
        return ""

    h = harvester or _Harvester()

    # 1. Known leadership paths on www and plain domain
    for path in _LEADERSHIP_FETCH_PATHS:
        if h.exhausted:
            break
        if not h.fetch(f"https://www.{domain}{path}", follow_bios=True):
            h.fetch(f"https://{domain}{path}", follow_bios=True)

    # 2. Sitemap-discovered leadership URLs
    if not h.exhausted:
        for url in _sitemap_leadership_urls(domain):
            if h.exhausted:
                break
            h.fetch(url, follow_bios=True)

    # 3. IR subdomains (investor.company.com etc.)
    for sub in _IR_SUBDOMAINS:
        for path in _IR_GOV_PATHS:
            if h.exhausted:
                break
            h.fetch(f"https://{sub}.{domain}{path}", follow_bios=True)

    # 4. Individual bio/detail pages, only once every index page has been tried
    if harvester is None:
        h.drain_bios()
        result = h.text()
        if result:
            logger.info("Direct fetch for '%s': %d chars across %d pages",
                        domain, len(result), len(h.blocks))
        return result
    return ""   # shared harvester — caller owns draining and reading


def _gemini_discover_leadership_urls(
    company_name: str,
    api_key: str,
    domain: str = "",
) -> tuple[list[str], str]:
    """
    Use Gemini 2.0 Flash with Google Search grounding to find Board of Directors
    and executive leadership page URLs for a company.

    Three targeted queries are submitted:
      1. Board of Directors (site-constrained when domain is known)
      2. Executive leadership team
      3. Governance page search (when domain is known, constrained to site)

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

    if domain:
        # STRICT SITE-ONLY strategy: four targeted queries locked to the company domain.
        # Gemini grounding will only surface pages from site:{domain} and its subdomains.
        queries = [
            # Query 1: leadership / executive / officers pages
            f'site:{domain} "leadership" OR "executive" OR "officers"',
            # Query 2: board / governance pages
            f'site:{domain} "board of directors" OR "governance" OR "directors"',
            # Query 3: about-us / management / team pages (catches smaller company sites)
            f'site:{domain} "about us" OR "management" OR "team"',
            # Query 4: investor-relations subdomains (ir.domain, investors.domain)
            f'site:investors.{domain} OR site:ir.{domain}',
        ]
    else:
        # No domain — fall back to open-web queries with native-language variants
        queries = [
            (f"Who are the current Board of Directors of {company_name}? "
             f"List each member's full name and title from the official corporate website."),
            (f"Who are the executive leadership team members of {company_name}? "
             f"List each executive's full name and title from the official company website."),
            # Spanish / French / German fallback for non-English companies
            (f"{company_name} junta directiva directores ejecutivos OR "
             f"{company_name} conseil d'administration dirigeants OR "
             f"{company_name} Vorstand Aufsichtsrat"),
        ]

    all_urls:  list[str] = []
    all_texts: list[str] = []
    seen: set[str] = set()

    # Try both REST tool name variants — stable models use camelCase "googleSearch",
    # earlier experimental models used snake_case "google_search".
    # We detect which works on the first call and reuse it for subsequent queries.
    _tool_key: str | None = None  # "google_search" | "googleSearch" — discovered at runtime

    def _try_tool(key: str, query: str):
        return httpx.post(
            f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent",
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": query}]}],
                "tools":    [{key: {}}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
            },
            timeout=90,
        )

    for query in queries:
        try:
            resp = None
            if _tool_key:
                resp = _try_tool(_tool_key, query)
            else:
                # Auto-detect tool key across Gemini model families:
                # gemini-2.5+: "googleSearch" (camelCase)
                # gemini-2.0:  "google_search" (snake_case, now deprecated)
                # gemini-1.5:  "google_search_retrieval"
                for candidate_key in ("googleSearch", "google_search", "google_search_retrieval"):
                    r = _try_tool(candidate_key, query)
                    if r.is_success:
                        _tool_key = candidate_key
                        logger.info("Gemini search tool key detected: '%s'", _tool_key)
                        resp = r
                        break
                    elif r.status_code not in (400, 422):
                        # Non-validation error (quota, auth) — no point trying other key
                        logger.warning("Gemini search %s=%d for '%s': %s",
                                       candidate_key, r.status_code,
                                       company_name, r.text[:300])
                        resp = r
                        break
                    else:
                        logger.debug("Gemini tool key '%s' rejected (%d): %s",
                                     candidate_key, r.status_code, r.text[:200])
                if resp is None:
                    continue

            if not resp.is_success:
                logger.warning("Gemini search %d for '%s': %s",
                               resp.status_code, company_name, resp.text[:300])
                continue

            data = resp.json()
            um = data.get("usageMetadata", {})
            from usage_tracker import record_usage
            record_usage("gemini", _GEMINI_MODEL,
                          um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))
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
                        if (any(kw in url.lower() for kw in _GEMINI_URL_SIGNAL)
                                and not _is_archival(url)):
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
        um = data.get("usageMetadata", {})
        from usage_tracker import record_usage
        record_usage("gemini", _GEMINI_MODEL,
                      um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))
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
    discovered_urls, grounded_text = _gemini_discover_leadership_urls(
        company_name, api_key, domain=domain
    )

    # ── Phase A supplemental: fetch discovered URLs + known static paths ──────
    # Step 2 of the site-scan strategy: navigate to the top pages Gemini found
    # and extract any leadership content not captured in the grounded text.
    harvester = _Harvester()
    try:
        # The company's OWN governance paths go first. They are the
        # authoritative current roster, and when they ran second a slow site
        # could spend the page budget and the wall-clock deadline on grounding
        # URLs before /governance/board-of-directors was ever requested —
        # which is how three sitting 3M directors went missing from live runs
        # while being present in a local crawl of the same domain.
        if domain:
            _fetch_static_leadership(domain, harvester=harvester)

        # Then the URLs Gemini grounding discovered, following one level of
        # bio links from each — these cover sites whose paths we can't guess.
        for u in discovered_urls[:15]:
            if harvester.exhausted:
                break
            harvester.fetch(u, follow_bios=True)

        # Bios last, so every index page is captured before drilling into people
        harvester.drain_bios()
        discovered_page_text = harvester.text()
        canonical_text = harvester.canonical_text()
    except Exception as exc:
        logger.warning("Page harvest failed for '%s': %s", company_name, exc)
        discovered_page_text = harvester.text()
        canonical_text = harvester.canonical_text()

    combined_text = "\n\n[Direct website content]\n".join(
        t for t in (grounded_text, discovered_page_text) if t
    )

    # Built once so every exit path — including the failures — is observable.
    harvest_info = {
        "pages":           len(harvester.fetched),
        "canonical":       len(harvester.canonical_urls),
        "chars":           len(discovered_page_text),
        "canonical_chars": len(canonical_text),
        "grounded_chars":  len(grounded_text),
        "deadline_hit":    harvester._expired,
        "urls":            harvester.fetched[:40],
    }

    if not combined_text:
        # Nothing fetched AND nothing grounded: usually the site refused us
        # (WAF/rate limit) rather than a bug. Without this the caller saw a
        # bare {} and had no way to tell those apart.
        logger.warning("Gemini Phase A returned no content for '%s' (harvest: %s)",
                       company_name, harvest_info)
        return {"board": [], "executives": [], "_harvest": harvest_info,
                "_error": "no_content_harvested"}

    logger.info(
        "Gemini Phase A for '%s': %d chars grounded + %d chars scraped",
        company_name, len(grounded_text), len(discovered_page_text),
    )

    # ── Phase B: Structured JSON synthesis with domain-lock guardrails ────────
    # The harvest is split into chunks and synthesised chunk-by-chunk, then
    # merged. A single call truncated to 22 KB silently dropped everyone who
    # appeared past that point — on a large board page that is most of the
    # roster — and a 4096-token cap could truncate the JSON mid-array, which
    # failed to parse and lost the whole extraction.
    chunks = _chunk_for_synthesis(combined_text)
    logger.info("Gemini Phase B for '%s': %d chunk(s) from %d chars",
                company_name, len(chunks), len(combined_text))

    partials: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        partial = _gemini_synthesize_chunk(
            company_name, domain, chunk, api_key, idx, len(chunks)
        )
        if partial:
            partials.append(partial)

    # ── Corroboration pass over the canonical leadership pages ───────────────
    # Gemini's synthesis is not reliably exhaustive: on 3M it returned 11, 12
    # and 8 board members across otherwise identical runs, while every one of
    # those directors was present in the harvest the whole time. A second
    # model over the index pages costs little and the merge is a union, so a
    # name either model sees survives. Off via ORGANOGRAM_CORROBORATE=0.
    gemini_board = len(_merge_leadership(partials).get("board", [])) if partials else 0
    corroborated = _corroborate_canonical(company_name, canonical_text)
    corrob_board = len(_merge_leadership(corroborated).get("board", [])) if corroborated else 0
    partials.extend(corroborated)

    if not partials:
        logger.warning("No extraction succeeded for '%s' (harvest: %s)",
                       company_name, harvest_info)
        return {"board": [], "executives": [], "_harvest": harvest_info,
                "_error": "no_extraction"}

    result = _merge_leadership(partials)
    merged_board = len(result.get("board", []))
    result = _resolve_stale(result, canonical_text)
    harvest_info["stages"] = {
        "gemini_board":       gemini_board,
        "corroborated_board": corrob_board,
        "merged_board":       merged_board,
        "final_board":        len(result.get("board", [])),
        "corroboration_ran":  bool(corroborated),
    }
    _strip_internal_fields(result)
    # Diagnostics for /debug-leadership — consumers read board/executives only.
    result["_harvest"] = harvest_info
    logger.info(
        "Gemini Phase B merged for '%s': %d board, %d execs (%d senior)",
        company_name,
        len(result.get("board", [])),
        len(result.get("executives", [])),
        len(result.get("senior_leadership", [])),
    )
    return result


_CORROBORATE_MAX_CHUNKS = 3   # index pages only — bounds the added cost


def _corroborate_canonical(company_name: str, canonical_text: str) -> list[dict]:
    """
    Re-extract the company's own leadership index pages with Claude.

    Two models reading the same pages miss different people, and
    _merge_leadership is a union, so anyone either model names is kept. This
    runs only over the canonical index pages (never the whole harvest), which
    is where the full board roster lives, so the extra cost is bounded to a
    few Haiku calls per company.
    """
    if os.environ.get("ORGANOGRAM_CORROBORATE", "1") != "1":
        return []
    if not canonical_text or not os.environ.get("ANTHROPIC_API_KEY"):
        return []

    chunks = _chunk_for_synthesis(canonical_text)[:_CORROBORATE_MAX_CHUNKS]
    out: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=(
                f"Extract the Board of Directors and Executive Management for "
                f"{company_name}.\n"
                f"COMPLETENESS: this is excerpt {idx} of {len(chunks)} from the "
                f"company's own leadership pages. Extract EVERY person named — "
                f"a page listing 12 directors must yield 12 entries.\n"
                f"NO HALLUCINATIONS: only people explicitly named below.\n\n{chunk}"
            ),
            label=f"{company_name} corroborate {idx}/{len(chunks)}",
            source_text=chunk,
        )
        if result.get("board") or result.get("executives"):
            out.append(result)
    if out:
        logger.info("Corroboration for '%s': %d chunk(s) added %d board, %d execs",
                    company_name, len(out),
                    sum(len(r.get("board", [])) for r in out),
                    sum(len(r.get("executives", [])) for r in out))
    return out


def _chunk_for_synthesis(text: str) -> list[str]:
    """
    Split the harvest into synthesis-sized chunks on page boundaries.

    Splitting on the "[Page: url]" / "[Structured Data …]" markers keeps each
    person's name and title inside the same chunk; a blind character split
    would cut rosters in half and orphan titles from names.
    """
    if not text:
        return []
    if len(text) <= _SYNTHESIS_CHUNK:
        return [text]

    segments = re.split(r"\n(?=\[(?:Page|Structured Data|JS Data|Image/label)\b)", text)
    chunks: list[str] = []
    current = ""
    for seg in segments:
        # A single oversized segment is hard-split — nothing else to do.
        while len(seg) > _SYNTHESIS_CHUNK:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(seg[:_SYNTHESIS_CHUNK])
            seg = seg[_SYNTHESIS_CHUNK:]
        if len(current) + len(seg) + 1 > _SYNTHESIS_CHUNK and current:
            chunks.append(current)
            current = seg
        else:
            current = f"{current}\n{seg}" if current else seg
    if current:
        chunks.append(current)
    return chunks[:_MAX_SYNTHESIS_CHUNKS]


def _gemini_synthesize_chunk(
    company_name: str,
    domain: str,
    chunk: str,
    api_key: str,
    idx: int,
    total: int,
) -> dict:
    """Run one Phase B synthesis call over a single harvest chunk."""
    try:
        import httpx
    except ImportError:
        return {}

    domain_lock = (
        f"DOMAIN LOCK: Only include people whose names appear in the research text "
        f"sourced from {domain}. " if domain else ""
    )
    synthesis_prompt = (
        f"Extract the Board of Directors and Executive Management for {company_name}.\n\n"
        f"{domain_lock}"
        f"NO HALLUCINATIONS: Only include people explicitly named in the text below — "
        f"do NOT invent names or use training knowledge.\n"
        f"COMPLETENESS: This is excerpt {idx} of {total} from the company's website. "
        f"Extract EVERY person named in it — do not stop early, do not summarise, "
        f"do not return only the most senior people. A page listing 30 directors "
        f"must yield 30 entries.\n"
        f"DUPLICATION: If one person holds both a board title AND an executive title, "
        f"list them in BOTH the board array and the executives array.\n\n"
        f"[Research text — sourced from {domain or 'Google Search'} and company website]\n"
        f"{chunk}"
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
                    "maxOutputTokens": 8192,
                    "responseMimeType": "application/json",
                },
            },
            timeout=120,
        )
        if not resp.is_success:
            logger.warning(
                "Gemini Phase B chunk %d/%d for '%s': HTTP %s — %s",
                idx, total, company_name, resp.status_code, resp.text[:300],
            )
            return {}

        data = resp.json()
        um = data.get("usageMetadata", {})
        from usage_tracker import record_usage
        record_usage("gemini", _GEMINI_MODEL,
                      um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))
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

        parsed = _loads_lenient(raw)
        if parsed is None:
            logger.warning("Gemini Phase B chunk %d/%d for '%s': unparseable JSON",
                           idx, total, company_name)
            return {}

        # Handle rich schema (board_of_directors / executive_management keys).
        # Verify names against this chunk — the person was extracted from it.
        if "board_of_directors" in parsed or "executive_management" in parsed:
            result = _rich_to_flat(parsed, chunk)
        else:
            board  = _clean_list(parsed.get("board",            []), is_board=True)
            execs  = _clean_list(parsed.get("executives",        []))
            senior = _clean_list(parsed.get("senior_leadership", []))
            result = {
                "board":             board,
                "executives":        execs + senior,
                "senior_leadership": senior,
            }
            result = _strip_hallucinations(result, chunk)

        logger.info(
            "Gemini Phase B chunk %d/%d for '%s': %d board, %d execs",
            idx, total, company_name,
            len(result.get("board", [])), len(result.get("executives", [])),
        )
        return result

    except Exception as exc:
        logger.warning("Gemini Phase B chunk %d/%d failed for '%s': %s",
                       idx, total, company_name, exc)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# CACHE + PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

_LEADERSHIP_CACHE: dict[str, dict] = {}
_LEADERSHIP_CACHE_MAX = 500  # entries hold full board/exec lists — cap tighter
                             # than the industry cache to bound long-run memory.


def _cache_leadership(key: str, value: dict) -> None:
    if key not in _LEADERSHIP_CACHE and len(_LEADERSHIP_CACHE) >= _LEADERSHIP_CACHE_MAX:
        _LEADERSHIP_CACHE.pop(next(iter(_LEADERSHIP_CACHE)))  # drop oldest (dict insertion order)
    _LEADERSHIP_CACHE[key] = value


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

    gemini_result: dict = {}
    gemini_key    = os.environ.get("GEMINI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # ── Step 1: Gemini primary (Google Search grounding + JSON synthesis) ─────
    if gemini_key:
        logger.info("Step 1 Gemini for '%s'", company_name)
        result = _gemini_fetch_leadership(company_name, domain, gemini_key)
        gemini_result = result
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _cache_leadership(cache_key, result)
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
            _cache_leadership(cache_key, result)
            return result

    # ── No result — don't cache so next upload can retry ─────────────────────
    logger.info("No leaders found for '%s' — returning empty (not cached)", company_name)
    empty: dict = {"board": [], "executives": [], "_source": "none"}
    # Keep whatever the Gemini path recorded — otherwise an empty result
    # reaches the caller with no indication of whether the site refused us,
    # the extraction failed, or nothing was ever attempted.
    if isinstance(gemini_result, dict):
        for k in ("_harvest", "_error"):
            if gemini_result.get(k) is not None:
                empty[k] = gemini_result[k]
    return empty


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_HALLUCINATION_CHECK_MIN_CHARS = 200  # apply name verification even for short sources

def _ascii_fold(s: str) -> str:
    """Normalise accented chars to ASCII for fuzzy name matching."""
    import unicodedata
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _name_in_source(name: str, source_lower: str) -> bool:
    """
    Return True if the person's name is evidenced in the source text.

    Matching strategy (first match wins, both with and without accent folding):
    1. Full name exact match
    2. All significant name parts appear individually (handles middle initials)
    3. Surname alone (≥4 chars) — fallback for initials like "C. Scharf"
    Each strategy is tried with the original form then with ASCII-folded form
    so accented names (González / Gonzalez) match across encoding variants.
    """
    if not name or not source_lower:
        return False
    name_lower = name.lower()
    source_ascii = _ascii_fold(source_lower)
    name_ascii   = _ascii_fold(name_lower)

    def _check(n: str, src: str) -> bool:
        if n in src:
            return True
        words = [re.sub(r"[^\w]", "", p) for p in n.split()]
        words = [w for w in words if len(w) >= 2]
        if not words:
            return False
        if len(words) == 1:
            return words[0] in src

        # Given name and surname must appear NEAR each other, not merely both
        # somewhere in the page. Scattered-token matching let models invent a
        # director by keeping a real first name and initial and swapping the
        # surname ("Neil G. Mitchill" -> "Neil G. Bluhm"): every token existed
        # somewhere on the page, so the fabrication passed. Proximity still
        # accepts real variants — "Scharf, Charles" or "Charles W. Scharf".
        given, surname = words[0], words[-1]
        window = 60
        start = src.find(surname)
        while start != -1:
            around = src[max(0, start - window): start + len(surname) + window]
            if given in around:
                return True
            start = src.find(surname, start + 1)
        return False

    return _check(name_lower, source_lower) or _check(name_ascii, source_ascii)


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


def _loads_lenient(raw: str) -> dict | None:
    """
    Parse model JSON, salvaging output that was cut off by the token limit.

    A truncated response ends mid-object, so strict json.loads throws and the
    entire chunk's extraction is lost. Instead, trim back to the last complete
    entry and close the structure — keeping 25 of 30 recovered directors beats
    keeping none.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    def _closers(fragment: str) -> str:
        """Delimiters needed to close *fragment*, innermost first."""
        stack: list[str] = []
        in_string = False
        escaped = False
        for ch in fragment:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        return "".join(reversed(stack))

    cut = raw.rfind("}")
    while cut > 0:
        candidate = raw[: cut + 1]
        try:
            parsed = json.loads(candidate + _closers(candidate))
            if isinstance(parsed, dict):
                logger.info("Recovered truncated JSON (%d of %d chars)",
                            len(candidate), len(raw))
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        cut = raw.rfind("}", 0, cut)
    return None


_HONORIFIC_RE = re.compile(
    r"^(?:dr|mr|mrs|ms|miss|prof|professor|sir|dame|lord|lady|rev|hon|ing|eng)\.?\s+",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(
    r"\s+(?:jr|sr|ii|iii|iv|phd|ph\.d|mba|cpa|cfa|md|esq|obe|cbe|mbe)\.?$",
    re.IGNORECASE,
)
_NICKNAME_RE = re.compile(r'["“”\'(]([A-Za-z][\w\'-]*)["“”\')]')


def _name_aliases(name: str) -> set[str]:
    """
    All first+last keys a person's name could be written under.

    The same director arrives as "Dr. John Banovetz" on one page and "John
    Banovetz" on another, or as 'William "Bill" Brown' and plain "Bill Brown" —
    keying on the raw string puts them in the chart twice. Honorifics and
    suffixes are stripped, and a quoted/parenthesised nickname yields a second
    key with the nickname standing in for the given name.
    """
    original = str(name or "").strip()
    if not original:
        return set()

    # Nickname FIRST, on the unfolded string: _ascii_fold drops curly quotes
    # entirely, so folding before this turns William “Bill” Brown into the
    # three-word "William Bill Brown" and the nickname is lost.
    nickname = ""
    m = _NICKNAME_RE.search(original)
    if m:
        nickname = m.group(1)
        original = _NICKNAME_RE.sub(" ", original)

    raw = _ascii_fold(original).strip()
    cleaned = _SUFFIX_RE.sub("", _HONORIFIC_RE.sub("", raw)).lower()
    words = [w for w in re.sub(r"[^a-z ]", " ", cleaned).split() if len(w) > 1]
    if not words:
        return set()

    def _key(parts: list[str]) -> str:
        return f"{parts[0]} {parts[-1]}" if len(parts) >= 2 else parts[0]

    aliases = {_key(words)}
    if nickname:
        nick = re.sub(r"[^a-z]", "", nickname.lower())
        if nick:
            aliases.add(_key([nick] + words[1:]) if len(words) >= 2 else nick)
    return aliases


# Roles a company has exactly one of. Two names holding one of these means one
# of them has left — dated pages describe their own era's holder as current.
_SINGULAR_ROLES: list[tuple[str, re.Pattern]] = [
    ("ceo",      re.compile(r"\bchief executive officer\b|\bceo\b", re.I)),
    ("cfo",      re.compile(r"\bchief financial officer\b|\bcfo\b", re.I)),
    ("coo",      re.compile(r"\bchief operating officer\b|\bcoo\b", re.I)),
    ("cto",      re.compile(r"\bchief technology officer\b|\bcto\b", re.I)),
    ("cio",      re.compile(r"\bchief information (?:and digital )?officer\b|\bcio\b", re.I)),
    ("chro",     re.compile(r"\bchief human resources officer\b|\bchro\b", re.I)),
    ("cmo",      re.compile(r"\bchief marketing officer\b|\bcmo\b", re.I)),
    ("clo",      re.compile(r"\bgeneral counsel\b|\bchief legal officer\b", re.I)),
    ("chairman", re.compile(r"\bchair(?:man|woman|person)?\b(?!.*\bcommittee\b)", re.I)),
]

# A qualified title is not a singular role: "CEO, Australia" and "Group
# President, Consumer" legitimately coexist with their global counterparts.
_SCOPE_QUALIFIER_RE = re.compile(
    r"\b(?:deputy|vice|assistant|regional|country|division|divisional|group"
    r"|business unit|segment|interim|acting|designate|emerging|americas|europe"
    r"|emea|apac|asia|africa|pacific|latin america|north america|china|india"
    r"|japan|australia|brazil|canada|germany|france|uk|united states)\b",
    re.IGNORECASE,
)


def _role_keys(title: str) -> set[str]:
    """
    Every singular global role a title claims — empty when it claims none.

    A title can hold more than one ("Chairman and CEO" is both), and that
    matters: a former Executive Chairman only conflicts with the sitting CEO
    once the CEO's title is recognised as also claiming the chair.
    """
    t = str(title or "")
    if not t:
        return set()
    # Strip the rank prefix FIRST — "Executive Vice President, Chief
    # Information Officer" is the CIO, but the bare word "vice" in it would
    # otherwise read as a scope qualifier and disqualify the whole title.
    t = re.sub(r"^\s*(?:executive|senior|sr\.?|exec\.?)?\s*vice president(?:\s+and)?,?\s*",
               "", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*(?:e?vp|svp)(?:\s+and)?,?\s*", "", t, flags=re.IGNORECASE)
    if _SCOPE_QUALIFIER_RE.search(t):
        return set()
    return {role for role, pattern in _SINGULAR_ROLES if pattern.search(t)}


def _resolve_stale(result: dict, canonical_text: str) -> dict:
    """
    Drop office-holders contradicted by a current one.

    Recall-first: nobody is removed for being absent from the canonical pages.
    A person is dropped ONLY when someone else holds the same singular role and
    is better evidenced — named on the company's own leadership index page,
    corroborated across more excerpts, or described more specifically.

    This is what catches a former Executive Chairman or a predecessor CIO: the
    dated page that named them says nothing about their departure, so the only
    signal is that the live roster names someone else in that seat.
    """
    if not canonical_text:
        return result
    canon = canonical_text.lower()

    def _rank(entry: dict) -> tuple[int, int, int]:
        on_index = 1 if _name_in_source(str(entry.get("name", "")), canon) else 0
        return (on_index, int(entry.get("_mentions", 1)), len(str(entry.get("title", ""))))

    for section in ("board", "executives", "senior_leadership"):
        people = result.get(section) or []
        by_role: dict[str, list[dict]] = {}
        for person in people:
            for role in _role_keys(person.get("title", "")):
                by_role.setdefault(role, []).append(person)

        drop: set[int] = set()
        for role, holders in by_role.items():
            if len(holders) < 2:
                continue
            best = max(holders, key=_rank)
            for other in holders:
                if other is best or _rank(other) == _rank(best):
                    continue   # can't separate them — keep both rather than guess
                drop.add(id(other))
                logger.info("Stale: dropping %s (%s) — %s holds %s and is better evidenced",
                            other.get("name"), other.get("title"),
                            best.get("name"), role)
        if drop:
            result[section] = [p for p in people if id(p) not in drop]
    return result


def _strip_internal_fields(result: dict) -> None:
    """Remove merge bookkeeping so it never reaches the DAG or the frontend."""
    for section in ("board", "executives", "senior_leadership"):
        for person in result.get(section) or []:
            person.pop("_mentions", None)


def _prefix_match(aliases: set[str], index: dict[str, int]) -> int | None:
    """
    Find an already-seen person whose surname matches and whose given name is a
    shortening of this one — "Chris Goralski" is "Christian Goralski".

    Unquoted nicknames carry no marker to key on, so this is the only way to
    collapse them. Requires the shorter given name to be at least 3 characters
    and a true prefix, which keeps distinct people (Ana / Anabel would merge,
    but two leaders of one company sharing a surname and a name stem is far
    rarer than the same person written two ways).
    """
    for alias in aliases:
        parts = alias.split()
        if len(parts) != 2:
            continue
        given, surname = parts
        for known, slot in index.items():
            kparts = known.split()
            if len(kparts) != 2 or kparts[1] != surname:
                continue
            kgiven = kparts[0]
            if given == kgiven:
                return slot
            short, long_ = sorted((given, kgiven), key=len)
            if len(short) >= 3 and long_.startswith(short):
                return slot
    return None


def _merge_leadership(results: list[dict]) -> dict:
    """
    Union several per-chunk extractions into one, de-duplicated by person.

    Chunks overlap in coverage (the same CEO appears on the leadership page and
    on their own bio page), so the same person arrives several times with
    varying detail. Keep the richest version: the entry with the most populated
    fields, tie-broken by the longer (more specific) title.
    """
    def _score(entry: dict) -> tuple[int, int]:
        return (len([v for v in entry.values() if v]), len(str(entry.get("title", ""))))

    merged: dict = {}
    for section in ("board", "executives", "senior_leadership"):
        # alias → index into `picked`, so the same person reached by any of
        # their name forms collapses onto one entry.
        index: dict[str, int] = {}
        picked: list[dict] = []
        for result in results:
            for entry in result.get(section, []) or []:
                aliases = _name_aliases(str(entry.get("name", "")))
                if not aliases:
                    continue
                slot = next((index[a] for a in aliases if a in index), None)
                if slot is None:
                    slot = _prefix_match(aliases, index)
                if slot is None:
                    entry = dict(entry, _mentions=1)
                    picked.append(entry)
                    slot = len(picked) - 1
                else:
                    # How many excerpts named this person — corroboration, used
                    # to break ties when two people claim the same role.
                    seen_count = int(picked[slot].get("_mentions", 1)) + 1
                    if _score(entry) > _score(picked[slot]):
                        picked[slot] = dict(entry)
                    picked[slot]["_mentions"] = seen_count
                for a in aliases:
                    index[a] = slot
        merged[section] = picked

    # Preserve the passthrough fields the flat schema carries.
    for extra in ("dual_roles", "data_gaps"):
        values = [v for r in results for v in (r.get(extra) or [])]
        if values:
            merged[extra] = values
    return merged


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
        entry: dict = {"name": name, "title": _normalize_title(title, True)}
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
        entry: dict = {"name": name, "title": _normalize_title(title, False)}
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
        # claude-3-5-haiku-20241022 retired 2026-02-19 — no longer callable, removed.
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
        from usage_tracker import record_usage
        record_usage("anthropic", model_id,
                      response.usage.input_tokens, response.usage.output_tokens)
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model added them
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)

        data = _loads_lenient(raw)
        if data is None:
            raise json.JSONDecodeError("unparseable leadership JSON", raw[:200], 0)

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
                    entry: dict = {"name": name,
                                   "title": _normalize_title(title, not check_retired)}
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


_PLACEHOLDER_TITLES = {"unknown", "n/a", "na", "none", "-", "--", "not stated",
                       "not specified", "null", "tbd"}


def _normalize_title(title: str, is_board: bool) -> str:
    """
    Replace placeholder titles with a usable role label.

    Pages that list people as a photo grid often carry no title at all, and the
    model faithfully reports "unknown" — which then renders as a person's
    designation in the org chart. They belong in the chart, just not labelled
    "unknown".
    """
    if title.strip().lower().strip(".") in _PLACEHOLDER_TITLES:
        return "Board Member" if is_board else "Executive"
    return title


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
        entry: dict = {"name": name, "title": _normalize_title(title, is_board)}
        for k in ("linkedin_url", "confidence", "director_type", "committees",
                  "function", "scope", "function_or_bu"):
            if item.get(k):
                entry[k] = item[k]
        out.append(entry)
    return out
