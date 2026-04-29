"""
Universal Organogram Engine - FastAPI Server
Provides REST endpoints for the React frontend.
"""

import io
import json
import re
import tempfile
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from structural_engine import build_from_records, OrganogramDAG, OrganogramDB

# ─── Flexible column name mapping ────────────
# Maps any common column variant → canonical field name
COLUMN_ALIASES: dict[str, str] = {
    # FirstName
    "firstname": "FirstName", "first_name": "FirstName",
    "first name": "FirstName", "given name": "FirstName",
    "givenname": "FirstName", "fname": "FirstName",
    # LastName
    "lastname": "LastName", "last_name": "LastName",
    "last name": "LastName", "surname": "LastName",
    "family name": "LastName", "lname": "LastName",
    # Designation
    "designation": "Designation", "title": "Designation",
    "job title": "Designation", "jobtitle": "Designation",
    "position": "Designation", "role": "Designation",
    "job role": "Designation", "current title": "Designation",
    "currenttitle": "Designation", "headline": "Designation",
    "current position": "Designation",
    # Company
    "company": "Company", "company name": "Company",
    "companyname": "Company", "organization": "Company",
    "organisation": "Company", "employer": "Company",
    "current company": "Company", "currentcompany": "Company",
    "account": "Company", "firm": "Company",
    # LinkedInURL
    "linkedinurl": "LinkedInURL", "linkedin url": "LinkedInURL",
    "linkedin": "LinkedInURL", "linkedin profile": "LinkedInURL",
    "profile url": "LinkedInURL", "profileurl": "LinkedInURL",
    "url": "LinkedInURL",
    # Location
    "location": "Location", "city": "Location",
    "country": "Location", "region": "Location",
    "office location": "Location", "officelocation": "Location",
    "geography": "Location", "geo": "Location",
    "based in": "Location", "basedin": "Location",
    # Industry_Hint
    "industry_hint": "Industry_Hint", "industry": "Industry_Hint",
    "sector": "Industry_Hint", "domain": "Industry_Hint",
    "vertical": "Industry_Hint", "industry hint": "Industry_Hint",
    "industryhint": "Industry_Hint",
    # ProTrail ProfileLevel — treated as strong industry/dept signal
    "profilelevel": "ProfileLevel", "profile level": "ProfileLevel",
    "profile_level": "ProfileLevel",
    # ProTrail raw department field
    "department": "Department", "dept": "Department",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Remap DataFrame columns to canonical schema names."""
    rename_map = {}
    for col in df.columns:
        key = re.sub(r"[^a-z0-9 _]", "", str(col).lower().strip())
        canonical = COLUMN_ALIASES.get(key)
        if canonical and canonical not in df.columns:
            rename_map[col] = canonical
    if rename_map:
        df = df.rename(columns=rename_map)

    # Synthesize FullName → FirstName/LastName if only full name exists
    if "FirstName" not in df.columns and "LastName" not in df.columns:
        for full_col in ["name", "full name", "fullname", "contact name",
                         "person name", "employee name"]:
            key = re.sub(r"[^a-z0-9 ]", "", full_col)
            match = next((c for c in df.columns
                          if re.sub(r"[^a-z0-9 ]", "", c.lower()) == key), None)
            if match:
                parts = df[match].astype(str).str.split(n=1, expand=True)
                df["FirstName"] = parts[0]
                df["LastName"]  = parts[1] if parts.shape[1] > 1 else ""
                break

    return df

app = FastAPI(title="Organogram Engine API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
def ping():
    """Lightweight wake-up probe — keeps Render from cold-starting on first upload."""
    return {"status": "ok"}

# ─── In-memory state (single session) ────────
_dag: OrganogramDAG | None = None
_db:  OrganogramDB  | None = None


def _require_dag():
    if _dag is None or _db is None:
        raise HTTPException(status_code=400, detail="No dataset loaded. POST /upload first.")
    return _dag, _db


# ─────────────────────────────────────────────
# UPLOAD
# ─────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile = File(...),
                      company_name: str = Query("Organization")):
    """Accept CSV, JSON, or Excel. Build the DAG and return stats."""
    global _dag, _db

    content = await file.read()
    fname   = file.filename or ""

    try:
        if fname.endswith(".json"):
            records = json.loads(content)
            if isinstance(records, dict):
                records = records.get("records", [])
            detected_cols = list(records[0].keys()) if records else []
            mapped_cols   = detected_cols
        elif fname.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
            detected_cols = list(df.columns)
            df = normalize_columns(df)
            mapped_cols = list(df.columns)
            records = df.where(pd.notna(df), "").to_dict(orient="records")
        elif fname.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content))
            detected_cols = list(df.columns)
            df = normalize_columns(df)
            mapped_cols = list(df.columns)
            records = df.where(pd.notna(df), "").to_dict(orient="records")
        else:
            raise HTTPException(status_code=400,
                                detail="Unsupported format. Use JSON, CSV, or Excel.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not records:
        raise HTTPException(status_code=422, detail="File appears empty.")

    MAX_ROWS = 500
    if len(records) > MAX_ROWS:
        records = records[:MAX_ROWS]

    _dag, _db = build_from_records(records, company_name=company_name)

    # Report which canonical fields were found
    canonical = {"FirstName", "LastName", "Designation", "Company",
                 "LinkedInURL", "Location", "Industry_Hint"}
    found   = canonical & set(mapped_cols)
    missing = canonical - set(mapped_cols)

    return {
        "status": "ok",
        "records_ingested": len(records),
        "detected_columns": detected_cols,
        "mapped_columns":   mapped_cols,
        "canonical_found":  sorted(found),
        "canonical_missing": sorted(missing),
        "stats": _dag.stats(),
    }


@app.post("/load-demo")
async def load_demo():
    """Load the bundled test_data.json."""
    global _dag, _db
    data_path = Path(__file__).parent / "test_data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="test_data.json not found")

    with open(data_path) as f:
        records = json.load(f)

    _dag, _db = build_from_records(records, company_name="Global Conglomerate Inc.")
    return {
        "status": "ok",
        "records_ingested": len(records),
        "stats": _dag.stats(),
    }


# ─────────────────────────────────────────────
# GRAPH DATA
# ─────────────────────────────────────────────

@app.get("/graph")
def get_full_graph():
    """Return full node + edge list for the frontend renderer."""
    dag, _ = _require_dag()
    return {
        "nodes": dag.get_flat_nodes(),
        "edges": dag.get_edges(),
        "stats": dag.stats(),
    }


@app.get("/tree")
def get_tree(root: str = Query("root_global"), max_depth: int = Query(20)):
    """Return nested tree JSON rooted at `root`."""
    dag, _ = _require_dag()
    tree = dag.get_subtree(root, max_depth=max_depth)
    if not tree:
        raise HTTPException(status_code=404, detail=f"Node '{root}' not found.")
    return tree


@app.get("/subtree")
def get_subtree_db(root: str = Query("root_global")):
    """Recursive CTE from SQLite — flat list with depth."""
    _, db = _require_dag()
    return db.recursive_subtree(root)


@app.get("/search")
def search_nodes(q: str = Query(..., min_length=1)):
    """Full-text search across node labels, sectors, types."""
    _, db = _require_dag()
    return db.search(q)


@app.get("/stats")
def get_stats():
    dag, _ = _require_dag()
    return dag.stats()


@app.get("/industries")
def get_industries():
    """Return loaded industry directories (names + sectors)."""
    from inference_logic import get_nlp
    nlp = get_nlp()
    return {
        "industries": [
            {"id": d.id, "name": d.name, "sector": d.sector}
            for d in nlp._dirs
        ]
    }


@app.get("/executives")
def get_executives(dept_id: str = Query(...)):
    """
    Walk the DAG from dept_id and return all person nodes (sorted by layer),
    skipping ghost/placeholder nodes entirely.
    """
    dag, _ = _require_dag()
    if dept_id not in dag.G:
        raise HTTPException(status_code=404, detail=f"Node '{dept_id}' not found.")

    people: list[dict] = []

    def collect(nid: str, visited: set):
        if nid in visited:
            return
        visited.add(nid)
        attrs = dict(dag.G.nodes.get(nid, {}))
        if attrs.get("node_type") == "person":
            people.append(attrs)
        for child in dag.G.successors(nid):
            collect(child, visited)

    collect(dept_id, set())
    people.sort(key=lambda p: (p.get("layer", 99), p.get("label", "")))
    return {"executives": people, "count": len(people)}


# ─────────────────────────────────────────────
# PUBLIC COMPANY  (Yahoo Finance + Web scraping)
# ─────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_board_member(title: str) -> bool:
    """Return True if title indicates a board / non-executive / supervisory role."""
    t = title.lower()
    return any(kw in t for kw in [
        "board of directors", "board of director", "board member", "board director",
        "chairman of the board", "chairwoman of the board",
        "non-executive director", "non-exec director", "non executive director",
        "independent non-executive director", "independent non-executive",
        "independent director", "outside director", "lead independent director",
        "supervisory board", "board of trustees", "board of management",
        "board of governors", "board of commissioners",
        "trustee", "governor", "member of parliament",
    ])


def _infer_layer(title: str) -> int:
    """
    Fast layer inference from a raw title string.
    Returns 0–10 matching the ProTrail-derived seniority scale.
    Used for public-company BOD/EM data where full NLP pipeline is bypassed.
    """
    t = title.lower()

    # Layer 0 — Board / Non-Executive
    if any(kw in t for kw in [
        "non-executive director", "non-exec director", "independent non-executive",
        "non executive director", "independent director", "outside director",
        "lead independent director", "supervisory board", "board of trustees",
        "board member", "board director",
    ]):
        return 0

    # Layer 1 — C-Suite (checked before chairman to avoid false 0 match)
    if any(kw in t for kw in [
        "chief executive officer", "chief financial officer", "chief operating officer",
        "chief technology officer", "chief information officer", "chief risk officer",
        "chief compliance officer", "chief digital officer", "chief data officer",
        "chief marketing officer", "chief people officer", "chief human resources officer",
        "chief commercial officer", "chief revenue officer", "chief legal officer",
        "chief medical officer", "chief scientific officer", "chief strategy officer",
        "chief accounting officer", "chief product officer", "chief investment officer",
        "chief sustainability officer",
        "president & ceo", "president and ceo", "chairman & ceo", "chairman and ceo",
        "co-founder & ceo", "co-founder and ceo", "founder & ceo", "founder and ceo",
        " ceo", "ceo,",
        " cfo", " cto", " coo", " ciso", " cmo", " chro", " cro",
    ]):
        return 1

    # Layer 0 — Chairman / Chair (after C-suite check so chairman & CEO → L1)
    if any(kw in t for kw in ["chairman", "chairperson", "chairwoman", "trustee", "governor"]):
        return 0

    # Layer 2 — MD / EVP / Executive Director
    if any(kw in t for kw in [
        "executive vice president", " evp", "evp ",
        "managing director", "executive director",
        "group managing director", "group director",
        "country ceo", "regional ceo", "divisional managing director",
        "president, ", "division president", "group president", "global president",
        "principal director",
    ]):
        return 2

    # Layer 3 — SVP / VP / General Manager / Country Head
    if any(kw in t for kw in [
        "senior vice president", " svp", "svp ",
        "first vice president", "group vice president", "corporate vice president",
        "regional vice president", "global vice president",
        "associate vice president", "assistant vice president",
        "vice president", " vp ", "vp,", "vp-", "vp of",
        "general manager", "country head", "regional head", "business head",
    ]):
        return 3

    # Layer 4 — Senior Director / Head of
    if any(kw in t for kw in [
        "senior director", "global director", "regional director",
        "head of ", "global head", "zonal director",
    ]):
        return 4

    # Layer 5 — Director / Head
    if any(kw in t for kw in ["director", "head"]):
        return 5

    # Layer 6 — Senior Manager
    if any(kw in t for kw in [
        "senior manager", "associate director", "deputy director",
        "deputy general manager", "principal manager", "assistant director",
    ]):
        return 6

    # Layer 7 — Manager
    if any(kw in t for kw in ["manager", "team lead", "team leader"]):
        return 7

    # Layer 8 — Senior IC / Staff Engineer
    if any(kw in t for kw in [
        "senior analyst", "senior specialist", "senior consultant", "senior associate",
        "senior engineer", "senior developer", "principal engineer", "staff engineer",
    ]):
        return 8

    # Layer 10 — Graduate / Intern
    if any(kw in t for kw in ["graduate", "intern", "trainee", "apprentice", "junior"]):
        return 10

    # Layer 9 — IC / Analyst / Associate default
    if any(kw in t for kw in ["analyst", "associate", "consultant", "specialist", "engineer"]):
        return 9

    return 5   # fallback: director-level


# ── Yahoo Finance (crumb-based auth) ─────────────────────────────────

def _yahoo_fetch_profile(symbol: str) -> dict:
    """
    Fetch Yahoo Finance assetProfile.
    Strategy 1: crumb from /v1/test/getcrumb (query1 then query2 host).
    Strategy 2: crumb embedded in quote page HTML.
    Raises urllib.error.HTTPError on 4xx/5xx, RuntimeError on parse failure.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import http.cookiejar

    base_hdr = {
        "User-Agent": _UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    api_hdr = {**base_hdr, "Accept": "application/json, text/plain, */*"}

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # 1 — seed cookies from finance.yahoo.com
    opener.open(urllib.request.Request("https://finance.yahoo.com/", headers=base_hdr), timeout=10)

    # 2 — get crumb (try query1, fall back to query2, fall back to scraping page)
    crumb = None
    for crumb_url in [
        "https://query1.finance.yahoo.com/v1/test/getcrumb",
        "https://query2.finance.yahoo.com/v1/test/getcrumb",
    ]:
        try:
            with opener.open(urllib.request.Request(crumb_url, headers=api_hdr), timeout=8) as r:
                c = r.read().decode("utf-8").strip()
                if c and len(c) < 50:
                    crumb = c
                    break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                continue   # try next host
            raise

    # Fallback: parse crumb from the quote page HTML
    if not crumb:
        import re as _re2
        try:
            with opener.open(
                urllib.request.Request(
                    f"https://finance.yahoo.com/quote/{symbol}",
                    headers=base_hdr,
                ),
                timeout=10,
            ) as r:
                page_html = r.read().decode("utf-8", errors="replace")
            m = _re2.search(r'"crumb"\s*:\s*"([^"]{6,30})"', page_html)
            if m:
                crumb = m.group(1).replace("\\u002F", "/")
        except Exception:
            pass

    if not crumb:
        raise RuntimeError("Could not obtain Yahoo Finance crumb (rate limited). Try again in a few minutes.")

    # 3 — fetch assetProfile (try query1 then query2)
    for host in ["query1", "query2"]:
        url = (
            f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            f"?modules=assetProfile&crumb={urllib.parse.quote(crumb)}"
        )
        try:
            with opener.open(urllib.request.Request(url, headers=api_hdr), timeout=12) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and host == "query1":
                continue
            raise

    raise RuntimeError("Yahoo Finance unavailable on both query1 and query2 hosts.")


def _parse_officers(officers: list, board: list, executives: list) -> None:
    """Classify Yahoo Finance officer records into board / exec lists."""
    for off in officers:
        title   = off.get("title", "")
        name    = off.get("name", "Unknown")
        age     = off.get("age")
        pay_obj = off.get("totalPay")
        pay     = pay_obj.get("raw") if isinstance(pay_obj, dict) else None
        layer   = _infer_layer(title)
        person  = {"name": name, "title": title, "age": age, "pay": pay, "layer": layer,
                   "source": "yahoo"}
        if _is_board_member(title):
            board.append(person)
        else:
            executives.append(person)


# ── Website scraper ────────────────────────────────────────────────────

# URL paths tried in order when looking for leadership/board pages
_LEADERSHIP_PATHS = [
    "/about/leadership",
    "/about/management-team",
    "/about/executive-leadership",
    "/about/executive-team",
    "/about/board-of-directors",
    "/about/governance",
    "/about-us/leadership",
    "/about-us/team",
    "/about-us/management",
    "/about-us/board-of-directors",
    "/company/leadership",
    "/company/team",
    "/company/management",
    "/company/about/leadership",
    "/leadership",
    "/team",
    "/management",
    "/board-of-directors",
    "/investor-relations/governance/board-of-directors",
    "/investor-relations/governance/board",
    "/investors/governance",
    "/corporate-governance/board",
    "/en/about/leadership",
    "/about",
]

import re as _re

_TITLE_KW = _re.compile(
    r"\b(ceo|cfo|cto|coo|ciso|cmo|chro|president|chairman|chairperson|"
    r"director|officer|head of|chief|vp|svp|evp|vice president|founder|"
    r"principal|partner|managing|general counsel|trustee)\b",
    _re.I,
)

_CARD_CLS = _re.compile(
    r"\b(person|team[-_]?member|executive|leader|bio[-_]?card|"
    r"board[-_]?member|management[-_]?team|officer|director[-_]?card|"
    r"leadership[-_]?card|staff[-_]?card|member[-_]?card|people[-_]?card|"
    r"management[-_]?item|executive[-_]?item)\b",
    _re.I,
)


def _looks_like_name(text: str) -> bool:
    """Rough heuristic: 2–5 words, no digits, not all-caps (heading), < 60 chars."""
    words = text.split()
    return (
        2 <= len(words) <= 5
        and len(text) < 60
        and not any(ch.isdigit() for ch in text)
        and text != text.upper()
    )


def _parse_people_from_html(html: str) -> list[dict]:
    """
    Multi-strategy parser.  Returns [{"name": ..., "title": ..., "layer": int}, ...]
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    people: list[dict] = []
    seen: set[str] = set()

    def add(name: str, title: str, source: str = "web") -> None:
        name  = " ".join(name.split())
        title = " ".join(title.split())[:140]
        if _looks_like_name(name) and name not in seen:
            seen.add(name)
            people.append({"name": name, "title": title,
                           "layer": _infer_layer(title), "source": source})

    # ── Strategy 1: JSON-LD ────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Person":
                    add(item.get("name", ""), item.get("jobTitle", ""))
                elif item.get("@type") in ("Organization", "WebPage", "ItemList"):
                    for el in item.get("itemListElement", []):
                        if isinstance(el, dict) and el.get("@type") == "Person":
                            add(el.get("name", ""), el.get("jobTitle", ""))
                    for member in item.get("member", []):
                        if isinstance(member, dict) and member.get("@type") == "Person":
                            add(member.get("name", ""), member.get("jobTitle", ""))
        except Exception:
            pass

    if people:
        return people

    # ── Strategy 2: Schema.org microdata ──────────────────────────────
    for item in soup.find_all(attrs={"itemtype": _re.compile(r"schema\.org/Person", _re.I)}):
        name_el  = item.find(attrs={"itemprop": "name"})
        title_el = item.find(attrs={"itemprop": _re.compile(r"job|title|role", _re.I)})
        if name_el:
            add(name_el.get_text(" ", strip=True),
                title_el.get_text(" ", strip=True) if title_el else "")

    if people:
        return people

    # ── Strategy 3: CSS class card heuristic ──────────────────────────
    for card in soup.find_all(class_=_CARD_CLS):
        name_el = card.find(["h2", "h3", "h4", "h5", "strong", "b"])
        if not name_el:
            continue
        name = name_el.get_text(" ", strip=True)
        title = ""
        for el in card.find_all(["p", "span", "div"]):
            text = el.get_text(" ", strip=True)
            if text and text != name and 1 < len(text.split()) <= 10:
                if _TITLE_KW.search(text) or len(text) < 50:
                    title = text
                    break
        add(name, title)

    if people:
        return people

    # ── Strategy 4: class-based name/title detection ──────────────────
    # Matches sites like Apple (profile-name / typography-profile-title)
    _NAME_CLS  = _re.compile(r"\b(profile[-_]?name|person[-_]?name|member[-_]?name|bio[-_]?name|exec[-_]?name|leader[-_]?name)\b", _re.I)
    _TITLE_CLS = _re.compile(r"\b(profile[-_]?title|person[-_]?title|member[-_]?title|bio[-_]?title|exec[-_]?title|job[-_]?title|role[-_]?title|position)\b", _re.I)

    for name_el in soup.find_all(class_=_NAME_CLS):
        name = name_el.get_text(" ", strip=True)
        if not _looks_like_name(name):
            continue
        # Look for title in a sibling with a matching class, or next h4/p/span
        title = ""
        parent = name_el.parent
        if parent:
            title_el = parent.find(class_=_TITLE_CLS)
            if title_el and title_el != name_el:
                title = title_el.get_text(" ", strip=True)
        if not title:
            for sib in list(name_el.next_siblings)[:4]:
                if hasattr(sib, 'get_text'):
                    t = sib.get_text(" ", strip=True)
                    if t and t != name and len(t) < 120:
                        title = t
                        break
        add(name, title)

    if people:
        return people

    # ── Strategy 5: heading pair (h3→h4 or h2→h3 name/title) ──────────
    # Apple-style: <h3>Tim Cook</h3> <h4>CEO</h4> inside a container
    for h in soup.find_all(["h2", "h3"]):
        name = h.get_text(" ", strip=True)
        if not _looks_like_name(name):
            continue
        # Check all next siblings (incl. h4) for a title
        title = ""
        for sib in list(h.next_siblings)[:4]:
            if not hasattr(sib, 'get_text'):
                continue
            t = sib.get_text(" ", strip=True)
            if not t or t == name:
                continue
            # Accept if it looks like a title (keyword match OR short ≤ 12 words)
            if (_TITLE_KW.search(t) or len(t.split()) <= 12) and len(t) < 120:
                title = t
                break
        add(name, title)

    if people:
        return people

    # ── Strategy 6: heading + subtitle proximity (p/span/div) ──────────
    for h in soup.find_all(["h3", "h4"]):
        name = h.get_text(" ", strip=True)
        if not _looks_like_name(name):
            continue
        sib = h.find_next_sibling(["p", "span", "div"])
        if sib:
            t = sib.get_text(" ", strip=True)
            if _TITLE_KW.search(t) and len(t) < 120:
                add(name, t)

    return people


def _fetch_url(url: str, opener) -> str | None:
    """Try fetching url; return decoded html or None on error."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with opener.open(req, timeout=10) as r:
            if r.status == 200:
                return r.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def _scrape_company_website(domain: str) -> dict:
    """
    Attempt to extract BOD and executive info by scraping the company's
    public website.  Tries a ranked list of common leadership page paths.
    Returns {"board": [...], "executives": [...], "page_url": str | None}.
    """
    import urllib.request
    import http.cookiejar

    # Normalise domain → base URL
    base = domain.strip()
    if not _re.match(r"https?://", base):
        base = "https://" + base
    base = base.rstrip("/")

    jar   = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    people: list[dict] = []
    page_url: str | None = None

    for path in _LEADERSHIP_PATHS:
        html = _fetch_url(base + path, opener)
        if html:
            found = _parse_people_from_html(html)
            if len(found) >= 2:           # need at least 2 people to be credible
                people  = found
                page_url = base + path
                break

    # Classify into board / exec
    board:      list[dict] = []
    executives: list[dict] = []
    for p in people:
        if _is_board_member(p["title"]):
            board.append(p)
        else:
            executives.append(p)

    board.sort(key=lambda x: (x["layer"], x["name"]))
    executives.sort(key=lambda x: (x["layer"], x["name"]))

    return {"board": board, "executives": executives, "page_url": page_url}


def _merge_people(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Merge two person lists, de-duplicating by name (fuzzy — first 3 words)."""
    seen: set[str] = set()
    merged: list[dict] = []
    for p in primary + secondary:
        key = " ".join(p["name"].split()[:3]).lower()
        if key not in seen:
            seen.add(key)
            merged.append(p)
    return merged


# ── Unified endpoint ───────────────────────────────────────────────────

@app.get("/public-company")
async def get_public_company(
    ticker: str | None = Query(default=None, min_length=1, max_length=12),
    domain: str | None = Query(default=None, min_length=3, max_length=128),
):
    """
    Fetch Board of Directors and Executive Management for a public company.

    - **ticker**: stock ticker (e.g. AAPL) → Yahoo Finance assetProfile
    - **domain**: company website (e.g. apple.com) → web scrape leadership page
    - Both can be provided; results are merged and de-duplicated.
    At least one of ticker or domain is required.
    """
    import urllib.error

    if not ticker and not domain:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of: ticker (e.g. AAPL) or domain (e.g. apple.com)",
        )

    board:      list[dict] = []
    executives: list[dict] = []
    company_name = ticker or domain or ""
    industry     = ""
    sector_val   = ""
    website      = domain or ""

    # ── Yahoo Finance ──────────────────────────────────────────────────
    ticker_error: str | None = None
    if ticker:
        symbol = ticker.strip().upper()
        try:
            raw = _yahoo_fetch_profile(symbol)
            profile = raw["quoteSummary"]["result"][0]["assetProfile"]
            _parse_officers(profile.get("companyOfficers") or [], board, executives)
            company_name = profile.get("longName") or profile.get("shortName") or symbol
            industry     = profile.get("industry", "")
            sector_val   = profile.get("sector", "")
            website      = website or profile.get("website", "")
        except urllib.error.HTTPError as exc:
            ticker_error = f"Yahoo Finance {exc.code}: check ticker '{ticker}'"
        except (KeyError, IndexError, TypeError):
            ticker_error = f"No Yahoo Finance data for '{ticker}'"
        except RuntimeError as exc:
            ticker_error = str(exc)
        except Exception as exc:
            ticker_error = f"Yahoo Finance error: {exc}"

    # ── Website scrape ─────────────────────────────────────────────────
    web_error: str | None = None
    page_url:  str | None = None
    if domain:
        try:
            scraped  = _scrape_company_website(domain)
            page_url = scraped["page_url"]
            if not company_name or company_name == ticker:
                company_name = domain
            board      = _merge_people(board,      scraped["board"])
            executives = _merge_people(executives, scraped["executives"])
        except Exception as exc:
            web_error = f"Website scrape failed: {exc}"

    # If both sources failed, surface the errors
    if not board and not executives:
        errors = [e for e in [ticker_error, web_error] if e]
        raise HTTPException(
            status_code=502,
            detail="; ".join(errors) if errors else "No executive data found.",
        )

    board.sort(key=lambda p: (p["layer"], p["name"]))
    executives.sort(key=lambda p: (p["layer"], p["name"]))

    return {
        "ticker":        ticker.upper() if ticker else None,
        "domain":        domain,
        "companyName":   company_name,
        "industry":      industry,
        "sector":        sector_val,
        "website":       website,
        "pageUrl":       page_url,
        "board":         board,
        "executives":    executives,
        "tickerError":   ticker_error,
        "webError":      web_error,
    }


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
