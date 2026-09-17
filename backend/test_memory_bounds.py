"""
Memory bounds for the job store and upload path.

Render was restarting organogram-engine on its memory limit. Measured cost is
~0.7 MB per resident 300-person job, and _JOBS had a 24h TTL but no cap on how
MANY jobs were resident, so a busy day accumulated hundreds of MB. The upload
path had no byte limit at all, and pandas holds roughly five copies of a file
while parsing it.

Run: python3 backend/test_memory_bounds.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


import api_server as A
from fastapi import HTTPException
from structural_engine import build_from_records

_TITLES = ["Chief Executive Officer", "VP Engineering", "Director of Sales", "Senior Manager"]


def _records(n):
    return [{"Full Name": f"Person Number {i}", "Designation": _TITLES[i % 4],
             "Company": "Acme Corporation", "Email": f"p{i}@acme.com",
             "LinkedIn URL": f"https://linkedin.com/in/p{i}",
             "Location": "London", "Country": "UK"} for i in range(n)]


def _make_session(name="Acme", n=5):
    dag, db, classified, _domain, _industry = build_from_records(
        _records(n), company_name=name, db_path=tempfile.mktemp(suffix=".db"))
    return A.JobSession(dag=dag, db=db, classified_records=classified,
                        enrichment_done=True, last_accessed_at=time.time(),
                        db_path="", company_name=name)


# ── Resident job cap ────────────────────────────────────────────────────────
_saved_jobs = dict(A._JOBS)
A._JOBS.clear()
try:
    for i in range(A._MAX_RESIDENT_JOBS + 25):
        A._JOBS[f"job{i}"] = _make_session()
        A._JOBS[f"job{i}"].last_accessed_at = time.time() + i   # newest last
        A._enforce_job_capacity()
    ok(len(A._JOBS) == A._MAX_RESIDENT_JOBS,
       f"jobs: resident count capped at {A._MAX_RESIDENT_JOBS} (got {len(A._JOBS)})")
    ok("job0" not in A._JOBS, "jobs: oldest evicted first")
    newest = f"job{A._MAX_RESIDENT_JOBS + 24}"
    ok(newest in A._JOBS, "jobs: most recent kept")

    # Under the cap nothing is touched.
    A._JOBS.clear()
    A._JOBS["only"] = _make_session()
    A._enforce_job_capacity()
    ok(len(A._JOBS) == 1, "jobs: under cap is a no-op")
finally:
    A._JOBS.clear()
    A._JOBS.update(_saved_jobs)

# ── Upload size limit ───────────────────────────────────────────────────────
ok(A._MAX_UPLOAD_BYTES > 0 and A._MAX_ROWS > 0, "upload: bounds configured")
ok(A._MAX_ROWS >= 10_000,
   f"upload: row cap {A._MAX_ROWS} still >= 30x the 300-executive use case")

A._check_upload_size(b"x" * 1024)          # small file: no raise
ok(True, "upload: small file accepted")

try:
    A._check_upload_size(b"x" * (A._MAX_UPLOAD_BYTES + 1))
    ok(False, "upload: oversized file rejected")
except HTTPException as exc:
    ok(exc.status_code == 413, "upload: oversized file rejected with 413")
    ok("limit" in str(exc.detail).lower(), "upload: rejection explains the limit")

# ── Enrichment company cap ──────────────────────────────────────────────────
import structural_engine as S
ok(S._MAX_ENRICH_COMPANIES >= 1, "enrich: company cap configured")
ok(S._MAX_ENRICH_COMPANIES <= 10,
   f"enrich: cap {S._MAX_ENRICH_COMPANIES} keeps one upload from launching dozens of crawls")

# ── Page size cap ───────────────────────────────────────────────────────────
import llm_fallback as L
ok(L._MAX_PAGE_BYTES >= 256 * 1024, "page: cap large enough for real corporate pages")
ok(L._MAX_PAGE_BYTES <= 8 * 1024 * 1024, "page: cap small enough to bound a spike")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
