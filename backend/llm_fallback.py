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

_MAX_PAGES            = int(os.environ.get("ORGANOGRAM_LEADERSHIP_MAX_PAGES",  "45"))
_MAX_HARVEST_CHARS    = int(os.environ.get("ORGANOGRAM_LEADERSHIP_MAX_CHARS","150000"))
_PER_PAGE_CHARS       = 12_000   # per-page visible-text cap fed to the LLM
_SYNTHESIS_CHUNK      = 18_000   # chars per Phase B synthesis call
_MAX_SYNTHESIS_CHUNKS = 8        # hard ceiling on Phase B calls per company
_MAX_BIO_LINKS        = 40       # bio/detail pages followed per company
_HARVEST_DEADLINE_S   = int(os.environ.get("ORGANOGRAM_LEADERSHIP_DEADLINE_S", "180"))
_INDEX_BUDGET_SHARE   = 0.55     # fraction of the char budget index pages may use

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
_ATTR_PAIR_RE = re.compile(
    r'\b(alt|aria-label|title|heading|subheading|eyebrow|imgalt|data-name'
    r'|data-title|data-person|label|caption|name)\s*=\s*["\']([^"\']{3,140})["\']',
    re.IGNORECASE,
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
    # Any tag, not just img/a/div/span: component-driven sites render people as
    # custom elements. Abbott's leadership page is a wall of
    # <abbott-card eyebrow="Chairman and Chief Executive Officer"
    #              heading="Robert B. Ford "> — the entire board and executive
    # team live in attributes, and none of it survives tag stripping.
    # Two passes: tags first, then every attribute WITHIN each tag. A single
    # regex over the document returns non-overlapping matches, so after it
    # matched eyebrow= it resumed inside the same tag and never saw heading= —
    # which is exactly where Abbott keeps the person's name.
    hits: list[str] = []
    for tag in re.finditer(r"<[a-zA-Z][\w:-]*\s[^>]{0,4000}?>", html):
        name_part, role_part = "", ""
        for attr, val in _ATTR_PAIR_RE.findall(tag.group(0)):
            val = re.sub(r"\s+", " ", val).strip()
            if not val or _ATTR_NOISE_RE.search(val):
                continue
            head = re.split(r"\s*[,–—|]\s*|\s+-\s+", val, maxsplit=1)[0].strip()
            if _PERSONISH_RE.match(head):
                name_part = name_part or val
            elif (attr.lower() in ("eyebrow", "subheading", "data-title", "caption")
                  and len(val) < 90):
                role_part = role_part or val
        if name_part and role_part:
            hits.append(f"{name_part} — {role_part}")
        elif name_part:
            hits.append(name_part)
        elif role_part:
            hits.append(role_part)

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
        parts.append(f"[Page: {url}]\n{_page_window(text, _PER_PAGE_CHARS)}")
    return "\n".join(parts)


_LEADERSHIP_HEADING_RE = re.compile(
    r"\b(?:our\s+leadership|leadership\s+team|executive\s+(?:team|committee|leadership)"
    r"|board\s+of\s+(?:directors|trustees|governors)|management\s+team|senior\s+leadership"
    r"|governing\s+board|meet\s+(?:the|our)\s+(?:team|leadership|leaders)|our\s+leaders"
    r"|leadership)\b", re.IGNORECASE)


_TITLE_WORD_RE = re.compile(
    r"\b(?:chief|officer|president|vice\s+president|svp|evp|vp|director|head\s+of|counsel"
    r"|founder|chair(?:man|woman|person)?|treasurer|secretary|dean|minister|trustee)\b",
    re.IGNORECASE)


def _page_window(text: str, cap: int) -> str:
    """
    The part of a page's text worth sending, at most *cap* characters.

    Keeping the first *cap* characters cut Pyramid Consulting's roster off:
    its "Our Leadership" section starts at character 12,540, after two copies
    of the navigation menu and a block of schema JSON. When the text is too
    long, the window starts just before the first leadership heading instead.
    """
    # Schema JSON already arrives as [Structured Data]; as visible text it is
    # only noise eating the budget.
    text = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith(('{"@context', '{"@graph')))
    if len(text) <= cap:
        return text
    # Candidate windows: the top of the page, and one just before each
    # leadership heading. Keep the one holding the most job titles — that is
    # where a roster is, whether it sits at the top (a board page whose footer
    # links "Leadership") or below a long header (Pyramid).
    starts = {0} | {max(0, m.start() - 600) for m in _LEADERSHIP_HEADING_RE.finditer(text)}

    def titles(start: int) -> int:
        return len(_TITLE_WORD_RE.findall(text, start, start + cap))

    best = max(sorted(starts), key=titles)     # ties keep the earliest
    return text[best:best + cap]


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
You are an organisational intelligence assistant. The organisation may be a \
company, a government ministry or agency, a public-sector body, a state-owned \
enterprise, a university or business school, a hospital, an NGO or a \
foundation. Extract every person explicitly named in the source text as a \
member of its governing board, its leadership / executive team, or its senior \
leadership.

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
board — everyone listed under the GOVERNING body, regardless of title: Board \
of Directors, Supervisory Board, Board of Trustees, Board of Governors, \
Governing Board / Body / Council, University Council or Senate (governing), \
Board of Commissioners, Steering Board: Chairman, Vice-Chairman, Managing \
Director (when on board), Executive Director, Non-Executive Director, \
Independent Director, Nominee Director, Lead Director, Trustee, Governor, \
Council Member. Include committee memberships if mentioned. Members of a purely \
ADVISORY board or council are NOT the governing board — leave them out.

executives — EVERY person explicitly listed as a member of the Executive \
Committee, Operating Committee, Management Committee, Executive Leadership \
Team, or C-Suite, regardless of their specific title. This includes (but is \
NOT limited to): CEO, President, COO, CFO, CTO, CIO, CISO, CMO, CHRO, CRO, \
CLO / General Counsel, Chief Strategy Officer, Chief Digital Officer, Chief \
Commercial Officer, Group President — AND ALSO any EVP, Senior EVP, or \
business head who appears on the same committee listing page. If the source \
says "Operating Committee" or "Executive Team" and lists 14 people, put ALL \
14 in executives.
Outside companies the same tier goes by other names — put these in executives \
too: anyone listed under "Leadership Team", "Senior Leadership Team", \
"Management Team", "Executive Board", "Management Board", "Cabinet", \
"Rectorate", "Dean's Office", "Office of the President"; and heads of the \
organisation and their deputies: Minister, Deputy / Assistant Minister, \
Secretary-General, Permanent Secretary, Under-Secretary, Director-General, \
Deputy Director-General, Commissioner, Governor (of a central bank or \
authority), Chief Executive, Managing Director, Executive Director (as head), \
President / Vice-President, Rector / Vice-Rector, Chancellor / \
Vice-Chancellor, Provost, Dean, Associate / Deputy Dean, Executive Director, \
Chief Operating & Administrative Officer, Chief Growth Officer, Registrar.

