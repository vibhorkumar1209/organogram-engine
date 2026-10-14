"""
Who leads Executive Management, real seniority, junior staff kept out, and an
empty board retried.

From live charts, October 2026:
  - Pyramid Consulting: the CEO ("Co-founder and Chief Executive Officer")
    was not shown leading Executive Management. Every executive was layer 1,
    and the panel's title check read "Co-" as co-CEO and rejected him. Ten
    "Executive Resourcing" recruiters were in Executive Management too.
  - NVIDIA: 0 board members on one run, 11 on the next — the roster is only
    on investor.nvidia.com (unreachable from Render), so it depended on one
    free-text search answer.

Run: python3 backend/test_hierarchy_and_board.py
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient
import api_server as A
os.environ["PARALLEL_API_KEY"] = ""          # .env is loaded by api_server; no paid calls
import structural_engine as S
import llm_fallback as F
from inference_logic import ClassifiedRecord

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


client = TestClient(A.app)

# ── Pyramid Consulting, as the leadership search returned it ────────────────
PYRAMID = {
    "_source": "web",
    "board": [{"name": "Bill Preston", "title": "Chairman & Managing Director"}],
    "executives": [
        {"name": "Sanjeev Tirath", "title": "Co-founder and Chief Executive Officer"},
        {"name": "Ramesh Maturu", "title": "President and Co-founder"},
        {"name": "Nevin Okay", "title": "President & Managing Partner"},
        {"name": "Namita Tirath", "title": "EVP Client Relations & Business Development"},
        {"name": "Brian Church", "title": "Executive Vice President US Staffing"},
        {"name": "Anand Kapoor", "title": "Senior Vice President Of Global Sales"},
        {"name": "Sunil Dhar", "title": "Vice President - Client Services"},
        {"name": "Janelle Polen", "title": "Director Of Business Development"},
        {"name": "Amit Raheja", "title": "Executive Resourcing"},
        {"name": "Jai Prakash", "title": "Executive-Resourcing"},
        {"name": "Habeeb Rahman", "title": "Resource Executive"},
    ],
    "senior_leadership": [],
}

# The junior filter runs inside the leadership search, on its final result.
cleaned = {k: [dict(p) for p in v] if isinstance(v, list) else v for k, v in PYRAMID.items()}
F._strip_internal_fields(cleaned)
names = [p["name"] for p in cleaned["executives"]]
ok(not {"Amit Raheja", "Jai Prakash", "Habeeb Rahman"} & set(names),
   f"junior: 'Executive Resourcing' recruiters are dropped (kept {len(names)})")
ok({"Sanjeev Tirath", "Brian Church", "Janelle Polen"} <= set(names),
   "junior: real executives, incl. 'Executive Vice President', are kept")
for title, junior in [("Sales Executive", True), ("HR Executive", True),
                      ("Executive Assistant to the CEO", True), ("Senior Associate", True),
                      ("Executive Director", False), ("Executive Chairman", False),
                      ("Chief Executive Officer", False), ("Assistant Vice President", False),
                      ("Managing Director", False), ("Principal Consultant", False)]:
    ok(F._is_junior_title(title) == junior, f"junior: {title!r} -> {junior}")

# Build the chart the way enrichment does, with the cleaned search result.
_orig = F.llm_fetch_leadership
F.llm_fetch_leadership = lambda *a, **k: {k2: ([dict(p) for p in v] if isinstance(v, list) else v)
                                         for k2, v in cleaned.items()}
job = client.post("/company-chart", json={"company_name": "Pyramid Consulting",
                                          "domain": "pyramidci.com",
                                          "hq_location": "Alpharetta, Georgia"}).json()["job_id"]
F.llm_fetch_leadership = _orig
dag = A._JOBS[job].dag

em = {}
for nid, a in dag.G.nodes(data=True):
    if a.get("node_type") == "person" and S._person_department(dag, nid) == S.EXEC_DEPT:
        em[a["label"]] = (nid, a)

apex = [n for n, (_, a) in em.items() if (a.get("metadata") or {}).get("is_apex")]
ok(apex == ["Sanjeev Tirath"], f"apex: the Co-founder and CEO leads Executive Management (got {apex})")
em_meta = dag.G.nodes[S._dept_node_id(S.EXEC_DEPT)].get("metadata", {})
ok(em_meta.get("head_name") == "Sanjeev Tirath",
   f"apex: the Executive Management card names its leader (got {em_meta.get('head_name')})")

layers = {n: a.get("layer") for n, (_, a) in em.items()}
ok(layers["Sanjeev Tirath"] == 1, "layers: CEO at 1")
ok(layers["Brian Church"] == 2 and layers["Anand Kapoor"] == 3 and layers["Sunil Dhar"] == 4
   and layers["Janelle Polen"] == 6,
   f"layers: EVP 2, SVP 3, VP 4, Director 6 — not all 1 (got {layers})")
ok(layers["Ramesh Maturu"] == 1 and layers["Nevin Okay"] == 1,
   "layers: presidents and founders stay in the top tier")

r = client.get("/leadership-ready", params={"job_id": job}).json()
ok(r["board_count"] == 1 and r["exec_count"] == 8,
   f"counts: 1 director, 8 executives (got {r['board_count']}, {r['exec_count']})")

# Only one apex, whatever the order or how many CEO-ish titles there are.
for title_set, expected in [
    (["President", "Chief Executive Officer", "Chairman and CEO"], "Chairman and CEO"),
    (["Dean", "Chief Operating Officer"], "Dean"),
    (["Chief Financial Officer", "SVP Sales"], None),
]:
    d = S.OrganogramDAG.__new__(S.OrganogramDAG)
    jid = client.post("/company-chart", json={"company_name": f"Apex Co {uuid.uuid4().hex[:4]}",
                                              "domain": "", "hq_location": ""}).json()["job_id"]
    d = A._JOBS[jid].dag
    for t in title_set:
        d.insert_person(ClassifiedRecord(
            id="llm_" + uuid.uuid4().hex[:8], full_name=f"P {t}", designation=t,
            company="X", linkedin_url="", location="", country="", sector="Private",
            region="Global HQ", layer=1, dept_primary=S.EXEC_DEPT, dept_secondary="",
            dept_tertiary="", nlp_confidence=0.9, nlp_industry="llm",
            nlp_method="llm_leadership_web"))
    S.split_executive_departments(d)
    got = [a.get("metadata", {}).get("designation") for _, a in d.G.nodes(data=True)
           if a.get("node_type") == "person" and (a.get("metadata") or {}).get("is_apex")]
    ok(got == ([expected] if expected else []),
       f"apex: one leader among {title_set} -> {expected} (got {got})")

# ── An empty board gets one board-only search ───────────────────────────────
calls = []


def _fake_retry(company, domain, location, api_key):
    calls.append(company)
    return {"board": [{"name": "Tench Coxe", "title": "Director"},
                      {"name": "Dawn Hudson", "title": "Director"}]}


F._gemini_board_retry = _fake_retry
F._gemini_discover_leadership_urls = lambda *a, **k: ([], "Jensen Huang, President and CEO of NVIDIA.")
F._fetch_static_leadership = lambda *a, **k: ""
F._gemini_synthesize_chunk = lambda *a, **k: {
    "board": [], "executives": [{"name": "Jensen Huang", "title": "President and CEO"}],
    "senior_leadership": []}
F._corroborate_canonical = lambda *a, **k: []
res = F._gemini_fetch_leadership("NVIDIA", "nvidia.com", "key", location="Santa Clara")
ok(calls == ["NVIDIA"], "board retry: runs when the search returned no board")
ok([p["name"] for p in res["board"]] == ["Tench Coxe", "Dawn Hudson"]
   and res["_harvest"]["stages"]["board_retry"] == 2,
   f"board retry: its directors join the result, and diagnostics say so ({res['_harvest']['stages']})")

# A board of one — the dual-role CEO — is not a board found.
calls.clear()
F._gemini_synthesize_chunk = lambda *a, **k: {
    "board": [{"name": "Jensen Huang", "title": "Founder, President and CEO"}],
    "executives": [{"name": "Jensen Huang", "title": "President and CEO"}], "senior_leadership": []}
res = F._gemini_fetch_leadership("NVIDIA", "nvidia.com", "key")
ok(calls == ["NVIDIA"] and len(res["board"]) == 3,
   f"board retry: runs when only the CEO is on the board, and merges (got {len(res['board'])})")

calls.clear()
F._gemini_synthesize_chunk = lambda *a, **k: {
    "board": [{"name": "Tench Coxe", "title": "Director"}, {"name": "Aarti Shah", "title": "Director"},
              {"name": "Dawn Hudson", "title": "Director"}],
    "executives": [{"name": "Jensen Huang", "title": "President and CEO"}], "senior_leadership": []}
F._gemini_fetch_leadership("NVIDIA", "nvidia.com", "key")
ok(calls == [], "board retry: not run when the board was already found")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
