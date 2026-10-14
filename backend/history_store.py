"""
Durable, server-wide history of completed org-chart generations.

Backed by Upstash Redis — what "Vercel KV" became: Vercel retired its own KV
in December 2024 and provisions Upstash Redis through the Marketplace instead.
It is reached over its HTTPS REST API, so the backend (on Render) needs no
database driver, no region pairing and no connection pool.

This replaced a Render Postgres that silently disappeared: Render's free
Postgres expires after 30 days, DATABASE_URL kept pointing at a host that no
longer existed, and every failure here was a no-op, so an empty history looked
exactly like "no charts yet". Failures are now recorded and reported by
status() / GET /history-status.

Configuration (either naming works — the Vercel integration sets KV_*, the
Upstash console shows UPSTASH_*):
    KV_REST_API_URL / KV_REST_API_TOKEN
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN

Layout (all keys under "organogram:" so the store can be shared):
    organogram:run:{job_id}   JSON summary row (what the History page lists)
    organogram:snap:{job_id}  gzip+base64 JSON {nodes, edges} — reloads a chart
    organogram:runs           sorted set of job_ids, score = created_at epoch
"""

import base64
import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_PREFIX = "organogram:"
_RUNS = _PREFIX + "runs"
_TIMEOUT = 10

# The most recent failure, so /history-status can say WHY history is empty.
_last_error: dict = {}
_warned_unconfigured = False


def _config() -> tuple[str, str]:
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL") or ""
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
    return url.rstrip("/"), token


def _note_error(where: str, exc: Exception) -> None:
    msg = re.sub(r"https://\S+", "https://***", str(exc))          # no URLs/tokens out
    _last_error.clear()
    _last_error.update({"where": where, "error": f"{type(exc).__name__}: {msg[:300]}",
                        "at": time.time()})


def _configured() -> bool:
    global _warned_unconfigured
    url, token = _config()
    if url and token:
        return True
    if not _warned_unconfigured:
        logger.warning("KV_REST_API_URL / KV_REST_API_TOKEN not set — "
                       "org chart history will not be persisted.")
        _warned_unconfigured = True
    return False