senior_leadership — EVPs, SVPs, VPs, Business Heads, Country Heads, Plant \
Heads, Division Heads, Group Heads, Regional Heads named in the source who \
are NOT already listed in executives above. In the public sector and \
academia: Directors of departments / directorates, Assistant \
Under-Secretaries, Heads of Department, Associate Deans, Program Directors \
who manage a unit.

NOT LEADERSHIP — leave out: academic chairs and professorships held as a \
research title ("X Chair in Purposeful Leadership", "Professor of Strategy"), \
faculty lists, students, alumni, event speakers, people quoted in news, and \
leaders of OTHER organisations mentioned on the page.

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
You are an organisational intelligence assistant with knowledge of public \
companies, governments, public-sector bodies, universities and NGOs.

Return the current governing board (Board of Directors / Trustees / Governors) \
and executive leadership (C-Suite, or the organisation's head — Minister, \
Director-General, Secretary-General, President, Dean, Rector — and their \
leadership team) for the named organisation based on your training knowledge. Only include people currently \
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
    # Government, public sector, universities, non-profits
    "dean", "deans", "minister", "ministers", "ministry", "rector", "rectorate",
    "president", "presidency", "chancellor", "provost", "secretary-general",
    "director-general", "officials", "cabinet", "organisation", "organization",
    "structure", "org-chart", "administration", "governors", "commissioners",
    "a-propos", "qui-sommes-nous", "equipe", "gouvernance", "ueber-uns",
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
    # ── Government, public sector, universities, non-profits ───────────────────
    "/about/leadership-team",
    "/about-us/leadership-team",
    "/about-us/our-leadership",
    "/senior-leadership",
    "/senior-leadership-team",
    "/leadership-and-governance",
    "/about/governance",
    "/board-of-trustees",
    "/about/board-of-trustees",
    "/governing-board",
    "/board-of-governors",
    "/council",
    "/administration",
    "/about/administration",
    "/office-of-the-president",
    "/president",
    "/about/president",
    "/dean",
    "/deans-office",
    "/about/dean",
    "/the-minister",
    "/minister",
    "/about/minister",
    "/about-us/minister",
    "/senior-officials",
    "/organisational-structure",
    "/organizational-structure",
    "/about/organisational-structure",
    "/about/organizational-structure",
    "/about-us/organizational-structure",
    "/about-us/organisational-structure",
    "/org-chart",
    # ── The About page itself: smaller organisations list leaders there ──────
    "/about-us",
    "/about",
    "/en/about-us",
    "/en/about",
    # ── IR subdomains are tried separately (see _ir_leadership_urls) ───────────
]

# Guessed paths that 404 in a row before guessing stops. A site that answers
# none of the first several dozen conventional spellings does not follow the
# conventions, and each guess costs a round trip: on qatar.exed.hec.edu the
# guesses alone spent the entire 180s harvest deadline and found nothing.
_MAX_GUESS_MISSES = int(os.environ.get("ORGANOGRAM_MAX_GUESS_MISSES", "60"))
# …and a time limit, because on a slow site 60 misses can still be minutes.
_GUESS_BUDGET_S = int(os.environ.get("ORGANOGRAM_GUESS_BUDGET_S", "45"))
# Pages the site's own navigation points at are the likeliest to hold the
# roster; a slow server gets longer for those than for a guessed path.
_NAV_FETCH_TIMEOUT = 15

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
    r"|board|director|directors|governance|member|members"
    r"|trustee|trustees|council|dean|deans|minister|ministers|officials|leaders)(?:[/-]|$)",
    re.IGNORECASE,
)


# Pages that describe the past, not the present. A 2019 press release names
# its executives as current, so nothing in the text marks them as former —
# the URL is the only reliable signal that the page is dated.
_ARCHIVAL_URL_RE = re.compile(
    # Matched anywhere inside a path segment, not just at its start: Abbott
    # serves its newsroom under /corpnewsroom/, which an anchored pattern
    # walked straight past — ten of those articles then consumed the entire
    # harvest budget before the real leadership page could be drilled into.
    r"/[^/]*(?:newsroom|press-?release|press-?room|mediacent|media-cent"
    r"|newscent|news-cent)[^/]*(?:/|$)"
    r"|/(?:news|press|media|blog|stories|article|articles|archive|archives"
    r"|events?|speeches?|tag|tags|category|categories|author|authors"
    r"|annual-reports?|sec-filings?|earnings|quarterly-earnings"
    r"|19\d\d|20\d\d)(?:[/-]|$)",
    re.IGNORECASE,
)


def _is_archival(url: str) -> bool:
    """True when a URL's path marks it as dated/archived content."""
    from urllib.parse import urlparse
    return bool(_ARCHIVAL_URL_RE.search(urlparse(url).path))


# Per-page transient. The accumulated harvest (_MAX_HARVEST_CHARS, ~110 KB of
# text) is negligible against the ~368 MB available, and pages are fetched one
# at a time and freed — so page COUNT costs time, not memory, and is left alone
# rather than trading away recall. This cap bounds the one real spike: a single
# oversized page being decoded and regex-scanned several times.
_MAX_PAGE_BYTES = int(os.environ.get("ORGANOGRAM_MAX_PAGE_KB", "1024")) * 1024


