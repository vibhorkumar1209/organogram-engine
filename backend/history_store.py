"""
Durable, server-wide history of completed org-chart generations.

Backed by Postgres (DATABASE_URL) so history survives a Render redeploy —
unlike the per-job SQLite files in structural_engine.py, which only survive
a process restart (see JobSession/_JOBS in api_server.py). Every function
here degrades to a no-op when DATABASE_URL is unset or unreachable, the same
way ANTHROPIC_API_KEY/GEMINI_API_KEY already degrade for local dev instead
of crashing the app.

A new connection is opened and closed per call rather than held open —
simplest safe option given psycopg2 connections aren't meant to be shared
across concurrent requests, and call volume here is low (a couple of writes
per job, one list query per History page load).
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_warned_no_db = False


def _connect():
    """A new psycopg2 connection, or None if unavailable — callers must
    handle None by skipping the operation."""
    global _warned_no_db
    if not _DATABASE_URL:
        if not _warned_no_db:
            logger.warning("DATABASE_URL not set — org chart history will not be persisted.")
            _warned_no_db = True
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(_DATABASE_URL)
        conn.autocommit = True
        return conn
    except Exception as exc:
        logger.warning("history_store: could not connect to Postgres: %s", exc)
        return None


def init_schema() -> None:
    """Call once at startup. Safe to call repeatedly (IF NOT EXISTS)."""
    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chart_runs (
                    job_id            TEXT PRIMARY KEY,
                    company_name      TEXT NOT NULL,
                    industry          TEXT,
                    source            TEXT NOT NULL,
                    status            TEXT NOT NULL,
                    people_count      INT,
                    dept_count        INT,
                    board_count       INT,
                    exec_count        INT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                    completed_at      TIMESTAMPTZ,
                    input_tokens      INT DEFAULT 0,
                    output_tokens     INT DEFAULT 0,
                    cost_usd          NUMERIC(12,6) DEFAULT 0,
                    gemini_pricing_is_estimate BOOLEAN DEFAULT FALSE,
                    snapshot          JSONB
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_chart_runs_status_created
                    ON chart_runs (status, created_at DESC)
            """)
        logger.info("history_store: schema ready")
    except Exception as exc:
        logger.warning("history_store: schema init failed: %s", exc)
    finally:
        conn.close()


def record_job_created(job_id: str, company_name: str, source: str) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chart_runs (job_id, company_name, source, status)
                VALUES (%s, %s, %s, 'processing')
                ON CONFLICT (job_id) DO NOTHING
            """, (job_id, company_name, source))
    except Exception as exc:
        logger.warning("history_store: record_job_created failed for '%s': %s", job_id, exc)
    finally:
        conn.close()


def record_job_completed(job_id: str, dag, industry: str, usage_summary: dict) -> None:
    """dag: OrganogramDAG. usage_summary: the dict UsageTracker.summary() returns."""
    conn = _connect()
    if conn is None:
        return
    try:
        people_count = board_count = exec_count = dept_count = 0
        for _, attrs in dag.G.nodes(data=True):
            node_type = attrs.get("node_type")
            if node_type in ("dept_primary", "dept_secondary", "dept_tertiary"):
                dept_count += 1
                continue
            if node_type != "person":
                continue
            people_count += 1
            meta = attrs.get("metadata", {})
            if meta.get("nlp_method") in ("llm_leadership_web", "llm_leadership_ai", "uploaded_data"):
                if "Board" in meta.get("dept_primary", ""):
                    board_count += 1
                else:
                    exec_count += 1

        # Build node entries directly from (node_id, attrs) pairs rather than
        # dag.get_flat_nodes() — that method returns each node's attrs dict
        # as-is, and at least one node type (board-of-management governance
        # nodes) doesn't embed its own node_id inside attrs, only relying on
        # it being the networkx graph key. Mirror the same approach
        # OrganogramDB.upsert_dag/load_dag already use for exactly this reason.
        nodes_out = [{**attrs, "node_id": node_id} for node_id, attrs in dag.G.nodes(data=True)]
        snapshot = json.dumps({"nodes": nodes_out, "edges": dag.get_edges()})

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE chart_runs SET
                    status = 'ready',
                    industry = %s,
                    people_count = %s,
                    dept_count = %s,
                    board_count = %s,
                    exec_count = %s,
                    completed_at = now(),
                    input_tokens = %s,
                    output_tokens = %s,
                    cost_usd = %s,
                    gemini_pricing_is_estimate = %s,
                    snapshot = %s
                WHERE job_id = %s
            """, (
                industry, people_count, dept_count, board_count, exec_count,
                usage_summary.get("input_tokens", 0),
                usage_summary.get("output_tokens", 0),
                usage_summary.get("cost_usd", 0),
                usage_summary.get("gemini_pricing_is_estimate", False),
                snapshot,
                job_id,
            ))
    except Exception as exc:
        logger.warning("history_store: record_job_completed failed for '%s': %s", job_id, exc)
    finally:
        conn.close()


def delete_job(job_id: str) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chart_runs WHERE job_id = %s", (job_id,))
    except Exception as exc:
        logger.warning("history_store: delete_job failed for '%s': %s", job_id, exc)
    finally:
        conn.close()


def list_jobs(limit: int = 50, offset: int = 0, source: str = "", q: str = "") -> list[dict]:
    conn = _connect()
    if conn is None:
        return []
    try:
        clauses = ["status = 'ready'"]
        params: list = []
        if source:
            clauses.append("source = %s")
            params.append(source)
        if q:
            clauses.append("company_name ILIKE %s")
            params.append(f"%{q}%")
        where = " AND ".join(clauses)
        params.extend([limit, offset])
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT job_id, company_name, industry, source, status,
                       people_count, dept_count, board_count, exec_count,
                       created_at, completed_at, input_tokens, output_tokens,
                       cost_usd, gemini_pricing_is_estimate
                FROM chart_runs
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["created_at"]   = r["created_at"].isoformat() if r["created_at"] else None
            r["completed_at"] = r["completed_at"].isoformat() if r["completed_at"] else None
            r["cost_usd"]     = float(r["cost_usd"]) if r["cost_usd"] is not None else 0.0
        return rows
    except Exception as exc:
        logger.warning("history_store: list_jobs failed: %s", exc)
        return []
    finally:
        conn.close()


def load_snapshot(job_id: str) -> dict | None:
    """Returns {"company_name": ..., "nodes": [...], "edges": [...]} or None."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT company_name, snapshot FROM chart_runs
                WHERE job_id = %s AND snapshot IS NOT NULL
            """, (job_id,))
            row = cur.fetchone()
        if not row:
            return None
        company_name, snapshot = row
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        return {"company_name": company_name, **snapshot}
    except Exception as exc:
        logger.warning("history_store: load_snapshot failed for '%s': %s", job_id, exc)
        return None
    finally:
        conn.close()
