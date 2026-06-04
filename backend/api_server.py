"""
Universal Organogram Engine - FastAPI Server
Provides REST endpoints for the React frontend.
"""

import io
import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Load .env file for local development ─────────────────────────────────────
# On Render/production, env vars are injected by the platform.
# Locally, create backend/.env with ANTHROPIC_API_KEY, PARALLEL_API_KEY, etc.
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        _load_dotenv(_env_path, override=True)  # override=True so .env wins over empty shell vars
        import logging as _log
        _log.getLogger(__name__).info("Loaded env vars from %s", _env_path)
except ImportError:
    pass  # python-dotenv not installed — rely on shell env

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from structural_engine import (
    build_from_records, OrganogramDAG, OrganogramDB,
    _enrich_with_llm_leadership,
    _inject_knowledge_leadership,
    promote_uploaded_to_leadership,
)

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

    # ── Job classification signals ───────────────────────────────────────
    # job_function → soft tiebreaker only (never authoritative for dept)
    # job_level    → layer fallback when title matches no pattern
    "job_function": "job_function",
    "job_level":    "job_level",

    # ── LinkedIn enrichment fields ───────────────────────────────────────
    "linkedin_headline": "linkedin_headline",
    "linkedin_industry": "Industry_Hint",

    # ── Org-level fallback identifiers ───────────────────────────────────
    "job_org_linkedin_url": "job_org_linkedin_url",
    "email_domain":         "email_domain",

    # ── New schema pass-through fields ───────────────────────────────────
    "id":                          "id",
    "job_count":                   "job_count",
    "job_is_current":              "job_is_current",
    "linkedin_connections_count":  "linkedin_connections_count",
}


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remap DataFrame columns to canonical schema names.

    Priority for name: FullName (FULL_NAME) > FirstName+LastName.
    Priority for title: Designation (JOB_TITLE) > linkedin_headline.
    Priority for company: Company > job_org_linkedin_url > email_domain.
    Priority for location/region: job_country_code > job_country > Location.
    """
    # Build rename map ensuring each canonical name is claimed by at most ONE
    # source column.  Without this guard, two source columns that both alias
    # to e.g. "Department" would both be renamed, creating duplicate columns —
    # and any subsequent df["Department"].str.strip() receives a DataFrame
    # instead of a Series, raising "'DataFrame' object has no attribute 'str'".
    rename_map: dict = {}
    already_claimed: set = set(df.columns)   # canonicals already present win
    for col in df.columns:
        key = re.sub(r"[^a-z0-9 _]", "", str(col).lower().strip())
        canonical = COLUMN_ALIASES.get(key)
        if canonical and canonical not in already_claimed:
            rename_map[col] = canonical
            already_claimed.add(canonical)   # block any second column claiming it
    if rename_map:
        df = df.rename(columns=rename_map)

    # Safety net: drop any duplicate columns that may have survived
    # (can happen when the Excel itself has repeated headers).
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="first")]

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

import csv
import os
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Organogram Engine API", version="1.0.0")

# ── Persistent SQLite path ────────────────────────────────────────────────────
# On Render Pro, the container filesystem persists between process restarts
# (though not across deploys).  Writing to /tmp lets us survive a uvicorn
# reload or a watchdog restart without losing the loaded DAG.
_DB_PATH = os.environ.get("ORGANOGRAM_DB_PATH", "/tmp/organogram_last.db")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],   # lets JS read filename from response
)


def _infer_org_name(records: list[dict]) -> str:
    """
    Derive the organisation display name from the data itself.

    Priority:
      1. Most common non-empty Company value across all records.
      2. Derive from most common email_domain  (rmsindia.com → "Rmsindia")
      3. Derive from most common job_org_linkedin_url slug
         (linkedin.com/company/rms-india → "Rms India")
    Returns "" if nothing found.
    """
    from collections import Counter

    def _best(values: list[str]) -> str:
        cleaned = [v.strip() for v in values if v and str(v).strip()
                   and str(v).strip().lower() not in ("nan", "none", "")]
        if not cleaned:
            return ""
        return Counter(cleaned).most_common(1)[0][0]

    # 1. Company column
    companies = [str(r.get("Company", "") or "") for r in records]
    name = _best(companies)
    if name:
        return name

    # 2. email_domain  →  strip TLD, title-case
    domains = [str(r.get("email_domain", "") or "") for r in records]
    domain = _best(domains)
    if domain:
        # "rmsindia.com" → "Rmsindia", "rms-india.co.in" → "Rms India"
        stem = domain.split(".")[0]
        return stem.replace("-", " ").title()

    # 3. job_org_linkedin_url  →  slug → title-case
    urls = [str(r.get("job_org_linkedin_url", "") or "") for r in records]
    url = _best(urls)
    if url:
        slug = url.rstrip("/").split("/")[-1]
        return slug.replace("-", " ").title()

    return ""


@app.on_event("startup")
async def _auto_restore():
    """
    On startup, try to restore the last uploaded DAG from the persistent
    SQLite DB (/tmp/organogram_last.db).  This survives process restarts
    (uvicorn reload, container watchdog) so users don't need to re-upload
    just because the server restarted.  Fresh deploys clear /tmp, so the
    first upload after a deploy still builds fresh.
    """
    global _dag, _db
    if not os.path.exists(_DB_PATH):
        return
    try:
        restored_db  = OrganogramDB(db_path=_DB_PATH)
        restored_dag = restored_db.load_dag()
        if restored_dag and restored_dag.G.number_of_nodes() > 1:
            _dag = restored_dag
            _db  = restored_db
            import logging as _lg
            _lg.getLogger(__name__).info(
                "Auto-restored DAG from %s (%d nodes)",
                _DB_PATH, _dag.G.number_of_nodes(),
            )
    except Exception as exc:
        import logging as _lg
        _lg.getLogger(__name__).warning("Startup DAG restore failed: %s", exc)


@app.get("/ping")
def ping():
    """Lightweight wake-up probe — keeps Render from cold-starting on first upload."""
    return {"status": "ok"}

# ─── In-memory state (single session) ────────
_dag: OrganogramDAG | None = None
_db:  OrganogramDB  | None = None
_classified_records: list = []
_enrichment_done: bool = False   # set True when background BOD/EM enrichment finishes


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
                      company_name: str = Query("Organization"),
                      company_website: str = Query(""),
                      background_tasks: BackgroundTasks = None):
    """Accept CSV, JSON, or Excel. Build the DAG and return stats.

    Optional: company_website=https://morganstanley.com
    When provided, the backend scrapes that domain for BOD/EM leadership data.
    """
    global _dag, _db, _classified_records

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

    MAX_ROWS = 200_000
    if len(records) > MAX_ROWS:
        logger.warning("Upload truncated: %d → %d rows", len(records), MAX_ROWS)
        records = records[:MAX_ROWS]

    # ── Fix 1: always prefer org name inferred from data ────────────────
    # Data-derived name (Company col → email_domain → LinkedIn slug) beats
    # any caller-supplied string (often just the raw filename).
    inferred = _infer_org_name(records)
    company_name = inferred or company_name or "Organization"

    try:
        _dag, _db, _classified, _domain, _industry = build_from_records(
            records, company_name=company_name, db_path=_DB_PATH
        )
        _classified_records = _classified
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500,
                            detail=f"Pipeline failed: {e}\n{traceback.format_exc()}")

    # ── Sync BOD/EM injection ─────────────────────────────────────────────────
    # Passes the inferred domain so the company's own website is tried first.
    # When the domain resolves (web-sourced), leadership is tagged "llm_leadership_web".
    # When not found on the website or no domain, falls back to LLM training
    # knowledge tagged "llm_leadership_ai".
    # The background thread below then runs the full scrape and can upgrade
    # "llm_leadership_ai" entries to "llm_leadership_web" when web data lands.
    # Explicit company_website param overrides auto-detected domain
    if company_website:
        import re as _re
        _m = _re.search(r"(?:https?://)?(?:www\.)?([^/]+)", company_website)
        _domain = _m.group(1) if _m else _domain

    # ── BOD/EM enrichment in background ─────────────────────────────────────
    # Parallel.AI research takes 60-90 s.  Running it synchronously would block
    # the upload response and time out on Render's free tier.  Instead we return
    # the org chart immediately and let the enrichment run in a background thread.
    # The frontend can re-fetch /chart after a few seconds to pick up BOD/EM data.
    global _enrichment_done
    _enrichment_done = False   # reset for new upload

    def _run_enrichment(dag, classified, co_name, domain):
        global _enrichment_done
        import logging, traceback
        _log = logging.getLogger(__name__)
        try:
            # ── Step 0: Full industry classification (web search + homepage) ──
            # build_from_records used quick=True (Claude-knowledge only, ~1s).
            # Here we run the full web-based classification and update the DAG
            # root node if we get a better result.
            try:
                from industry_classifier import classify_industry as _clf, _INDUSTRY_CACHE
                _ikey = co_name.strip().lower()
                _INDUSTRY_CACHE.pop(_ikey, None)  # clear quick-mode result so full reruns
                full_industry = _clf(co_name, domain or "", quick=False)
                if full_industry and "root_global" in dag.G.nodes:
                    meta = dict(dag.G.nodes["root_global"].get("metadata", {}))
                    if meta.get("industry") != full_industry:
                        meta["industry"] = full_industry
                        dag.G.nodes["root_global"]["metadata"] = meta
                        _log.info("Industry updated for '%s': %s", co_name, full_industry)
            except Exception as _ie:
                _log.debug("Background industry classification failed: %s", _ie)

            # ── Step 1: Leadership enrichment (BOD + EM) ──────────────────────
            # Clear any stale empty cache entry so re-uploads always retry
            try:
                from llm_fallback import _LEADERSHIP_CACHE
                cache_key = f"{co_name.strip().lower()}|{(domain or '').strip().lower()}"
                removed = _LEADERSHIP_CACHE.pop(cache_key, None)
                if removed is not None:
                    _log.info("Cleared stale cache for '%s' before enrichment", co_name)
            except Exception:
                pass
            _log.info("Background enrichment starting for '%s' (domain=%s)", co_name, domain)
            _enrich_with_llm_leadership(dag, classified, co_name, domain=domain)

            # ── Uploaded-data fallback ────────────────────────────────────────
            # If web scraping found no (or thin) BOD/EM data, promote the most
            # senior people from the uploaded CSV into those panels so the chart
            # is never left with empty leadership sections.
            promoted = promote_uploaded_to_leadership(dag, classified, co_name)
            if promoted:
                _log.info("Uploaded-fallback: %d people promoted to BOD/EM for '%s'",
                          promoted, co_name)

            _db.upsert_dag(dag)
            _log.info("Background enrichment complete for '%s'", co_name)
        except Exception as exc:
            _log.warning("Leadership enrichment failed for '%s': %s\n%s",
                         co_name, exc, traceback.format_exc())
        finally:
            _enrichment_done = True   # always mark done, even on error / empty result

    if background_tasks is not None:
        background_tasks.add_task(_run_enrichment, _dag, _classified, company_name, _domain)
    else:
        # Fallback: run synchronously if no background task context available
        _run_enrichment(_dag, _classified, company_name, _domain)

    # ── Field coverage check (group-aware) ──────────────────────────────────
    # Vendors use many equivalent column names.  We check coverage by semantic
    # group, not by exact canonical name, so "full_name" satisfies "Name" even
    # though neither "FirstName" nor "LastName" is present.
    mapped_set = set(mapped_cols)

    has_name = (
        "FullName"    in mapped_set              # full_name / name / etc.
        or ("FirstName" in mapped_set and "LastName" in mapped_set)
    )
    has_title    = "Designation"  in mapped_set  # job_title / title / designation
    has_company  = "Company"      in mapped_set  # company_name / company / employer
    has_linkedin = "LinkedInURL"  in mapped_set  # linkedin_url / linkedinurl
    has_location = bool(mapped_set & {           # any of these satisfies "location"
        "Location", "city", "country_name", "country_code",
        "job_city", "job_country", "job_country_code",
    })

    missing: list[str] = []
    if not has_name:    missing.append("Name (FirstName+LastName or FullName)")
    if not has_title:   missing.append("Designation / JobTitle")
    if not has_company: missing.append("Company")
    # Location and LinkedInURL are strongly recommended but not blocking
    if not has_location:  missing.append("Location / city / country")
    if not has_linkedin:  missing.append("LinkedInURL")

    return {
        "status": "ok",
        "records_ingested":  len(records),
        "detected_columns":  detected_cols,
        "mapped_columns":    mapped_cols,
        "canonical_missing": missing,   # empty list = all key fields detected
        "industry":          _industry or "",
        "stats": _dag.stats(),
    }


@app.get("/leadership-ready")
async def leadership_ready():
    """
    Poll this after upload to detect when background BOD/EM enrichment is done.
    Returns board_count, exec_count, and the latest industry (updated by background task).
    Frontend polls every 10s; re-fetches /tree when counts > 0 or industry changed.
    """
    if _dag is None:
        return {"ready": False, "board_count": 0, "exec_count": 0, "industry": ""}
    board_count = 0
    exec_count  = 0
    for nid in _dag.G.nodes:
        attrs = _dag.G.nodes[nid]
        if attrs.get("node_type") != "person":
            continue
        meta = attrs.get("metadata", {})
        if meta.get("nlp_method") in ("llm_leadership_web", "llm_leadership_ai", "uploaded_data"):
            dept = meta.get("dept_primary", "")
            if "Board" in dept:
                board_count += 1
            else:
                exec_count += 1
    ready = board_count > 0 or exec_count > 0
    # Return latest industry from root node (may have been updated by background task)
    root_meta = _dag.G.nodes.get("root_global", {}).get("metadata", {})
    industry  = root_meta.get("industry", "")
    return {
        "ready":            ready,
        "board_count":      board_count,
        "exec_count":       exec_count,
        "industry":         industry,
        "enrichment_done":  _enrichment_done,  # True once background task finished
    }


@app.post("/load-demo")
async def load_demo():
    """Load the bundled test_data.json."""
    global _dag, _db, _classified_records
    data_path = Path(__file__).parent / "test_data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="test_data.json not found")

    with open(data_path) as f:
        records = json.load(f)

    _dag, _db, _classified, _domain, _industry = build_from_records(
        records, company_name="AutoPrime Motors"
    )
    _classified_records = _classified
    return {
        "status": "ok",
        "records_ingested": len(records),
        "stats": _dag.stats(),
    }


@app.get("/debug/classified")
def debug_classified():
    """Return NLP classification results per person — useful for diagnosing wrong dept mappings."""
    if not _classified_records:
        return {"error": "No data loaded. POST /upload first."}
    rows = []
    for r in _classified_records:
        rows.append({
            "name":          getattr(r, "full_name", ""),
            "title":         getattr(r, "designation", ""),
            "layer":         getattr(r, "layer", "?"),
            "dept_primary":  getattr(r, "dept_primary", ""),
            "dept_secondary": getattr(r, "dept_secondary", ""),
            "nlp_method":    getattr(r, "nlp_method", ""),
        })
    rows.sort(key=lambda x: (x["dept_primary"], x["layer"], x["name"]))
    return {"count": len(rows), "records": rows}


# ─────────────────────────────────────────────
# EXPORT  (full org chart + executives as CSV)
# ─────────────────────────────────────────────

_SENIORITY_LABELS: dict[int, str] = {
    0:  "Board of Management (G0)",
    1:  "C-Suite (G1)",
    2:  "Executive VP (G2)",
    3:  "SVP / Managing Director (G3)",
    4:  "VP / Head of (G4)",
    5:  "Senior Director / AVP (G5)",
    6:  "Director (G6)",
    7:  "Senior Manager (G7)",
    8:  "Manager (G8)",
    9:  "Senior / Lead / Staff (G9)",
    10: "Analyst / Specialist (G10)",
}


@app.get("/export")
def export_org_chart(fmt: str = Query("csv", description="csv or json")):
    """
    Export the full org chart — all person nodes with their details — as CSV or JSON.

    CSV columns:
      Name, Title, Category, Seniority Level, Region, Location, LinkedIn URL, Source
    """
    if not _dag_loaded():
        raise HTTPException(status_code=404, detail="No data loaded. POST /upload first.")
    dag, _ = _require_dag()

    # Collect company name from root node
    root_attrs  = dag.G.nodes.get("root_global", {})
    company     = root_attrs.get("label", "Organization")
    root_meta   = root_attrs.get("metadata", {})
    industry    = root_meta.get("industry", "")

    rows: list[dict] = []

    for nid in dag.G.nodes:
        attrs = dag.G.nodes[nid]
        if attrs.get("node_type") != "person":
            continue

        meta  = attrs.get("metadata", {})
        layer = attrs.get("layer", 9)
        dept  = meta.get("dept_primary", "")
        meth  = str(meta.get("nlp_method", ""))

        # Human-readable category
        if "Board" in dept:
            category = "Board of Directors"
        elif "Executive" in dept:
            category = "Executive Management"
        else:
            category = dept or "Other"

        # Human-readable source
        if "web" in meth:
            source = "Company Website"
        elif "llm_leadership" in meth:
            source = "AI Knowledge"
        else:
            source = "Uploaded Data"

        rows.append({
            "Name":             attrs.get("label", ""),
            "Title":            str(meta.get("designation", "") or ""),
            "Category":         category,
            "Department":       dept,
            "Seniority Level":  _SENIORITY_LABELS.get(layer, f"G{layer}"),
            "Region":           str(meta.get("region", "") or ""),
            "Location":         str(meta.get("location", "") or ""),
            "LinkedIn URL":     str(meta.get("linkedin_url", "") or ""),
            "Source":           source,
        })

    # Sort: category order (BOD → EM → everything else), then seniority, then name
    _cat_order = {"Board of Directors": 0, "Executive Management": 1}
    rows.sort(key=lambda r: (
        _cat_order.get(r["Category"], 2),
        r["Department"],
        int("".join(filter(str.isdigit, r["Seniority Level"][:3])) or 9),
        r["Name"],
    ))

    if fmt == "json":
        return {
            "company": company,
            "industry": industry,
            "total_people": len(rows),
            "people": rows,
        }

    # ── CSV output ────────────────────────────────────────────────────────
    from datetime import datetime
    filename = f"{company.replace(' ', '_')}_org_chart_{datetime.now().strftime('%Y%m%d')}.csv"

    def _generate_csv():
        buf = io.StringIO()
        # Write BOM so Excel opens UTF-8 correctly
        buf.write("﻿")
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        yield buf.getvalue()

    return StreamingResponse(
        _generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/pptx")
def export_pptx():
    """
    Export the full org chart as a PowerPoint presentation.
    One slide per top-level department; people arranged in seniority-band rows.
    """
    if not _dag_loaded():
        raise HTTPException(status_code=404, detail="No data loaded. POST /upload first.")
    dag, _ = _require_dag()

    # Company metadata from root node
    root_attrs = dag.G.nodes.get("root_global", {})
    company    = root_attrs.get("label", "Organization")
    root_meta  = root_attrs.get("metadata", {})
    industry   = root_meta.get("industry", "")

    # ── Helpers ───────────────────────────────────────────────────────────
    from collections import deque as _deque

    _DEPT_TYPES = {"dept_primary", "dept_secondary", "dept_tertiary"}
    _SUB_TYPES  = {"dept_secondary", "dept_tertiary"}
    _PALETTE_P  = [
        (0x2c,0x5f,0x8a),(0x1b,0x7a,0x53),(0x7a,0x1f,0x96),
        (0xb0,0x26,0x26),(0xc9,0x70,0x1e),(0x10,0x6b,0x84),
    ]

    def _all_people_in(nid: str, stop_at_primary: bool = True) -> list[dict]:
        """DFS: collect every person node in subtree (stops at dept_primary boundary)."""
        out: list[dict] = []
        seen: set[str]  = set()
        def _dfs(n: str, depth: int = 0) -> None:
            if n in seen:
                return
            seen.add(n)
            a  = dag.G.nodes.get(n, {})
            nt = a.get("node_type", "")
            if nt == "person":
                out.append(dict(a))
                return
            if stop_at_primary and depth > 0 and nt == "dept_primary":
                return
            for c in dag.G.successors(n):
                _dfs(c, depth + 1)
        _dfs(nid)
        out.sort(key=lambda p: (p.get("layer", 99), p.get("label", "")))
        return out

    def _direct_people(nid: str) -> list[dict]:
        """Only immediate person-children of a node."""
        out = []
        for c in dag.G.successors(nid):
            a = dag.G.nodes.get(c, {})
            if a.get("node_type") == "person":
                out.append(dict(a))
        out.sort(key=lambda p: (p.get("layer", 99), p.get("label", "")))
        return out

    # ── Collect dept_primary nodes (BFS from root) ────────────────────────
    dept_nodes:  list[str] = []
    visited_bfs: set[str]  = set()
    q = _deque(["root_global"])
    while q:
        nid = q.popleft()
        if nid in visited_bfs:
            continue
        visited_bfs.add(nid)
        nt = dag.G.nodes.get(nid, {}).get("node_type", "")
        if nt == "dept_primary":
            dept_nodes.append(nid)
        for c in dag.G.successors(nid):
            q.append(c)

    # Fallback: if no dept_primary, use any top-level dept under root
    if not dept_nodes:
        for c in dag.G.successors("root_global"):
            if dag.G.nodes.get(c, {}).get("node_type", "") in _DEPT_TYPES:
                dept_nodes.append(c)

    # ── Build column structure for each dept ──────────────────────────────
    # MAX_PER_COL matches the new compact CARD_H=0.82" layout:
    #   available height below col-head ≈ 4.07"
    #   members per col = floor(4.07 / (0.82+0.06)) = 4
    # Sub-depts with more than MAX_PER_COL members get overflow columns
    # (head=None, members=remainder) so ALL people are included.
    MAX_PER_COL = 4

    def _overflow_columns(head, all_members, accent_rgb):
        """Split members into MAX_PER_COL-sized columns; first has head."""
        cols = []
        # First column: the named head + first MAX_PER_COL members
        cols.append({
            "head":       head,
            "members":    all_members[:MAX_PER_COL],
            "accent_rgb": accent_rgb,
        })
        # Overflow columns: no head (continuation), rest of members
        for start in range(MAX_PER_COL, len(all_members), MAX_PER_COL):
            cols.append({
                "head":       None,   # continuation — pptx_export skips head card
                "members":    all_members[start : start + MAX_PER_COL],
                "accent_rgb": accent_rgb,
            })
        return cols

    def _build_columns_for_dept(dept_id: str) -> tuple[dict | None, list[dict], int]:
        """
        Returns (dept_head, columns, headcount).
        Each column: {head, members, accent_rgb}
        head=None means continuation column (no column-head card drawn).
        """
        dept_color = dag.G.nodes.get(dept_id, {}).get("color", "#3491E8")
        dept_rgb   = tuple(int(dept_color.lstrip("#")[i:i+2], 16) for i in (0,2,4)) \
                     if len(dept_color.lstrip("#")) == 6 else (0x3d, 0x51, 0x68)

        all_ppl    = _all_people_in(dept_id)
        headcount  = len(all_ppl)
        if not all_ppl:
            return None, [], 0

        dept_head  = all_ppl[0]

        # Try sub-dept nodes first (natural columns)
        sub_dept_ids = [c for c in dag.G.successors(dept_id)
                        if dag.G.nodes.get(c, {}).get("node_type") in _SUB_TYPES]

        columns: list[dict] = []

        if sub_dept_ids:
            sd_nids: set[str] = set()   # track every node ID covered by sub-depts
            for idx, sd_id in enumerate(sub_dept_ids):
                sd_ppl   = _all_people_in(sd_id, stop_at_primary=False)
                if not sd_ppl:
                    continue
                for p in sd_ppl:
                    sd_nids.add(p.get("node_id", ""))
                sd_color = dag.G.nodes.get(sd_id, {}).get("color", dept_color)
                sd_rgb   = tuple(int(sd_color.lstrip("#")[i:i+2], 16) for i in (0,2,4)) \
                           if len(sd_color.lstrip("#")) == 6 else _PALETTE_P[idx % 6]
                if sd_rgb == dept_rgb:
                    sd_rgb = _PALETTE_P[idx % 6]
                # Include ALL members — overflow into continuation columns
                columns.extend(_overflow_columns(sd_ppl[0], sd_ppl[1:], sd_rgb))

            # ── Orphan people: in dept but not in any sub-dept ──────────────
            # (They sit directly under the dept_primary or its ghost chain,
            #  not under a dept_secondary/tertiary node.)
            dept_head_nid = dept_head.get("node_id", "") if dept_head else ""
            orphans = [p for p in all_ppl
                       if p.get("node_id", "") not in sd_nids
                       and p.get("node_id", "") != dept_head_nid]
            if orphans:
                acc = _PALETTE_P[len(sub_dept_ids) % 6]
                columns.extend(_overflow_columns(orphans[0], orphans[1:], acc))
        else:
            # No sub-depts: distribute remaining people as implied columns
            rest = all_ppl[1:]  # exclude dept head
            if not rest:
                return dept_head, [], headcount

            # Group by layer
            layer_groups: dict[int, list[dict]] = {}
            for p in rest:
                layer_groups.setdefault(p.get("layer", 9), []).append(p)
            layers = sorted(layer_groups.keys())

            if len(layers) >= 2:
                # First sub-layer = column heads; remaining = their stacked members
                col_heads = layer_groups[layers[0]]
                deeper    = []
                for L in layers[1:]:
                    deeper.extend(layer_groups[L])
                n_heads = len(col_heads)
                for i, ch in enumerate(col_heads):
                    start = (i * len(deeper)) // n_heads
                    end   = ((i+1) * len(deeper)) // n_heads
                    acc   = _PALETTE_P[i % 6]
                    columns.extend(_overflow_columns(ch, deeper[start:end], acc))
            else:
                # All same layer → one-person columns (all included)
                for i, p in enumerate(rest):
                    columns.append({
                        "head":       p,
                        "members":    [],
                        "accent_rgb": _PALETTE_P[i % 6],
                    })

        return dept_head, columns, headcount

    depts: list[dict] = []
    for dept_id in dept_nodes:
        attrs  = dag.G.nodes.get(dept_id, {})
        label  = attrs.get("label", dept_id)
        color  = attrs.get("color", "#3491E8")
        head, columns, hc = _build_columns_for_dept(dept_id)
        if hc == 0:
            continue
        depts.append({
            "label":     label,
            "color":     color,
            "headcount": hc,
            "head":      head,
            "columns":   columns,
        })

    if not depts:
        raise HTTPException(status_code=404,
                            detail="No people found. Upload a dataset first.")

    from pptx_export import build_pptx as _build_pptx
    pptx_bytes = _build_pptx(company, industry, depts)

    from datetime import datetime as _dt
    safe_name = company.replace(" ", "_").replace("/", "-")
    filename  = f"{safe_name}_org_chart_{_dt.now().strftime('%Y%m%d')}.pptx"

    return StreamingResponse(
        iter([pptx_bytes]),
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
def get_tree(
    root: str = Query("root_global"),
    max_depth: int = Query(20),
    dept_only: bool = Query(False),
):
    """
    Return nested tree JSON rooted at `root`.

    dept_only=true: strips person/ghost nodes from the response and adds a
    ``headcount`` key to every dept node with the total person count in that
    subtree.  Use this for large datasets (>10K people) to keep the payload
    small — people are loaded on demand via /executives.
    """
    if not _dag_loaded():
        return {"loaded": False, "id": root, "children": []}
    dag, _ = _require_dag()
    tree = dag.get_subtree(root, max_depth=max_depth, dept_only=dept_only)
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
    """Return the canonical department taxonomy used by the classifier."""
    from classifier import (
        DEPT_BOARD, DEPT_EXEC, DEPT_FIN, DEPT_HR, DEPT_LRC, DEPT_IT,
        DEPT_ENG, DEPT_RD, DEPT_PM, DEPT_MKT, DEPT_SALES, DEPT_CS,
        DEPT_OPS, DEPT_STR, DEPT_FAC, DEPT_COMM, DEPT_SUS,
        DEPT_IB, DEPT_ST, DEPT_WM, DEPT_IM, DEPT_ACT, DEPT_UW, DEPT_CLM,
        DEPT_SC, DEPT_MFG, DEPT_PRC,
    )
    core = [
        DEPT_BOARD, DEPT_EXEC, DEPT_FIN, DEPT_HR, DEPT_LRC,
        DEPT_IT, DEPT_ENG, DEPT_RD, DEPT_PM, DEPT_MKT,
        DEPT_SALES, DEPT_CS, DEPT_OPS, DEPT_STR, DEPT_FAC,
        DEPT_COMM, DEPT_SUS,
    ]
    industry_specific = [
        DEPT_IB, DEPT_ST, DEPT_WM, DEPT_IM,
        DEPT_ACT, DEPT_UW, DEPT_CLM,
        DEPT_SC, DEPT_MFG, DEPT_PRC,
    ]
    return {
        "departments": {
            "core": core,
            "industry_specific": industry_specific,
        }
    }


@app.post("/reset")
async def reset_data():
    """Clear all loaded data and return to idle state."""
    global _dag, _db, _classified_records
    _dag = None
    _db  = None
    _classified_records = []
    return {"status": "reset"}


@app.get("/executives")
def get_executives(
    dept_id: str = Query(...),
    offset: int = Query(0, ge=0),
    limit:  int = Query(200, ge=1, le=5000),
):
    """
    Return person nodes in the subtree rooted at dept_id, sorted by seniority
    (layer asc) then name.

    Pagination: offset + limit (default limit=200).  Response includes
    ``total`` so the client can show "showing N of M" and fetch more pages.
    People are sorted most-senior first, so the first page always contains
    the highest-layer executives.
    """
    if not _dag_loaded():
        return {"loaded": False, "executives": [], "count": 0, "total": 0}
    dag, _ = _require_dag()
    if dept_id not in dag.G:
        raise HTTPException(status_code=404, detail=f"Node '{dept_id}' not found.")

    people: list[dict] = []

    def collect(nid: str, visited: set, depth: int = 0):
        if nid in visited:
            return
        visited.add(nid)
        attrs = dict(dag.G.nodes.get(nid, {}))
        node_type = attrs.get("node_type", "")
        if node_type == "person":
            people.append(attrs)
        # Stop at child dept_primary boundaries — prevents BOD from including
        # Executive Management people (EM sits under BOD in the DAG).
        if depth > 0 and node_type == "dept_primary":
            return
        for child in dag.G.successors(nid):
            collect(child, visited, depth + 1)

    collect(dept_id, set())

    people.sort(key=lambda p: (p.get("layer", 99), p.get("label", "")))
    total = len(people)
    page  = people[offset : offset + limit]
    return {
        "executives": page,
        "count":  len(page),
        "total":  total,
        "offset": offset,
        "limit":  limit,
    }


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
# DEBUG ENDPOINT — remove after diagnosis
# ─────────────────────────────────────────────
@app.get("/ping-llm")
async def ping_llm():
    """Fast diagnostic: env vars + minimal Claude API call + Apify token check. Returns in <15s."""
    import os, time
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    parallel_key    = os.environ.get("PARALLEL_API_KEY", "")
    jina_key        = os.environ.get("JINA_API_KEY", "")
    apify_key       = os.environ.get("APIFY_API_TOKEN", "")
    scraper_api_url = os.environ.get("SCRAPER_API_URL", "")

    claude_result = {"ok": False, "error": "", "model": "", "response": ""}
    if anthropic_key:
        try:
            import anthropic as _ant
            t0 = time.monotonic()
            _client = _ant.Anthropic(api_key=anthropic_key)
            # Try the primary model first, fallback to known-good
            for model_id in ["claude-haiku-4-5-20251001", "claude-3-5-haiku-20241022"]:
                try:
                    _resp = _client.messages.create(
                        model=model_id,
                        max_tokens=20,
                        messages=[{"role": "user", "content": "Say: pong"}],
                    )
                    claude_result = {
                        "ok": True,
                        "error": "",
                        "model": model_id,
                        "response": _resp.content[0].text.strip(),
                        "elapsed_s": round(time.monotonic() - t0, 2),
                    }
                    break
                except Exception as _me:
                    claude_result["error"] += f"[{model_id}]: {_me} | "
        except Exception as exc:
            claude_result["error"] = str(exc)
    else:
        claude_result["error"] = "ANTHROPIC_API_KEY not set"

    # Quick Wikipedia check (no Parallel.AI — fast)
    wiki_chars = 0
    try:
        from llm_fallback import _scrape_wikipedia
        wiki_text  = _scrape_wikipedia("Wells Fargo")
        wiki_chars = len(wiki_text)
    except Exception as _we:
        wiki_chars = -1

    # Apify token validation — GET /v2/users/me (no actor run, instant)
    apify_result = {"token_set": bool(apify_key), "ok": False, "user": "", "error": ""}
    if apify_key:
        try:
            import httpx as _hx
            _ar = _hx.get(
                "https://api.apify.com/v2/users/me",
                params={"token": apify_key},
                timeout=8,
            )
            if _ar.status_code == 200:
                _ud = _ar.json().get("data", {})
                apify_result["ok"]   = True
                apify_result["user"] = _ud.get("username", _ud.get("id", ""))
                apify_result["plan"] = _ud.get("plan", {}).get("id", "")
            else:
                apify_result["error"] = f"HTTP {_ar.status_code}: {_ar.text[:120]}"
        except Exception as _ae:
            apify_result["error"] = str(_ae)
    else:
        apify_result["error"] = "APIFY_API_TOKEN not set"

    return {
        "env": {
            "ANTHROPIC_API_KEY": bool(anthropic_key),
            "PARALLEL_API_KEY":  bool(parallel_key),
            "JINA_API_KEY":      bool(jina_key),
            "APIFY_API_TOKEN":   bool(apify_key),
            "SCRAPER_API_URL":   bool(scraper_api_url),
        },
        "claude":   claude_result,
        "apify":    apify_result,
        "wikipedia_chars_wells_fargo": wiki_chars,
    }


@app.get("/test-knowledge")
async def test_knowledge(company: str = "Wells Fargo"):
    """Fast (<5s): test Claude knowledge fallback ONLY — no web scraping."""
    import os
    from llm_fallback import _call_claude, _SYSTEM_FROM_KNOWLEDGE
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return {"error": "ANTHROPIC_API_KEY not set", "result": {}}
    result = _call_claude(
        system=_SYSTEM_FROM_KNOWLEDGE,
        user_msg=f"Company: {company}",
        label=f"{company} [knowledge-test]",
    )
    return {
        "company": company,
        "board_count": len(result.get("board", [])),
        "exec_count": len(result.get("executives", [])),
        "result": result,
    }


@app.get("/test-apify")
async def test_apify(company: str = "Wells Fargo", domain: str = "wellsfargo.com"):
    """
    Test Apify scraper end-to-end with full diagnostics.
    Takes 60-120s. Use /ping-llm first to confirm token is valid.
    Example: /test-apify?company=Wells+Fargo&domain=wellsfargo.com
             /test-apify?company=Apple&domain=apple.com
    """
    import os, time
    import httpx
    from llm_fallback import (
        _apify_run, _discover_via_nav,
        _APIFY_LEADERSHIP_PATHS, _STRONG_LEADERSHIP_PATH_KW,
        _APIFY_ACTOR, _APIFY_BASE, _APIFY_MAX_PAGES,
    )

    apify_key = os.environ.get("APIFY_API_TOKEN", "")
    if not apify_key:
        return {"error": "APIFY_API_TOKEN not set"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    deadline = time.monotonic() + 12

    # ── URL discovery (mirrors _apify_fetch_leadership logic exactly) ──────────
    t0 = time.monotonic()
    nav_urls = _discover_via_nav(domain, headers, time.monotonic() + 8)
    discovery_s = round(time.monotonic() - t0, 2)

    seen: set[str] = set()
    start_urls: list[str] = []
    # Priority 1: nav URLs with strong leadership path signal
    for u in nav_urls:
        if any(kw in u.lower() for kw in _STRONG_LEADERSHIP_PATH_KW):
            if u not in seen:
                seen.add(u); start_urls.append(u)
    # Priority 2: curated specific paths (www-only to maximise unique paths)
    for path in _APIFY_LEADERSHIP_PATHS:
        u = f"https://www.{domain}{path}"
        if u not in seen:
            seen.add(u); start_urls.append(u)
    start_urls = start_urls[:_APIFY_MAX_PAGES]

    # ── Apify: submit once, poll inline, fetch dataset ───────────────────────
    t1 = time.monotonic()
    _APIFY_B  = "https://api.apify.com/v2"
    _ACTOR    = "apify~website-content-crawler"
    _POLL_INT = 8          # seconds between status polls
    _TIMEOUT  = 180        # max seconds to wait for run
    _SIGNAL   = {"director","chairman","chief","ceo","president","officer",
                 "executive","board","governance","management","leadership","trustee"}

    apify_diag: dict = {"phase": "submit"}
    run_id = dataset_id = None
    raw_items: list[dict] = []

    try:
        _input = {
            "startUrls":   [{"url": u} for u in start_urls[:6]],
            "crawlerType": "playwright:chrome",
            "maxCrawlDepth":  0,
            "maxCrawlPages":  6,
            "outputFormats":  ["markdown"],
            "htmlTransformer": "readableText",
        }
        _sr = httpx.post(
            f"{_APIFY_B}/acts/{_ACTOR}/runs",
            params={"token": apify_key},
            json=_input,
            timeout=20,
        )
        apify_diag["submit_status"] = _sr.status_code
        apify_diag["submit_preview"] = _sr.text[:500]
        if not _sr.is_success:
            apify_diag["phase"] = "submit_failed"
        else:
            _rd = _sr.json()["data"]
            run_id     = _rd["id"]
            dataset_id = _rd["defaultDatasetId"]
            apify_diag["run_id"]     = run_id
            apify_diag["dataset_id"] = dataset_id
            apify_diag["phase"]      = "polling"

            # ── Poll until SUCCEEDED / failed / timeout ───────────────────────
            _deadline = time.monotonic() + _TIMEOUT
            _poll_log: list[dict] = []
            while time.monotonic() < _deadline:
                time.sleep(_POLL_INT)
                _ps = httpx.get(
                    f"{_APIFY_B}/acts/{_ACTOR}/runs/{run_id}",
                    params={"token": apify_key}, timeout=15,
                )
                _status = _ps.json()["data"]["status"] if _ps.is_success else "POLL_ERR"
                _poll_log.append({"elapsed_s": round(time.monotonic()-t1,1), "status": _status})
                if _status == "SUCCEEDED":
                    apify_diag["phase"] = "fetching"
                    break
                if _status in ("FAILED","ABORTED","TIMED-OUT","POLL_ERR"):
                    apify_diag["phase"] = f"run_{_status.lower()}"
                    apify_diag["poll_log"] = _poll_log
                    break
            else:
                apify_diag["phase"] = "timeout"

            apify_diag["poll_log"] = _poll_log

            # ── Fetch dataset items ───────────────────────────────────────────
            if apify_diag["phase"] == "fetching":
                _di = httpx.get(
                    f"{_APIFY_B}/datasets/{dataset_id}/items",
                    params={"token": apify_key, "format": "json",
                            "limit": 10, "fields": "url,markdown,text"},
                    timeout=30,
                )
                apify_diag["dataset_status"] = _di.status_code
                if _di.is_success:
                    raw_items = _di.json()
                    apify_diag["phase"] = "done"
                else:
                    apify_diag["dataset_preview"] = _di.text[:300]
                    apify_diag["phase"] = "fetch_failed"

    except Exception as _e:
        apify_diag["error"] = str(_e)

    apify_s = round(time.monotonic() - t1, 2)

    # Per-item summary
    item_summary = []
    for item in raw_items:
        text = (item.get("markdown") or item.get("text") or "").strip()
        has_signal = any(kw in text.lower() for kw in _SIGNAL)
        item_summary.append({
            "url":         item.get("url", ""),
            "chars":       len(text),
            "has_leadership_signal": has_signal,
            "preview":     text[:300] if text else "",
        })

    kept = [i for i in item_summary if i["has_leadership_signal"] and i["chars"] >= 200]

    return {
        "company":   company,
        "domain":    domain,
        "discovery": {
            "nav_urls_all":           nav_urls[:10],
            "nav_urls_with_signal":   [u for u in nav_urls if any(kw in u.lower() for kw in _STRONG_LEADERSHIP_PATH_KW)],
            "start_urls_submitted":   start_urls,
            "elapsed_s":              discovery_s,
        },
        "apify": {
            "raw_items_returned":         len(raw_items),
            "items_with_leadership_signal": len(kept),
            "elapsed_s":                  apify_s,
            "diag":                       apify_diag,
            "items":                      item_summary,
        },
    }


@app.get("/debug-leadership")
async def debug_leadership(company: str = "Wells Fargo", domain: str = "wellsfargo.com"):
    """
    Directly test llm_fetch_leadership and return raw result + diagnostics.
    Use: /debug-leadership?company=Wells+Fargo&domain=wellsfargo.com
    NOTE: takes 90-120 s due to Parallel.AI — use /ping-llm for fast env check.
    """
    import os, time
    from llm_fallback import llm_fetch_leadership, _scrape_wikipedia, _ddg_leadership_snippets
    t0 = time.monotonic()
    wiki = _scrape_wikipedia(company)
    ddg  = _ddg_leadership_snippets(company)
    t1 = time.monotonic()
    result = llm_fetch_leadership(company, domain=domain)
    t2 = time.monotonic()
    return {
        "company": company,
        "domain": domain,
        "env": {
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "PARALLEL_API_KEY":  bool(os.environ.get("PARALLEL_API_KEY")),
            "JINA_API_KEY":      bool(os.environ.get("JINA_API_KEY")),
        },
        "source_chars": {
            "wikipedia": len(wiki),
            "ddg":       len(ddg),
        },
        "wiki_preview": wiki[:300] if wiki else "",
        "timing_seconds": {
            "sources": round(t1 - t0, 1),
            "total":   round(t2 - t0, 1),
        },
        "result": result,
    }


# ─────────────────────────────────────────────
# CHANGE REPORTS  (user-submitted corrections)
# ─────────────────────────────────────────────

# In-memory store (survives the process lifetime; lost on restart).
# For persistence, swap with a DB write or webhook call.
_CHANGE_REPORTS: list[dict] = []


class ChangeReportPayload(BaseModel):
    type:        str             # no_longer_here | wrong_dept | wrong_hierarchy | new_executive | other
    companyName: str
    personName:  Optional[str] = None
    personTitle: Optional[str] = None
    currentDept: Optional[str] = None
    linkedInUrl: Optional[str] = None
    notes:       Optional[str] = None
    timestamp:   Optional[int] = None


@app.post("/report-change")
async def report_change(payload: ChangeReportPayload):
    """Accept a user-reported correction to the org chart."""
    entry = payload.dict()
    entry["id"]          = str(uuid.uuid4())
    entry["received_at"] = datetime.now(timezone.utc).isoformat()
    _CHANGE_REPORTS.append(entry)
    import logging as _lg
    _lg.getLogger(__name__).info("Change report received: %s", json.dumps(entry))
    return {"ok": True, "id": entry["id"]}


@app.get("/change-reports")
async def get_change_reports():
    """Return all received change reports (admin / review endpoint)."""
    return {"count": len(_CHANGE_REPORTS), "reports": _CHANGE_REPORTS}


@app.get("/company")
async def get_loaded_company():
    """Return the company currently loaded in the backend's in-memory state.

    Frontend calls this before PPTX export to confirm the backend holds the
    same dataset as what the user is viewing (prevents exporting stale/wrong data).
    """
    if not _dag_loaded():
        return {"loaded": False, "company": None, "people": 0}
    dag, _ = _require_dag()
    root   = dag.G.nodes.get("root_global", {})
    company = root.get("label") or "Unknown"
    people  = sum(1 for _, a in dag.G.nodes(data=True) if a.get("node_type") == "person")
    return {"loaded": True, "company": company, "people": people}


# ─────────────────────────────────────────────
# FRONTEND STATIC FILES
# Mount the built React app so the backend serves everything from one port.
# API routes defined above take priority; this catches everything else.
# ─────────────────────────────────────────────
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="frontend")


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8769, reload=True)