def _get_bounded(url: str, timeout: int = 6, final_url: list | None = None) -> str | None:
    """
    GET a page, refusing to hold more than _MAX_PAGE_BYTES of it in memory.

    resp.text on an unbounded response is only the first copy — _strip_html,
    _extract_js_data and _extract_attr_names each scan the whole string and
    the re.sub chains allocate more. One oversized page could therefore cost
    many times its own size. Streaming lets us stop reading instead.

    Returns the decoded HTML, or None when the page is unusable (non-HTML,
    non-200, or over the cap).
    """
    import httpx

    with httpx.stream("GET", url, headers=_FETCH_HEADERS, timeout=timeout,
                      follow_redirects=True) as resp:
        if resp.status_code != 200:
            return None
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "json" not in ctype:
            logger.debug("Skipping %s: content-type %s", url, ctype)
            return None
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_PAGE_BYTES:
            logger.info("Skipping %s: %d bytes exceeds page cap", url, int(declared))
            return None

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > _MAX_PAGE_BYTES:
                logger.info("Skipping %s: body exceeded %d-byte page cap",
                            url, _MAX_PAGE_BYTES)
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if final_url is not None:
            final_url.append(str(resp.url))      # where redirects actually landed

    encoding = resp.encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


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
        # Index pages get only part of the char budget; the rest is held back
        # for individual bio pages, which is where a roster actually lives.
        # Ten newsroom articles once consumed 107 KB of a 110 KB budget and
        # left nothing for 34 Abbott bio pages that were already queued.
        self.index_budget_chars = int(_MAX_HARVEST_CHARS * _INDEX_BUDGET_SHARE)
        self.deadline = time.monotonic() + _HARVEST_DEADLINE_S
        self._expired = False
        self.dead_hosts: dict[str, int] = {}   # host → consecutive connect failures
        self.learned_prefix: str | None = None   # e.g. "/en-us"
        self.learned_suffix: str | None = None   # e.g. ".html"
        self.last_html = ""                      # most recent harvested page
        self.content_hashes: set[int] = set()    # bodies already harvested

    @property
    def exhausted(self) -> bool:
        # Hold back the reserve only while bio pages are actually waiting. The
        # sitemap often yields the bio pages directly, and those are fetched as
        # index pages — reserving unconditionally then capped the crawl at the
        # 55% mark with nothing to spend the remainder on.
        cap = self.index_budget_chars if self.bio_queue else _MAX_HARVEST_CHARS
        return self._over_budget(cap)

    @property
    def fully_exhausted(self) -> bool:
        """Budget including the slice reserved for bio pages."""
        return self._over_budget(_MAX_HARVEST_CHARS)

    def _over_budget(self, char_cap: int) -> bool:
        import time
        if self.chars >= char_cap or len(self.blocks) >= _MAX_PAGES:
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

    def fetch(self, url: str, follow_bios: bool = False, timeout: int = 6) -> bool:
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
        landed: list = []
        try:
            html = _get_bounded(url, timeout=timeout, final_url=landed)
        except Exception as exc:
            self.dead_hosts[host] = self.dead_hosts.get(host, 0) + 1
            logger.debug("Fetch %s: %s", url, exc)
            return False
        self.dead_hosts.pop(host, None)
        if html is None:
            return False
        # Sites that redirect unknown addresses to the homepage made every
        # guessed path "succeed": pyramidci.com answered all ten guesses under
        # /about-us/ with its homepage. Asking for a page and getting "/" back
        # means the page doesn't exist.
        if landed:
            from urllib.parse import urlparse as _up
            asked, got = _up(url).path.rstrip("/"), _up(landed[0]).path.rstrip("/")
            if asked and asked != got and (
                    not got or re.fullmatch(r"/[a-z]{2}(?:[-_][a-z]{2})?", got, re.I)):
                logger.debug("Redirected to the homepage, treating as missing: %s", url)
                return False
        block = _extract_page_signals(html, url)
        # A JS shell with no structured data is genuinely empty — but one that
        # ships __NEXT_DATA__ or JSON-LD carries the whole leadership roster,
        # and the old code discarded those pages before extracting anything.
        if not block:
            return False
        if _is_js_shell(html) and "[Page:" in block and "[JS Data" not in block \
                and "[Structured Data" not in block:
            return False
        # The same body under a different address — a soft 404 served with a
        # 200, or one page reachable by many spellings — adds nothing.
        body_hash = hash(re.sub(r"^\[Page:[^\]]*\]", "", block).strip())
        if body_hash in self.content_hashes:
            return False
        self.content_hashes.add(body_hash)

        self.blocks.append(block)
        self.last_html = html
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

    def path_variants(self, path: str) -> list[str]:
        """Spellings to try for *path*, narrowed once the site's convention is known.

        Guessing every locale prefix and .html spelling for every known path is
        ~12 requests each; across the full path list that is hundreds of 404s
        and the wall-clock deadline gone. After one path resolves, the same
        prefix/suffix is assumed for the rest.
        """
        if self.learned_prefix is not None or self.learned_suffix is not None:
            prefix = self.learned_prefix or ""
            suffix = self.learned_suffix or ""
            base = path if path.startswith(prefix + "/") else f"{prefix}{path}"
            if suffix and not base.endswith((".html", ".htm", "/")):
                base += suffix
            return [base]
        return _path_variants(path)

    def learn_convention(self, variant: str, path: str) -> None:
        """Remember the prefix/suffix that just worked."""
        if self.learned_prefix is not None:
            return
        suffix = ".html" if variant.endswith(".html") else ""
        trimmed = variant[: -len(suffix)] if suffix else variant
        prefix = trimmed[: -len(path)] if path and trimmed.endswith(path) else ""
        self.learned_prefix = prefix
        self.learned_suffix = suffix
        logger.info("Learned URL convention: prefix=%r suffix=%r", prefix, suffix)

    def drain_bios(self) -> None:
        """Fetch queued bio/detail pages, spending the reserved budget."""
        fetched = 0
        while self.bio_queue and not self.fully_exhausted and fetched < _MAX_BIO_LINKS:
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
        # "/Programs-and-Degrees/Executive-MBA" says "executive" but is a
        # course page; its admissions staff are not the leadership.
        if _is_offtopic_url(absolute):
            continue
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


# Where an organisation keeps its people, read from its own navigation. Path
# guessing only finds corporate conventions; a school, ministry, agency or
# charity names the page whatever it likes ("About us", "Leadership team",
# "The Minister", "Organisational structure", "Office of the Dean") — and
# links it from the homepage. Matched against the link's path AND its text.
_NAV_STRONG_RE = re.compile(
    # Phrases, not bare words, where a bare word is also a product or a topic:
    # NVIDIA's nav has "Executive Insights" and "NVIDIA App Enterprise", and a
    # hardware maker sells development "boards".
    r"\b(?:leadership|leaders|governance|board\s+of|boards?\s+of|our\s+board|the\s+board"
    r"|board\s+members|management\s+team|management\s+board|management\s+committee"
    r"|senior\s+management|executive\s+team|executive\s+committee|executive\s+leadership"
    r"|executive\s+board|executives|trustees?|council"
    r"|directors|officers|cabinet|senior[- ]officials?"
    r"|dean|deans|rector|rectorate|chancellor|provost|president|presidency"
    r"|minister|ministers|secretary[- ]general|director[- ]general|commissioners?"
    r"|governors|organi[sz]ational[- ]structure|organi[sz]ation[- ]structure"
    r"|org[- ]chart|our[- ]people|our[- ]team|who[- ]we[- ]are|administration"
    r"|gouvernance|direction|dirigeants|equipe|vorstand|geschaeftsleitung"
    r"|junta|consejo|directorio)\b", re.IGNORECASE)