def _pipeline(commands: list[list]) -> list:
    """Run Redis commands in one HTTPS round trip; returns each result."""
    import httpx
    url, token = _config()
    resp = httpx.post(f"{url}/pipeline", json=commands, timeout=_TIMEOUT,
                      headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    out = []
    for item in resp.json():
        if isinstance(item, dict) and item.get("error"):
            raise RuntimeError(f"Redis error: {item['error']}")
        out.append(item.get("result") if isinstance(item, dict) else item)
    return out


def _cmd(*args):
    return _pipeline([list(args)])[0]


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _pack(obj) -> str:
    return base64.b64encode(gzip.compress(json.dumps(obj).encode())).decode()


def _unpack(data: str):
    return json.loads(gzip.decompress(base64.b64decode(data)).decode())


# ── Public API (unchanged from the Postgres version) ─────────────────────────

def init_schema() -> None:
    """Call once at startup. Redis has no schema; this checks the store answers."""
    if not _configured():
        return
    try:
        _cmd("PING")
        logger.info("history_store: KV store reachable")
    except Exception as exc:
        logger.warning("history_store: KV store unreachable: %s", exc)
        _note_error("init", exc)


def record_job_created(job_id: str, company_name: str, source: str) -> None:
    if not _configured():
        return
    now = time.time()
    row = {"job_id": job_id, "company_name": company_name, "industry": None,
           "source": source, "status": "processing", "people_count": None,
           "dept_count": None, "board_count": None, "exec_count": None,
           "created_at": now, "completed_at": None, "input_tokens": 0,
           "output_tokens": 0, "cost_usd": 0.0, "gemini_pricing_is_estimate": False}
    try:
        # NX: never overwrite a run that already exists (a retried request).
        _pipeline([["SET", f"{_PREFIX}run:{job_id}", json.dumps(row), "NX"],
                   ["ZADD", _RUNS, "NX", str(now), job_id]])
    except Exception as exc:
        logger.warning("history_store: record_job_created failed for '%s': %s", job_id, exc)
        _note_error("record_job_created", exc)


def _counts(dag) -> dict:
    people = board = execs = depts = 0
    for _, attrs in dag.G.nodes(data=True):
        node_type = attrs.get("node_type")
        if node_type in ("dept_primary", "dept_secondary", "dept_tertiary"):
            depts += 1
            continue
        if node_type != "person":
            continue
        people += 1
        meta = attrs.get("metadata", {}) or {}
        if meta.get("nlp_method") in ("llm_leadership_web", "llm_leadership_ai", "uploaded_data"):
            if "Board" in (meta.get("dept_primary", "") or ""):
                board += 1
            else:
                execs += 1
    return {"people_count": people, "dept_count": depts,
            "board_count": board, "exec_count": execs}


def record_job_completed(job_id: str, dag, industry: str, usage_summary: dict) -> None:
    """dag: OrganogramDAG. usage_summary: the dict UsageTracker.summary() returns.

    Called again when LinkedIn profiles arrive after the chart, so it updates
    the row in place rather than requiring a fresh one.
    """
    if not _configured():
        return
    try:
        key = f"{_PREFIX}run:{job_id}"
        existing = _cmd("GET", key)
        row = json.loads(existing) if existing else {
            "job_id": job_id, "company_name": getattr(dag, "company_name", "") or "",
            "source": "upload", "created_at": time.time()}
        row.update(_counts(dag))
        row.update({
            "status": "ready",
            "industry": industry,
            "completed_at": time.time(),
            "input_tokens": usage_summary.get("input_tokens", 0),
            "output_tokens": usage_summary.get("output_tokens", 0),
            "cost_usd": float(usage_summary.get("cost_usd", 0) or 0),
            "gemini_pricing_is_estimate": bool(usage_summary.get("gemini_pricing_is_estimate")),
        })
        # Build node entries from (node_id, attrs) pairs rather than
        # dag.get_flat_nodes() — governance nodes don't embed their own
        # node_id inside attrs, only the networkx key carries it.
        nodes_out = [{**attrs, "node_id": node_id} for node_id, attrs in dag.G.nodes(data=True)]
        snapshot = _pack({"nodes": nodes_out, "edges": dag.get_edges()})
        _pipeline([["SET", key, json.dumps(row)],
                   ["SET", f"{_PREFIX}snap:{job_id}", snapshot],
                   ["ZADD", _RUNS, str(row.get("created_at") or time.time()), job_id]])
    except Exception as exc:
        logger.warning("history_store: record_job_completed failed for '%s': %s", job_id, exc)
        _note_error("record_job_completed", exc)


def delete_job(job_id: str) -> None:
    if not _configured():
        return
    try:
        _pipeline([["DEL", f"{_PREFIX}run:{job_id}", f"{_PREFIX}snap:{job_id}"],
                   ["ZREM", _RUNS, job_id]])
    except Exception as exc:
        logger.warning("history_store: delete_job failed for '%s': %s", job_id, exc)
        _note_error("delete_job", exc)


# How many recent runs to read before filtering — the filters (status, source,
# search) are applied in Python, which is fine at this app's volume.
_SCAN_LIMIT = 2000


def list_jobs(limit: int = 50, offset: int = 0, source: str = "", q: str = "") -> list[dict]:
    if not _configured():
        return []
    try:
        ids = _cmd("ZREVRANGE", _RUNS, "0", str(_SCAN_LIMIT - 1)) or []
        if not ids:
            return []
        raw = _cmd("MGET", *[f"{_PREFIX}run:{i}" for i in ids]) or []
        rows = []
        needle = (q or "").lower()
        for item in raw:
            if not item:
                continue
            row = json.loads(item)
            if row.get("status") != "ready":
                continue
            if source and row.get("source") != source:
                continue
            if needle and needle not in (row.get("company_name") or "").lower():
                continue
            rows.append(row)
        page = rows[offset: offset + limit]
        for r in page:
            r["created_at"] = _iso(r.get("created_at"))
            r["completed_at"] = _iso(r.get("completed_at"))
            r["cost_usd"] = float(r.get("cost_usd") or 0.0)
        return page
    except Exception as exc:
        logger.warning("history_store: list_jobs failed: %s", exc)
        _note_error("list_jobs", exc)
        return []


def load_snapshot(job_id: str) -> dict | None:
    """Returns {"company_name": ..., "nodes": [...], "edges": [...]} or None."""
    if not _configured():
        return None
    try:
        row_raw, snap = _pipeline([["GET", f"{_PREFIX}run:{job_id}"],
                                   ["GET", f"{_PREFIX}snap:{job_id}"]])
        if not snap:
            return None
        row = json.loads(row_raw) if row_raw else {}
        return {"company_name": row.get("company_name", ""), **_unpack(snap)}
    except Exception as exc:
        logger.warning("history_store: load_snapshot failed for '%s': %s", job_id, exc)
        _note_error("load_snapshot", exc)
        return None


def status() -> dict:
    """Whether Org History is being saved, and if not, why — no secrets."""
    out: dict = {"backend": "upstash-redis (Vercel KV)", "configured": _configured(),
                 "connected": False, "last_error": dict(_last_error) or None}
    if not out["configured"]:
        return out
    try:
        pong, total = _pipeline([["PING"], ["ZCARD", _RUNS]])
        out["connected"] = pong == "PONG"
        out["runs_total"] = int(total or 0)
        latest = _cmd("ZREVRANGE", _RUNS, "0", "0", "WITHSCORES") or []
        out["latest_created_at"] = _iso(float(latest[1])) if len(latest) >= 2 else None
    except Exception as exc:
        _note_error("status", exc)
        out["last_error"] = dict(_last_error)
    return out
