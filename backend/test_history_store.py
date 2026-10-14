"""
Org History on Upstash Redis (what Vercel KV became).

Production history silently stopped: the Render Postgres behind it expired
and was deleted, DATABASE_URL still pointed at the vanished host, and every
failure was a no-op — an empty history looked like "no charts yet". These run
the real app flow against an in-memory stand-in for the Upstash REST API.

Run: python3 backend/test_history_store.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


# ── An in-memory Upstash: the commands history_store uses ───────────────────
class FakeUpstash:
    def __init__(self):
        self.kv: dict = {}
        self.z: dict = {}
        self.calls = 0
        self.down = False

    def run(self, cmd):
        op, *a = cmd
        op = op.upper()
        if op == "PING":
            return "PONG"
        if op == "SET":
            key, val, *flags = a
            if "NX" in flags and key in self.kv:
                return None
            self.kv[key] = val
            return "OK"
        if op == "GET":
            return self.kv.get(a[0])
        if op == "MGET":
            return [self.kv.get(k) for k in a]
        if op == "DEL":
            return sum(1 for k in a if self.kv.pop(k, None) is not None)
        if op == "ZADD":
            key, *rest = a
            nx = rest and rest[0] == "NX"
            if nx:
                rest = rest[1:]
            score, member = float(rest[0]), rest[1]
            zs = self.z.setdefault(key, {})
            if nx and member in zs:
                return 0
            zs[member] = score
            return 1
        if op == "ZREM":
            return int(self.z.get(a[0], {}).pop(a[1], None) is not None)
        if op == "ZCARD":
            return len(self.z.get(a[0], {}))
        if op == "ZREVRANGE":
            key, start, stop, *flags = a
            items = sorted(self.z.get(key, {}).items(), key=lambda kv: -kv[1])
            items = items[int(start): int(stop) + 1]
            if "WITHSCORES" in flags:
                return [x for m, s in items for x in (m, str(s))]
            return [m for m, _ in items]
        raise AssertionError(f"unexpected command {cmd}")

    def pipeline(self, commands):
        self.calls += 1
        if self.down:
            raise ConnectionError("Name or service not known")
        return [self.run(c) for c in commands]


fake = FakeUpstash()
os.environ["KV_REST_API_URL"] = "https://fake.upstash.io"
os.environ["KV_REST_API_TOKEN"] = "secret-token"

import history_store as H
H._pipeline = fake.pipeline

from fastapi.testclient import TestClient
import api_server as A
os.environ["PARALLEL_API_KEY"] = ""
client = TestClient(A.app)

st = client.get("/history-status").json()
ok(st["configured"] and st["connected"] and st["runs_total"] == 0,
   f"status: configured, connected, empty ({st})")
ok("secret-token" not in str(st) and "fake.upstash.io" not in str(st), "status: no URL or token leaked")

# ── A chart is recorded, completed, listed ──────────────────────────────────
job = client.post("/load-demo").json()["job_id"]
runs = client.get("/history").json()["runs"]
ok([r["job_id"] for r in runs] == [job], "list: the demo chart appears in history")
r = runs[0]
ok(r["company_name"] == "AutoPrime Motors" and r["status"] == "ready" and r["people_count"] > 0,
   f"list: row carries company, status and counts ({r['people_count']} people)")
ok(isinstance(r["created_at"], str) and r["created_at"].endswith("+00:00"),
   "list: timestamps are ISO strings, as the frontend expects")
snap_raw = fake.kv[f"organogram:snap:{job}"]
ok(len(snap_raw) < 200_000, f"snapshot: stored compressed ({len(snap_raw):,} chars)")

# ── Survives losing the in-memory job (a redeploy) ──────────────────────────
A._evict_job(job)
import glob
for f in glob.glob(f"{A._job_db_path(job)}*"):
    os.remove(f)
tree = client.get("/tree", params={"job_id": job, "root": "root_global",
                                   "max_depth": 3, "dept_only": "true"})
ok(tree.status_code == 200 and tree.json().get("children"),
   "reload: a chart whose job and disk file are gone is rebuilt from KV")

# ── Filters ─────────────────────────────────────────────────────────────────
ok(client.get("/history", params={"source": "upload"}).json()["runs"] == [],
   "filter: source=upload excludes the demo")
ok(len(client.get("/history", params={"q": "autoprime"}).json()["runs"]) == 1,
   "filter: case-insensitive name search")

# A run still processing is not listed.
H.record_job_created("job-in-progress", "Halfway Inc", "upload")
ok(all(r["job_id"] != "job-in-progress" for r in client.get("/history").json()["runs"]),
   "list: unfinished runs are hidden")

# Completing twice (the LinkedIn step saves again) updates, never duplicates.
dag = A._JOBS[job].dag
H.record_job_completed(job, dag, "Automotive", {"cost_usd": 0.5})
runs = client.get("/history").json()["runs"]
ok(len(runs) == 1 and runs[0]["cost_usd"] == 0.5 and runs[0]["industry"] == "Automotive",
   "update: a second completion updates the same row")

# ── Delete ──────────────────────────────────────────────────────────────────
H.delete_job(job)
ok(client.get("/history").json()["runs"] == [] and f"organogram:snap:{job}" not in fake.kv,
   "delete: row, snapshot and index entry removed")

# ── Failures are reported, not silent ───────────────────────────────────────
fake.down = True
H.record_job_created("job-x", "Down Co", "upload")
st = client.get("/history-status").json()
ok(not st["connected"] and st["last_error"] and "Name or service not known" in st["last_error"]["error"],
   f"failure: status says why history isn't saving ({st.get('last_error')})")
fake.down = False

for k in ("KV_REST_API_URL", "KV_REST_API_TOKEN"):
    os.environ.pop(k)
os.environ["UPSTASH_REDIS_REST_URL"] = "https://fake.upstash.io"
os.environ["UPSTASH_REDIS_REST_TOKEN"] = "t"
ok(H._configured(), "config: Upstash's own variable names work too")
os.environ.pop("UPSTASH_REDIS_REST_URL"); os.environ.pop("UPSTASH_REDIS_REST_TOKEN")
ok(not H._configured() and client.get("/history-status").json()["configured"] is False,
   "config: unset -> reported as not configured")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