_NAV_WEAK_RE = re.compile(
    r"\b(?:about|about[- ]us|aboutus|team|people|a[- ]propos|qui[- ]sommes[- ]nous"
    r"|ueber[- ]uns|sobre[- ]nosotros|chi[- ]siamo|over[- ]ons|the[- ]school"
    r"|the[- ]ministry|the[- ]authority)\b", re.IGNORECASE)
# Big lists of people who are not the leadership, or pages that never hold it.
_NAV_EXCLUDE_RE = re.compile(
    r"\b(?:faculty|professors?|staff[- ]directory|students?|alumni|admissions?"
    r"|programs?|programmes?|courses?|careers?|jobs|vacanc|contact|login|sign[- ]in"
    r"|apply|donate|shop|privacy|cookies?|terms|events?|news|press|media|blog"
    r"|calendar|tenders?|procurement|faq|search"
    # A school's catalogue is full of leadership words: "Executive MBA",
    # "Healthcare Management", "Emerging Leaders Track".
    r"|mba|emba|degrees?|masters?|bachelors?|phd|doctorate|certificates?|diplomas?"
    r"|education|training|academy|track|summer|scholarships?"
    # Marketing sections of a product company.
    r"|insights?|software|solutions?|products?|partners?|industries|technolog(?:y|ies)"
    r"|platforms?|resources|support|developers?|downloads?|gaming|store|pricing)\b",
    re.IGNORECASE)


def _nav_leadership_links(html: str, base_url: str, limit: int = 8) -> list[tuple[str, bool]]:
    """
    Same-host links that probably lead to the leadership, as (url, strong).

    strong = the link names leadership outright ("Leadership Team", "Board of
    Trustees", "The Minister"); weak = an About page, where smaller
    organisations list their leaders. Strong links come first.
    """
    from urllib.parse import urljoin, urlparse

    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    strong: list[str] = []
    weak: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                         html, flags=re.DOTALL | re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href).split("?")[0]
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") \
                or parsed.netloc.lower().removeprefix("www.") != base_host:
            continue
        if parsed.path in ("", "/") or absolute in seen or _is_archival(absolute):
            continue
        # "/en-us/" is the homepage in another language, not a section.
        if re.fullmatch(r"/[a-z]{2}(?:[-_][a-z]{2})?/?", parsed.path, re.IGNORECASE):
            continue
        if re.search(r"\.(?:pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|mp4)$", parsed.path, re.I):
            continue
        anchor = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        # Judge the link by its own words: the last path segment and the text.
        label = f"{parsed.path.rstrip('/').rsplit('/', 1)[-1]} {anchor}".replace("-", " ")
        label = label.replace("_", " ")
        if _NAV_EXCLUDE_RE.search(label):
            continue
        if _NAV_STRONG_RE.search(label):
            strong.append(absolute)
        elif _NAV_WEAK_RE.search(label):
            weak.append(absolute)
        else:
            continue
        seen.add(absolute)
    return [(u, True) for u in strong][:limit] + \
        [(u, False) for u in weak][:max(0, limit - len(strong))]


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
    "council", "dean", "minister", "rector", "chancellor", "provost",
    "president", "secretary-general", "director-general", "senior-officials",
    "organisational-structure", "organizational-structure", "organisation-structure",
    "organization-structure", "org-chart", "governors", "commissioners",
    "gouvernance", "qui-sommes-nous", "equipe-de-direction",
    "vorstand", "aufsichtsrat", "geschaeftsleitung", "direccion", "directorio",
    "junta", "consejo", "conseil", "dirigeants", "consiglio", "bestuur",
    "styrelse", "ledelse", "yonetim",
}


# An organisation's "About us" page itself — where smaller organisations,
# schools and public bodies list their leadership team (HEC Paris Doha does:
# the roster is on /about-us and nowhere else). Its sub-pages (history,
# mission) are not matched.
_ABOUT_PAGE_RE = re.compile(
    r"/(?:about|about-us|aboutus|who-we-are|a-propos|qui-sommes-nous|ueber-uns"
    r"|uber-uns|sobre-nosotros|chi-siamo|over-ons)/?$")


# Catalogue and directory pages. At a school, "leadership" and "management"
# are subjects: hec.edu's search hits were /purposeful-leadership,
# /innovation-management-and-globalization and faculty department pages, and a
# professor's endowed chair came back as an executive.
_OFFTOPIC_URL_RE = re.compile(
    r"/(?:faculty|faculty-research|programs?|programmes?|courses?|degrees?"
    r"|admissions?|executive-education|mba|emba|masters?|bachelors?|phd|students?"
    r"|alumni|events?|careers?|jobs|vacancies|tenders?|services|e-services"
    r"|publications?|knowledge|insights?|case-?stud(?:y|ies)|success-stories"
    r"|blogs?|whitepapers?|webinars?)(?:[/-]|$)", re.IGNORECASE)


def _is_offtopic_url(url: str) -> bool:
    from urllib.parse import urlparse
    return bool(_OFFTOPIC_URL_RE.search(urlparse(url).path))


def _sitemap_leadership_urls(domain: str, limit: int = 40) -> list[str]:
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
            if _is_offtopic_url(loc):
                continue
            if any(kw in low for kw in _SITEMAP_URL_SIGNAL) or _ABOUT_PAGE_RE.search(low):
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


# Locale prefixes real corporate sites canonicalise to. Without these the
# crawler depended on a search engine to find pages we could have guessed.
_LOCALE_PREFIXES = ["", "/en-us", "/en-gb", "/us/en", "/en", "/global/en"]


