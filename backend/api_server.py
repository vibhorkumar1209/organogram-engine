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
    # ── Name fields ──────────────────────────────────────────────────────
    "firstname": "FirstName", "first_name": "FirstName",
    "first name": "FirstName", "given name": "FirstName",
    "givenname": "FirstName", "fname": "FirstName",

    "lastname": "LastName", "last_name": "LastName",
    "last name": "LastName", "surname": "LastName",
    "family name": "LastName", "lname": "LastName",

    # Vendor FULL_NAME (preferred over first+last when present)
    "full_name": "FullName", "full name": "FullName", "fullname": "FullName",
    "name": "FullName", "contact name": "FullName",
    "person name": "FullName", "employee name": "FullName",

    # ── Title / Designation ──────────────────────────────────────────────
    # JOB_TITLE is the primary vendor title column
    "job_title": "Designation", "job title": "Designation", "jobtitle": "Designation",
    "designation": "Designation", "title": "Designation",
    "position": "Designation", "role": "Designation",
    "job role": "Designation", "current title": "Designation",
    "currenttitle": "Designation", "current position": "Designation",

    # ── Company ──────────────────────────────────────────────────────────
    "company": "Company", "company name": "Company", "company_name": "Company",
    "companyname": "Company", "organization": "Company",
    "organisation": "Company", "employer": "Company",
    "current company": "Company", "currentcompany": "Company",
    "account": "Company", "firm": "Company",

    # ── LinkedIn / profile URL ───────────────────────────────────────────
    "linkedinurl": "LinkedInURL", "linkedin url": "LinkedInURL",
    "linkedin": "LinkedInURL", "linkedin profile": "LinkedInURL",
    "linkedin_url": "LinkedInURL", "profile url": "LinkedInURL",
    "profileurl": "LinkedInURL", "url": "LinkedInURL",

    # ── Location (legacy single-string field) ────────────────────────────
    "location": "Location", "office location": "Location",
    "officelocation": "Location", "geography": "Location",
    "geo": "Location", "based in": "Location", "basedin": "Location",

    # ── Industry / Sector ────────────────────────────────────────────────
    "industry_hint": "Industry_Hint", "industry": "Industry_Hint",
    "sector": "Industry_Hint", "domain": "Industry_Hint",
    "vertical": "Industry_Hint", "industry hint": "Industry_Hint",
    "industryhint": "Industry_Hint",

    # ── ProTrail ProfileLevel ────────────────────────────────────────────
    "profilelevel": "ProfileLevel", "profile level": "ProfileLevel",
    "profile_level": "ProfileLevel",

    # ── Department ───────────────────────────────────────────────────────
    "department": "Department", "dept": "Department",

    # ── Vendor job-location fields (primary region-routing signals) ──────
    # JOB_LOCATION_COUNTRY_CODE is the most authoritative region signal.
    "job_location_country_code":   "job_country_code",
    "job_location_country":        "job_country",
    "job_location_country_region": "job_country_region",
    "job_location_continent":      "job_continent",
    "job_location_state":          "job_state",
    "job_location_state_code":     "job_state",
    "job_location_city":           "job_city",

    # Person-level geography fallbacks (used when job-location is blank)
    "country_code":   "country_code",
    "country_name":   "country_name",
    "country_region": "country_region",
    "continent":      "continent",
    "state_code":     "state_code",
    "state_name":     "state_name",
    "city":           "city",

    # ── Vendor pre-classification ────────────────────────────────────────
    "job_function": "vendor_function",
    "job_level":    "vendor_level",
    "persona":      "vendor_persona",

    # ── LinkedIn enrichment fields ───────────────────────────────────────
    # LINKEDIN_HEADLINE is used as a title fallback when JOB_TITLE is empty.
    "linkedin_headline": "linkedin_headline",
    "linkedin_industry": "linkedin_industry",

    # ── Org-level fallback identifiers ───────────────────────────────────
    # JOB_ORG_LINKEDIN_URL is used to infer company name when no Company column.
    "job_org_linkedin_url": "job_org_linkedin_url",
    "email_domain":         "email_domain",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remap DataFrame columns to canonical schema names.

    Priority for name: FullName (FULL_NAME) > FirstName+LastName.
    Priority for title: Designation (JOB_TITLE) > linkedin_headline.
    Priority for company: Company > job_org_linkedin_url > email_domain.
    Priority for location/region: job_country_code > job_country > Location.
    """
    rename_map = {}
    for col in df.columns:
        key = re.sub(r"[^a-z0-9 _]", "", str(col).lower().strip())
        canonical = COLUMN_ALIASES.get(key)
        if canonical and canonical not in df.columns:
            rename_map[col] = canonical
    if rename_map:
        df = df.rename(columns=rename_map)

    # ── Name synthesis ────────────────────────────────────────────────
    # 1. FullName present → split into First + Last
    if "FullName" in df.columns:
        if "FirstName" not in df.columns or "LastName" not in df.columns:
            parts = df["FullName"].astype(str).str.strip().str.split(n=1, expand=True)
            if "FirstName" not in df.columns:
                df["FirstName"] = parts[0]
            if "LastName" not in df.columns:
                df["LastName"] = parts[1] if parts.shape[1] > 1 else ""
    # 2. No name columns at all → try any remaining full-name-like column
    if "FirstName" not in df.columns and "LastName" not in df.columns:
        for alias in ["contact name", "person name", "employee name"]:
            key = re.sub(r"[^a-z0-9 ]", "", alias)
            match = next((c for c in df.columns
                          if re.sub(r"[^a-z0-9 ]", "", c.lower()) == key), None)
            if match:
                parts = df[match].astype(str).str.split(n=1, expand=True)
                df["FirstName"] = parts[0]
                df["LastName"]  = parts[1] if parts.shape[1] > 1 else ""
                break

    # ── Title fallback: LINKEDIN_HEADLINE when JOB_TITLE is blank ────
    # Strip " at [Company]" suffix that LinkedIn appends to headlines
    # e.g. "Service Engineer at Recorders & Medicare Systems" → "Service Engineer"
    if "linkedin_headline" in df.columns:
        df["linkedin_headline"] = (
            df["linkedin_headline"].astype(str)
            .str.replace(r"\s+at\s+.+$", "", regex=True)
            .str.strip()
        )
    if "Designation" in df.columns and "linkedin_headline" in df.columns:
        mask = df["Designation"].isna() | (df["Designation"].astype(str).str.strip() == "")
        df.loc[mask, "Designation"] = df.loc[mask, "linkedin_headline"]
    elif "linkedin_headline" in df.columns and "Designation" not in df.columns:
        df["Designation"] = df["linkedin_headline"]

    # ── Company fallback: job_org_linkedin_url → slug, email_domain → domain ─
    if "Company" not in df.columns or df["Company"].isna().all():
        if "job_org_linkedin_url" in df.columns:
            def _slug_to_name(url: str) -> str:
                url = str(url or "").strip()
                if not url:
                    return ""
                slug = url.rstrip("/").split("/")[-1]
                return slug.replace("-", " ").title()
            df["Company"] = df["job_org_linkedin_url"].apply(_slug_to_name)
    if "Company" in df.columns:
        mask = df["Company"].isna() | (df["Company"].astype(str).str.strip() == "")
        if "email_domain" in df.columns:
            df.loc[mask, "Company"] = df.loc[mask, "email_domain"].apply(
                lambda d: str(d or "").split(".")[0].title() if d else ""
            )

    # ── Location synthesis: prefer job_country > city, country_name ──
    if "Location" not in df.columns:
        loc_parts = []
        for col in ["job_city", "city", "job_country", "country_name"]:
            if col in df.columns:
                loc_parts.append(col)
                break  # use first available as the Location string
        if loc_parts:
            df["Location"] = df[loc_parts[0]].astype(str).str.strip()

    # ── Industry fallback: LINKEDIN_INDUSTRY when Industry_Hint is blank ─
    if "linkedin_industry" in df.columns:
        if "Industry_Hint" not in df.columns:
            df["Industry_Hint"] = df["linkedin_industry"]
        else:
            mask = df["Industry_Hint"].isna() | (df["Industry_Hint"].astype(str).str.strip() == "")
            df.loc[mask, "Industry_Hint"] = df.loc[mask, "linkedin_industry"]

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


_EMPTY_GRAPH = {"loaded": False, "nodes": [], "edges": [],
                "stats": {"total_nodes": 0, "total_edges": 0,
                          "people_nodes": 0, "ghost_nodes": 0, "max_depth": 0}}


def _require_dag():
    """Return (dag, db) or raise — callers that prefer empty data use _dag_or_none()."""
    if _dag is None or _db is None:
        raise HTTPException(status_code=400, detail="No dataset loaded. POST /upload first.")
    return _dag, _db


def _dag_loaded() -> bool:
    return _dag is not None and _db is not None


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

    try:
        _dag, _db = build_from_records(records, company_name=company_name)
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500,
                            detail=f"Pipeline failed: {e}\n{traceback.format_exc()}")

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
    if not _dag_loaded():
        return {**_EMPTY_GRAPH}
    dag, _ = _require_dag()
    return {
        "loaded": True,
        "nodes": dag.get_flat_nodes(),
        "edges": dag.get_edges(),
        "stats": dag.stats(),
    }


@app.get("/tree")
def get_tree(root: str = Query("root_global"), max_depth: int = Query(20)):
    """Return nested tree JSON rooted at `root`."""
    if not _dag_loaded():
        return {"loaded": False, "id": root, "children": []}
    dag, _ = _require_dag()
    tree = dag.get_subtree(root, max_depth=max_depth)
    if not tree:
        raise HTTPException(status_code=404, detail=f"Node '{root}' not found.")
    return tree


@app.get("/subtree")
def get_subtree_db(root: str = Query("root_global")):
    """Recursive CTE from SQLite — flat list with depth."""
    if not _dag_loaded():
        return {"loaded": False, "nodes": []}
    _, db = _require_dag()
    return db.recursive_subtree(root)


@app.get("/search")
def search_nodes(q: str = Query(..., min_length=1)):
    """Full-text search across node labels, sectors, types."""
    if not _dag_loaded():
        return {"loaded": False, "results": []}
    _, db = _require_dag()
    return db.search(q)


@app.get("/stats")
def get_stats():
    if not _dag_loaded():
        return {"loaded": False, "total_nodes": 0, "total_edges": 0,
                "people_nodes": 0, "ghost_nodes": 0, "max_depth": 0}
    dag, _ = _require_dag()
    return {"loaded": True, **dag.stats()}


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
    if not _dag_loaded():
        return {"loaded": False, "executives": [], "count": 0}
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
# V2 — 5-Agent Organogram Pipeline
# ─────────────────────────────────────────────
#
# POST /v2/pipeline
#   Runs the full 5-agent pipeline (Parser → NLP → Reconciler) directly
#   from a JSON payload. Returns a CanonicalOrganogram JSON.
#
# GET  /v2/promote
#   Runs the nightly LedgerPromoter against the corrections ledger
#   and returns the PromotionReport summary.

from pydantic import BaseModel as _BaseModel
from typing import Any as _Any, Optional as _Optional, List as _List

_BACKEND_ROOT = Path(__file__).resolve().parent
_RULES_DIR    = _BACKEND_ROOT / "rules"
_LEDGER_PATH  = _BACKEND_ROOT / "output" / "corrections_ledger.jsonl"


class V2PersonRecord(_BaseModel):
    """Minimal representation of a person record for the v2 pipeline."""
    name: str = ""
    title: str = ""
    company: str = ""
    source_url: str = ""
    department: _Optional[str] = None
    geography: _Optional[str] = None
    tenure: _Optional[str] = None
    reports_to_name: _Optional[str] = None
    subsidiary: _Optional[str] = None
    vendor_function: _Optional[str] = None
    vendor_level: _Optional[str] = None
    vendor_persona: _Optional[str] = None
    job_country: _Optional[str] = None
    job_country_code: _Optional[str] = None
    job_country_region: _Optional[str] = None
    job_continent: _Optional[str] = None
    job_state: _Optional[str] = None
    job_city: _Optional[str] = None
    job_org_linkedin_url: _Optional[str] = None
    email_domain: _Optional[str] = None
    linkedin_industry: _Optional[str] = None
    linkedin_headline: _Optional[str] = None


class V2LeaderRecord(_BaseModel):
    name: str
    title: str
    source_url: str = ""
    source_type: str = "firm_website"
    is_board: bool = False
    immutable: bool = True


class V2PipelineRequest(_BaseModel):
    firm: str
    industry: str
    org_type: str = "Private"
    client_archetype: str = "Enterprise"
    geography_scope: str = "Global"
    sub_industry: _Optional[str] = None
    default_region: str = "USA"
    records: _List[V2PersonRecord]
    leaders: _List[V2LeaderRecord] = []


@app.post("/v2/pipeline")
async def v2_pipeline(req: V2PipelineRequest):
    """
    Run the 5-agent Adaptive Organogram Engine pipeline.

    Accepts person records + optional authoritative leaders in JSON.
    Returns a CanonicalOrganogram with functional, geographic, and
    legal-entity views — plus node-level metadata (level, function,
    region, matched rule, inference note).

    The overlay is the 481-row rules/region_overlay.csv (Africa +
    Russia/CIS added in v2), backed by 12 archetype JSON files.
    """
    try:
        from organogram.utils.rule_loader import RuleLibrary
        from organogram.agents.nlp_agent import LinkedInNLPAgent
        from organogram.agents.reconciler_agent import ReconcilerAgent
        from organogram.utils.translator import make_google_translator
        from organogram.schemas.types import (
            PersonRecord, AuthoritativeLeader,
        )
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"organogram package not available: {exc}")

    if not _RULES_DIR.exists():
        raise HTTPException(status_code=500,
                            detail="rules/ directory not found in backend")

    # 1 — Load rule library
    try:
        rules = RuleLibrary(_RULES_DIR)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"Failed to load rules: {exc}")

    archetype_data = rules.archetype_for_industry(req.industry)
    if not archetype_data:
        supported = list(rules.archetypes.keys())
        raise HTTPException(
            status_code=422,
            detail=f"No archetype for industry '{req.industry}'. "
                   f"Supported archetype IDs: {supported}",
        )
    archetype_id = archetype_data["archetype_id"]

    # 2 — Convert pydantic records → dataclasses
    persons = [
        PersonRecord(
            name=r.name, title=r.title, company=r.company,
            source_url=r.source_url, department=r.department,
            geography=r.geography, tenure=r.tenure,
            reports_to_name=r.reports_to_name, subsidiary=r.subsidiary,
            vendor_function=r.vendor_function, vendor_level=r.vendor_level,
            vendor_persona=r.vendor_persona,
            job_country=r.job_country, job_country_code=r.job_country_code,
            job_country_region=r.job_country_region,
            job_continent=r.job_continent, job_state=r.job_state,
            job_city=r.job_city,
            job_org_linkedin_url=r.job_org_linkedin_url,
            email_domain=r.email_domain,
            linkedin_industry=r.linkedin_industry,
            linkedin_headline=r.linkedin_headline,
        )
        for r in req.records
    ]
    leaders = [
        AuthoritativeLeader(
            name=L.name, title=L.title, source_url=L.source_url,
            source_type=L.source_type, is_board=L.is_board,
            immutable=L.immutable,
        )
        for L in req.leaders
    ]

    # 3 — Agent 3: NLP classification
    try:
        translator = make_google_translator()
        nlp = LinkedInNLPAgent(
            rules=rules,
            archetype_id=archetype_id,
            sub_industry=req.sub_industry,
            default_region=req.default_region,
            translator=translator,
        )
        normalized = nlp.normalize_all(persons)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"NLP agent error: {exc}")

    # 4 — Agent 4: Reconciler
    try:
        reconciler = ReconcilerAgent(
            rules=rules,
            firm=req.firm,
            industry=req.industry,
            org_type=req.org_type,
            client_archetype=req.client_archetype,
            geography_scope=req.geography_scope,
            sub_industry=req.sub_industry,
        )
        organogram_result = reconciler.reconcile(leaders, normalized)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"Reconciler error: {exc}")

    classified = sum(1 for n in normalized if n.function != "Unclassified")
    return {
        "status": "ok",
        "archetype_id": archetype_id,
        "overlay_rows_loaded": len(rules.overlay_rows),
        "persons_ingested": len(persons),
        "persons_classified": classified,
        "nodes_built": len(organogram_result.nodes),
        "organogram": organogram_result.to_dict(),
    }


@app.get("/v2/promote")
async def v2_promote(
    threshold: int = Query(default=20, ge=1),
    dry_run: bool = Query(default=True),
):
    """
    Run the §13 nightly LedgerPromoter against corrections_ledger.jsonl.

    - **threshold**: min corrections per pattern to trigger promotion (default 20).
    - **dry_run**: if true (default), compute report without writing files.

    Returns a PromotionReport with counts of promoted / skipped rules.
    """
    try:
        from organogram.utils.ledger_promoter import LedgerPromoter
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"organogram package not available: {exc}")

    if not _LEDGER_PATH.exists():
        return {
            "status": "no_ledger",
            "detail": "No corrections ledger found. POST corrections via the SDK first.",
            "ledger_path": str(_LEDGER_PATH),
        }

    promoter = LedgerPromoter()
    report = promoter.promote(
        ledger_path=_LEDGER_PATH,
        rules_dir=_RULES_DIR,
        threshold=threshold,
        dry_run=dry_run,
    )
    return {
        "status": "ok",
        "dry_run": dry_run,
        "threshold": threshold,
        "total_corrections": report.total_corrections,
        "eligible_keys": report.eligible_keys,
        "promoted": report.promoted,
        "already_in_overlay": report.already_in_overlay,
        "no_consensus": report.no_consensus,
        "promoted_rules": [
            {
                "archetype": r.archetype, "region": r.region,
                "sub_industry": r.sub_industry, "title_native": r.title_native,
                "title_en": r.title_en, "level": r.corrected_level,
                "function": r.corrected_function,
                "correction_count": r.correction_count,
                "analyst_ids": r.analyst_ids,
            }
            for r in report.promoted_rules
        ],
        "no_consensus_patterns": [
            {
                "archetype": s.archetype, "region": s.region,
                "sub_industry": s.sub_industry, "title_native": s.title_native,
                "correction_count": s.correction_count,
                "disagreements": s.disagreements,
            }
            for s in report.skipped_no_consensus
        ],
        "summary": report.summary_text(),
    }


@app.post("/v2/corrections")
async def v2_add_correction(correction: dict):
    """
    Append a single analyst correction to the corrections ledger (§13).

    Accepts a JSON body matching the CorrectionRecord schema.
    Required fields: node_id, firm, archetype, archetype_version, region,
    original_title_native, original_title_en, original_level,
    original_function, corrected_level, corrected_function, analyst_id.
    Optional: sub_industry, corrected_reports_to_id, correction_reason.
    """
    try:
        from organogram.utils.corrections_ledger import CorrectionsLedger, CorrectionRecord
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"organogram package not available: {exc}")

    required = [
        "node_id", "firm", "archetype", "archetype_version", "region",
        "original_title_native", "original_title_en", "original_level",
        "original_function", "corrected_level", "corrected_function", "analyst_id",
    ]
    missing = [f for f in required if f not in correction]
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"Missing required fields: {missing}")

    try:
        record = CorrectionRecord.from_dict(correction)
        ledger = CorrectionsLedger(_LEDGER_PATH)
        ledger.append(record)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "status": "ok",
        "node_id": record.node_id,
        "composite_key": list(record.composite_key),
        "ledger_path": str(_LEDGER_PATH),
    }


@app.get("/v2/corrections/summary")
async def v2_corrections_summary():
    """Return a summary of the corrections ledger (totals, by archetype, top patterns)."""
    try:
        from organogram.utils.corrections_ledger import CorrectionsLedger
    except ImportError as exc:
        raise HTTPException(status_code=500,
                            detail=f"organogram package not available: {exc}")

    ledger = CorrectionsLedger(_LEDGER_PATH)
    if not _LEDGER_PATH.exists():
        return {"total_corrections": 0, "unique_title_patterns": 0,
                "by_archetype": {}, "by_region": {}, "top_patterns": []}
    return ledger.summary()


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