def _path_variants(path: str) -> list[str]:
    """Every spelling of a leadership path worth trying, cheapest first.

    Enterprise CMSes differ on two details we cannot know in advance: whether
    pages carry a ".html" suffix (Adobe Experience Manager does — Abbott 404s
    /about-abbott/leadership but serves /about-abbott/leadership.html) and
    whether they sit under a locale prefix.
    """
    out: list[str] = []
    for prefix in _LOCALE_PREFIXES:
        if prefix and path.startswith(prefix + "/"):
            continue                      # already carries this prefix
        base = f"{prefix}{path}"
        out.append(base)
        if not base.endswith((".html", ".htm", "/")):
            out.append(base + ".html")
    return out


def _discover_from_navigation(h: "_Harvester", domain: str, seed_url: str = "") -> int:
    """
    Follow the organisation's own navigation to its leadership pages.

    Reads the homepage (and a page the user pointed at, when they gave a full
    URL), fetches the links that name the leadership, and from any About page
    reached that way follows its leadership links one level further. Returns
    how many pages were harvested.
    """
    hits = 0
    start_pages: list[tuple[str, str]] = []      # (url, html) to read links from
    if seed_url:
        # The page the user named is evidence in its own right.
        if h.fetch(seed_url, follow_bios=True, timeout=_NAV_FETCH_TIMEOUT):
            hits += 1
            start_pages.append((seed_url, h.last_html))
    for home in (f"https://{domain}/", f"https://www.{domain}/"):
        if h.exhausted:
            break
        try:
            html = _get_bounded(home, timeout=_NAV_FETCH_TIMEOUT)
        except Exception:
            html = None
        if html:
            start_pages.append((home, html))
            break

    strong_hits = 0
    for base, html in start_pages:
        for url, strong in _nav_leadership_links(html, base):
            if h.exhausted:
                break
            if not h.fetch(url, follow_bios=True, timeout=_NAV_FETCH_TIMEOUT):
                continue
            hits += 1
            if strong:
                strong_hits += 1
                continue
            # An About page is often a hub: "About us" → "Leadership".
            for sub, sub_strong in _nav_leadership_links(h.last_html, url, limit=4):
                if sub_strong and not h.exhausted and h.fetch(
                        sub, follow_bios=True, timeout=_NAV_FETCH_TIMEOUT):
                    hits += 1
                    strong_hits += 1
            # …and its leadership pages often sit right under it without being
            # linked in the HTML: NVIDIA's /about-nvidia/ never links its own
            # /about-nvidia/board-of-directors/, which exists.
            strong_hits += _probe_under(h, url)
    if hits:
        logger.info("Navigation discovery for '%s': %d page(s), %d leadership",
                    domain, hits, strong_hits)
    # Only leadership pages count: an About page or a menu hit is not a reason
    # to skip looking for the board.
    return strong_hits


_UNDER_ABOUT = ["board-of-directors", "leadership", "executive-team", "leadership-team",
                "management", "governance", "executive-leadership", "our-leadership",
                "management-team", "board"]


def _probe_under(h: "_Harvester", about_url: str) -> int:
    """Try the usual leadership pages directly under an About page."""
    from urllib.parse import urlparse
    path = urlparse(about_url).path
    if not path or path == "/":
        return 0
    suffix = "/" if about_url.endswith("/") else (".html" if about_url.endswith(".html") else "")
    base = about_url[: -len(".html")] if suffix == ".html" else about_url.rstrip("/")
    found = 0
    for leaf in _UNDER_ABOUT:
        if h.exhausted:
            break
        if h.fetch(f"{base}/{leaf}{suffix}", follow_bios=True, timeout=_NAV_FETCH_TIMEOUT):
            found += 1
    return found


def _fetch_static_leadership(domain: str, harvester: "_Harvester | None" = None,
                             seed_url: str = "") -> str:
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

    # 0. The organisation's own navigation. One homepage request says where
    #    THIS site keeps its people, whatever it calls them — the corporate
    #    path list below never guessed "/about-us", where HEC Paris Doha lists
    #    its leadership team.
    nav_hits = _discover_from_navigation(h, domain, seed_url)

    # 1. The sitemap first. It costs one request and names REAL urls, where
    #    guessing costs a request per spelling and usually 404s. On Abbott it
    #    lists every board and executive bio page directly, which no amount of
    #    path guessing would have found (/about-abbott/leadership is a 404;
    #    only /about-abbott/leadership.html works).
    sitemap_hits = 0
    for url in _sitemap_leadership_urls(domain):
        if h.exhausted:
            break
        if h.fetch(url, follow_bios=True):
            sitemap_hits += 1

    # 2. Known leadership paths, across locale prefixes and .html spellings.
    #    Skipped when the sitemap already produced a real harvest, so a site
    #    that told us where its pages are is not then probed with hundreds of
    #    guesses.
    if sitemap_hits + nav_hits < 2:
        import time as _time
        misses = 0
        guess_until = _time.monotonic() + _GUESS_BUDGET_S

        def _stop() -> bool:
            return (h.exhausted or misses >= _MAX_GUESS_MISSES
                    or _time.monotonic() >= guess_until)

        for path in _LEADERSHIP_FETCH_PATHS:
            if _stop():
                break
            for variant in h.path_variants(path):
                if _stop():
                    break
                if h.fetch(f"https://www.{domain}{variant}", follow_bios=True):
                    h.learn_convention(variant, path)
                    misses = 0
                    break                 # found this path; stop trying spellings
                if h.fetch(f"https://{domain}{variant}", follow_bios=True):
                    h.learn_convention(variant, path)
                    misses = 0
                    break
                misses += 1
        if misses >= _MAX_GUESS_MISSES or _time.monotonic() >= guess_until:
            logger.info("Path guessing stopped for '%s' (%d misses in a row)",
                        domain, misses)

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


_GROUNDING_REDIRECT = "grounding-api-redirect"


def _resolve_grounding_urls(urls: list[str]) -> list[str]:
    """Follow each grounding redirect one hop to the real page URL."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        import httpx
    except ImportError:
        return urls

    def one(url: str) -> str:
        if _GROUNDING_REDIRECT not in url:
            return url
        try:
            r = httpx.get(url, follow_redirects=False, timeout=6, headers=_FETCH_HEADERS)
            return r.headers.get("location") or ""
        except Exception:
            return ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [u for u in pool.map(one, urls[:60]) if u]


def _is_relevant_grounding_url(url: str, domain: str) -> bool:
    """A search hit worth fetching: a leadership-looking page on the org's site."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if domain and not (host == domain or host.endswith("." + domain)):
        # Off-site hits (a governance blog, a news article) describe other
        # organisations or other eras; the grounded TEXT still keeps what they
        # said about this one.
        return False
    if _is_archival(url) or _is_offtopic_url(url):
        return False
    low = url.lower()
    return any(kw in low for kw in _GEMINI_URL_SIGNAL) or bool(_ABOUT_PAGE_RE.search(low))


def _subdomain_hosts(urls: list[str], domain: str) -> list[str]:
    """Distinct subdomains of *domain* among *urls* (www excluded)."""
    from urllib.parse import urlparse
    if not domain:
        return []
    out: list[str] = []
    for u in urls:
        host = urlparse(u).netloc.lower().removeprefix("www.")
        if host != domain and host.endswith("." + domain) and host not in out:
            out.append(host)
    return out


def _gemini_discover_leadership_urls(
    company_name: str,
    api_key: str,
    domain: str = "",
    location: str = "",
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
        place = f" {location}" if location else ""
        queries = [
            # Query 1: leadership pages. Phrases, not bare "leadership": at a
            # school or a ministry, leadership and management are subjects.
            f'site:{domain} "leadership team" OR "executive committee" OR '
            f'"management team" OR "senior management" OR "officers"',
            # Query 2: board / governance pages, incl. public-sector boards
            f'site:{domain} "board of directors" OR "board of trustees" OR '
            f'"governing board" OR "council" OR "governance"',
            # Query 3: about-us / team pages (smaller organisations list leaders there)
            f'site:{domain} "about us" OR "our team" OR "who we are"',
            # Query 4: investor-relations subdomains (ir.domain, investors.domain)
            f'site:investors.{domain} OR site:ir.{domain}',
            # Query 5: the organisation itself, by name and place. A domain can
            # host many entities — hec.edu is HEC Paris, while HEC Paris in
            # Qatar lives on qatar.exed.hec.edu — and heads of public bodies
            # and schools carry titles a corporate query never asks for.
            f'site:{domain} "{company_name}"{place} dean OR president OR '
            f'"director general" OR minister OR "chief executive" OR "managing director"',
        ]
        if location:
            # Query 6: open web, for when the entity's own site is a subdomain
            # or a section the site: filter alone doesn't surface.
            queries.append(
                f"Who is on the current leadership team of {company_name} in {location}? "
                f"List each leader's full name and title (e.g. Dean, Director, President, "
                f"Minister, Director General, CEO, COO) from the official website.")
    else:
        # No domain — fall back to open-web queries with native-language variants
        place = f" in {location}" if location else ""
        queries = [
            (f"Who are the current Board of Directors (or Board of Trustees / governing "
             f"board) of {company_name}{place}? "
             f"List each member's full name and title from the official website."),
            (f"Who are the leadership team members of {company_name}{place} — e.g. CEO, "
             f"President, Dean, Minister, Director General, Secretary General, COO, CFO? "
             f"List each leader's full name and title from the official website."),
            # Spanish / French / German fallback for non-English companies
            (f"{company_name} junta directiva directores ejecutivos OR "
             f"{company_name} conseil d'administration dirigeants OR "
             f"{company_name} Vorstand Aufsichtsrat"),
        ]

    all_urls:  list[str] = []
    all_texts: list[str] = []
    raw_urls:  list[str] = []
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
                        seen.add(url)
                        raw_urls.append(url)

        except Exception as exc:
            logger.warning("Gemini query error for '%s': %s", company_name, exc)

    # Grounding sources arrive as vertexaisearch redirect links whose path is
    # an opaque token. The keyword filter used to run on that token, so
    # Google's actual hits — NVIDIA's board page among them — were dropped
    # unless random letters happened to spell "bod" or "team". Resolve each
    # to the page it points at and judge THAT.
    for url in _resolve_grounding_urls(raw_urls):
        if url in all_urls or not _is_relevant_grounding_url(url, domain):
            continue
        all_urls.append(url)
        logger.info("Gemini grounding URL for '%s': %s", company_name, url)

    return all_urls, "\n\n---\n\n".join(all_texts)


def _gemini_fetch_leadership(
    company_name: str,
    domain: str,
    api_key: str,
    site_url: str = "",
    location: str = "",
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
        company_name, api_key, domain=domain, location=location
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
            _fetch_static_leadership(domain, harvester=harvester, seed_url=site_url)

        # Then the URLs Gemini grounding discovered, following one level of
        # bio links from each — these cover sites whose paths we can't guess.
        for u in discovered_urls[:15]:
            if harvester.exhausted:
                break
            harvester.fetch(u, follow_bios=True)

        # A search hit on a SUBDOMAIN usually means the entity lives there
        # (qatar.exed.hec.edu under hec.edu): read that site's navigation too.
        for host in _subdomain_hosts(discovered_urls, domain)[:2]:
            if harvester.exhausted:
                break
            _discover_from_navigation(harvester, host)

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
    # An empty board is almost never the truth for an organisation that has a
    # leadership team. NVIDIA's roster lives only on investor.nvidia.com,
    # which refuses Render's IP, so the board came solely from the grounded
    # search answer — 11 directors on one run and none on the next. One
    # board-only search settles it.
    # "Empty" includes a board of one or two: the CEO arrives on the board list
    # as a dual-role director, and NVIDIA's chart showed exactly that — Jensen
    # Huang alone, the other ten directors missing.
    board_retry = 0
    if len(result.get("board") or []) < _MIN_PLAUSIBLE_BOARD:
        retry = _gemini_board_retry(company_name, domain, location, api_key)
        if retry.get("board"):
            board_retry = len(retry["board"])
            result = _merge_leadership([result, {"board": retry["board"],
                                                 "executives": [], "senior_leadership": []}])
    harvest_info["stages"] = {
        "gemini_board":       gemini_board,
        "corroborated_board": corrob_board,
        "merged_board":       merged_board,
        "board_retry":        board_retry,
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


def _site_only_leadership(company_name: str, domain: str, site_url: str = "") -> dict:
    """Leadership from the organisation's own site alone, extracted by Claude."""
    harvester = _Harvester()
    try:
        _fetch_static_leadership(domain, harvester=harvester, seed_url=site_url)
        harvester.drain_bios()
    except Exception as exc:
        logger.warning("Site-only harvest failed for '%s': %s", company_name, exc)
    text = harvester.text()
    info = {"pages": len(harvester.fetched), "chars": len(text),
            "urls": harvester.fetched[:40], "mode": "site_only"}
    if not text:
        return {"board": [], "executives": [], "_harvest": info,
                "_error": "no_content_harvested"}
    partials = []
    for idx, chunk in enumerate(_chunk_for_synthesis(text), start=1):
        part = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=(f"Extract the governing board and the leadership / executive team "
                      f"for {company_name}.\nNO HALLUCINATIONS: only people explicitly "
                      f"named below.\n\n{chunk}"),
            label=f"{company_name} site {idx}",
            source_text=chunk,
        )
        if part.get("board") or part.get("executives") or part.get("senior_leadership"):
            partials.append(part)
    if not partials:
        return {"board": [], "executives": [], "_harvest": info, "_error": "no_extraction"}
    result = _resolve_stale(_merge_leadership(partials), harvester.canonical_text())
    _strip_internal_fields(result)
    result["_harvest"] = info
    return result


_MIN_PLAUSIBLE_BOARD = 3


def _gemini_board_retry(company_name: str, domain: str, location: str,
                        api_key: str) -> dict:
    """One grounded search for the board alone, extracted to {board: [...]}."""
    try:
        import httpx
    except ImportError:
        return {}
    # Name the website: "Pyramid Consulting" alone got another company's
    # founders (a different "Pyramid") back as directors.
    ident = ", ".join(x for x in (f"website {domain}" if domain else "", location) if x)
    place = f" ({ident})" if ident else ""
    queries = [
        f"Who are the current members of the Board of Directors (or Board of Trustees / "
        f"governing board) of {company_name}{place}? List every member's full name and "
        f"board role, from the official website, investor relations site or latest proxy "
        f"statement. Only this organisation — not others with a similar name.",
    ]
    if domain:
        queries.append(f'site:{domain} "board of directors" OR "board of trustees" '
                       f'OR "governing board" members')
    texts: list[str] = []
    for query in queries:
        try:
            resp = httpx.post(
                f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent",
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": query}]}],
                      "tools": [{"googleSearch": {}}],
                      "generationConfig": {"temperature": 0, "maxOutputTokens": 2048}},
                timeout=90,
            )
            if not resp.is_success:
                continue
            data = resp.json()
            um = data.get("usageMetadata", {})
            from usage_tracker import record_usage
            record_usage("gemini", _GEMINI_MODEL,
                         um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0))
            for cand in data.get("candidates", []):
                parts = cand.get("content", {}).get("parts", [])
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                if text:
                    texts.append(text)
        except Exception as exc:
            logger.warning("Board retry search failed for '%s': %s", company_name, exc)
    if not texts:
        return {}
    # Grounded answers come from search results, not the org's own pages, so
    # no domain lock here — but only the board section is taken from it.
    joined = "\n\n".join(texts)
    parsed = _gemini_synthesize_chunk(company_name, "", joined, api_key, 1, 1)
    board = (parsed or {}).get("board", []) or []
    kept = [p for p in board if _named_near_org(p.get("name", ""), joined, company_name, domain)]
    if len(kept) < len(board):
        logger.info("Board retry for '%s': dropped %d not tied to the organisation",
                    company_name, len(board) - len(kept))
    logger.info("Board retry for '%s': %d member(s)", company_name, len(kept))
    return {"board": kept}


def _named_near_org(name: str, text: str, company_name: str, domain: str,
                    window: int = 1500) -> bool:
    """The person is named within *window* chars of the organisation's full name
    or its domain — not merely somewhere in an answer that may cover another
    organisation with a similar name."""
    low = _ascii_fold(text.lower())
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", _ascii_fold(company_name.lower())).split()
             if w not in {"inc", "corp", "corporation", "ltd", "limited", "plc", "llc", "the",
                          "company", "co", "group", "holdings"}]
    anchors = [" ".join(words)] if words else []
    stem = (domain or "").lower().removeprefix("www.").split(".")[0]
    if len(stem) >= 3:
        anchors.append(stem)
    surname = (name.split() or [""])[-1].lower()
    if not surname or not anchors:
        return True
    for anchor in anchors:
        start = low.find(anchor)
        while start != -1:
            if surname in low[max(0, start - window): start + len(anchor) + window]:
                return True
            start = low.find(anchor, start + 1)
    return False


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
                f"Extract the governing board (Board of Directors / Trustees / "
                f"Governors) and the leadership / executive team for {company_name}.\n"
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
        f"Extract the governing board (Board of Directors / Trustees / Governors) and "
        f"the leadership / executive team for {company_name}.\n\n"
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


def llm_fetch_leadership(company_name: str, domain: str = "", site_url: str = "",
                         location: str = "") -> dict:
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
    # A full URL or a location points the search at one entity under a shared
    # domain, so it is part of what was asked.
    if site_url or location:
        cache_key += f"|{site_url.strip().lower()}|{location.strip().lower()}"
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    gemini_result: dict = {}
    gemini_key    = os.environ.get("GEMINI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # ── Step 1: Gemini primary (Google Search grounding + JSON synthesis) ─────
    if gemini_key:
        logger.info("Step 1 Gemini for '%s'", company_name)
        result = _gemini_fetch_leadership(company_name, domain, gemini_key,
                                          site_url=site_url, location=location)
        gemini_result = result
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _cache_leadership(cache_key, result)
            return result
        logger.info("Gemini returned no leaders for '%s' — falling back to Wikipedia", company_name)
    else:
        logger.debug("GEMINI_API_KEY not set — skipping Gemini for '%s'", company_name)

    # ── Step 1b: the organisation's own site, read by Claude ──────────────────
    # Without Gemini (no key, or an outage) the site crawl never ran, and the
    # only remaining source was Wikipedia — which has nothing on most public
    # bodies, schools and private companies. The crawl needs no search engine.
    if not gemini_key and anthropic_key and domain:
        result = _site_only_leadership(company_name, domain, site_url)
        if result.get("board") or result.get("executives"):
            result["_source"] = "web"
            _cache_leadership(cache_key, result)
            return result

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
    tokens = re.sub(r"[^a-z ]", " ", cleaned).split()
    # Drop single letters (middle initials) EXCEPT a leading one: "N. Ahuja"
    # must keep its "n" or it collapses to a surname-only key and can never be
    # matched against "Nita Ahuja". Committee tables print directors that way.
    words = [w for i, w in enumerate(tokens) if len(w) > 1 or i == 0]
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
    if _SCOPE_QUALIFIER_RE.search(t) or _has_unit_qualifier(t):
        return set()
    return {role for role, pattern in _SINGULAR_ROLES if pattern.search(t)}


# "of the Board" / "of the Company" still describe the top seat; anything else
# after "of" or a comma names a business unit or an outside employer.
# Searched, not anchored: the company's own name often sits in front of the
# scope word ("of the 3M Board of Directors", "of Maersk Group"), and those
# are still the top seat rather than a division.
_GLOBAL_SCOPE_RE = re.compile(
    r"\b(?:board|company|group|corporation|organi[sz]ation|firm)\b",
    re.IGNORECASE,
)
_NAME_SUFFIX_RE = re.compile(r"^(?:jr|sr|ii|iii|iv|phd|cpa|cfa|md|esq)\.?$", re.IGNORECASE)


def _has_unit_qualifier(title: str) -> bool:
    """
    True when a title is scoped to a business unit or an outside employer.

    "CEO, Maersk Ocean" and "CFO of Terminals" are divisional seats that
    legitimately coexist with the group CEO and CFO — treating them as claims
    on the single global role would delete real executives, which is the same
    mistake that deleted real directors. "Chairman of the Board" is not scoped.
    """
    for tail in re.split(r",\s*", title)[1:]:
        tail = tail.strip()
        if tail and not _NAME_SUFFIX_RE.match(tail) and not _GLOBAL_SCOPE_RE.search(tail):
            return True
    m = re.search(r"\bof\s+(.+)$", title, flags=re.IGNORECASE)
    if m and not _GLOBAL_SCOPE_RE.search(m.group(1).strip()):
        return True
    return False


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

    # NOT the board. A director's title describes their OUTSIDE career —
    # "Retired Chief Sustainability Officer and Chief Marketing Officer,
    # Morgan Stanley", "Retired Group Vice President, Ford" — so two directors
    # who each ran a different company collide on the same role key and one
    # gets deleted as stale. That is exactly what dropped three real 3M
    # directors (12 merged -> 9 final) in a live run. _clean_list and
    # _rich_to_flat already refuse to apply retirement logic to board titles
    # for this same reason; this pass has to honour it too. Board seats are
    # not singular roles in any case — a board has many directors.
    for section in ("executives", "senior_leadership"):
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


def _tidy_name(name: str) -> str:
    """'DJELLOUL BEKKA' → 'Djelloul Bekka'; 'Joshua KOBB' → 'Joshua Kobb'.

    Sites print names in capitals as styling. Only all-capital words of three or
    more letters are changed, so initials ("J.P.") and particles stay as given,
    and so does a name already written in mixed case ("McDew", "de Holan").
    """
    def fix(word: str) -> str:
        letters = re.sub(r"[^A-Za-zÀ-ÿ]", "", word)
        if len(letters) >= 3 and letters.isupper():
            return "-".join(p[:1].upper() + p[1:].lower() for p in word.split("-"))
        return word
    return " ".join(fix(w) for w in str(name or "").split())


# "Executive" is a senior word in "Executive Vice President" and a junior job
# title on its own ("Sales Executive", "Executive - Resourcing" — common in
# India). Pyramid Consulting's team page listed ten "Executive Resourcing"
# recruiters, and all ten reached Executive Management.
_SENIOR_WORD_RE = re.compile(
    r"\b(?:chief|president|vice\s+president|vp|evp|svp|director|head|officer|chair"
    r"|chairman|chairwoman|chairperson|partner|founder|managing|general\s+manager"
    r"|dean|minister|secretary|commissioner|governor|rector|chancellor|provost"
    r"|ceo|cfo|coo|cto|cio|cmo|chro|cso|clo|treasurer|controller|counsel|fellow"
    r"|principal|leader|lead)\b", re.IGNORECASE)
_JUNIOR_TITLE_RE = re.compile(
    r"\b(?:executive|analyst|associate|coordinator|specialist|assistant|intern|trainee"
    r"|recruiter|representative|consultant|engineer|administrator|clerk|officer\s+trainee"
    r"|resourcing|sourcer)\b", re.IGNORECASE)


def _is_junior_title(title: str) -> bool:
    """A title with a junior job word and no senior one."""
    # Supporting a leader is not being one, whoever is named after "to".
    if re.search(r"\b(?:executive|personal|administrative)\s+assistant\b|\bassistant\s+to\b",
                 str(title or ""), re.I):
        return True
    t = re.sub(r"\bexecutive\s+(?:vice|director|chair|officer|committee|board|leadership"
               r"|president|partner|team)\b", "", str(title or ""), flags=re.I)
    t = re.sub(r"\bchief\s+executive\b", "", t, flags=re.I)
    return bool(_JUNIOR_TITLE_RE.search(t)) and not _SENIOR_WORD_RE.search(t)


def _drop_junior(result: dict) -> None:
    """Keep junior staff out of Executive Management and senior leadership."""
    for section in ("executives", "senior_leadership"):
        people = result.get(section) or []
        kept = [p for p in people if not _is_junior_title(p.get("title", ""))]
        if len(kept) < len(people):
            logger.info("Dropped %d junior title(s) from %s", len(people) - len(kept), section)
        result[section] = kept


def _strip_internal_fields(result: dict) -> None:
    """Remove merge bookkeeping so it never reaches the DAG or the frontend."""
    _drop_junior(result)
    for section in ("board", "executives", "senior_leadership"):
        for person in result.get(section) or []:
            person.pop("_mentions", None)
            if person.get("name"):
                person["name"] = _tidy_name(person["name"])


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
            # A bare initial against a full given name, same surname:
            # "N. Ahuja" is "Nita Ahuja". Committee tables print directors as
            # initials while the roster prints them in full, and keying on
            # first+last listed both as separate people.
            if len(short) == 1 and long_.startswith(short):
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
        if not name or not title or not _is_full_name(name):
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
        if not name or not title or not _is_full_name(name):
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
                    if not name or not title or not _is_full_name(name):
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


_INITIAL_ONLY_RE = re.compile(r"^[A-Za-z]\.?$")


def _is_full_name(name: str) -> bool:
    """
    True when *name* has a real given name AND a real surname.

    len(split()) >= 2 alone lets a truncated extraction through: a live run
    produced "John P." as an executive, which renders as a person's name in
    the org chart. A trailing initial is not a surname.
    """
    parts = str(name or "").split()
    if len(parts) < 2:
        return False
    # Only the SURNAME must be a real word. "J. Smith" and "C. Scharf" are how
    # plenty of sites print a real person (_name_in_source handles that form
    # deliberately), so requiring a spelled-out given name would cost recall.
    return not _INITIAL_ONLY_RE.match(parts[-1])


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
        if not name or not title or not _is_full_name(name):
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
