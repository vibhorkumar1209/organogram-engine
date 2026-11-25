"""
Departments under Executive Management, and where added people land.

Rules (user, Nov 2026):
- Executive Management branches into main departments only; a part of one
  ("Customer Marketing", "Advertising & Promotion") is a sub-department inside it.
- A director's job elsewhere is not an executive role here.
- A roster added to a department: confident matches for another department go
  there only; confident matches for this one land in the sub-department /
  sub-team their title names; uncertain ones stay here, flagged for review.
- Designation, headline, role, about and responsibilities all inform it.

Run: python3 backend/test_department_placement.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient
import api_server as A
os.environ["PARALLEL_API_KEY"] = ""          # .env is loaded by api_server; no paid calls
import structural_engine as S
import llm_fallback as F

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


client = TestClient(A.app)

LEADERS = {
    "_source": "web",
    "board": [
        {"name": "Ada Chair", "title": "Chair of the Board"},
        {"name": "Indra Elsewhere", "title": "Former Chair and CEO of PepsiCo, Inc."},
        {"name": "Brad Dean", "title": "President of Marshall University"},
    ],
    "executives": [
        {"name": "Cara Ceo", "title": "Chief Executive Officer"},
        {"name": "Fin Ance", "title": "Chief Financial Officer"},
        {"name": "Mia Market", "title": "Chief Marketing Officer"},
        {"name": "Cole Customer", "title": "SVP, Customer Marketing"},
        {"name": "Ada Vert", "title": "SVP, Advertising and Promotion"},
        {"name": "Eli Commerce", "title": "SVP, E-commerce"},
        {"name": "Uma Unit", "title": "President, Creativity & Productivity Business"},
        # Directors the research also returned as executives:
        {"name": "Indra Elsewhere", "title": "Former Chair and CEO of PepsiCo, Inc."},
        {"name": "Brad Dean", "title": "President of Marshall University"},
    ],
    "senior_leadership": [],
}

_orig = F.llm_fetch_leadership
F.llm_fetch_leadership = lambda *a, **k: {k2: ([dict(p) for p in v] if isinstance(v, list) else v)
                                         for k2, v in LEADERS.items()}
job = client.post("/company-chart", json={"company_name": "Acme Retail",
                                          "domain": "acmeretail.example",
                                          "hq_location": "Austin, Texas"}).json()["job_id"]
F.llm_fetch_leadership = _orig
dag = A._JOBS[job].dag


def tree():
    return client.get("/tree", params={"job_id": job, "root": "root_global",
                                       "max_depth": 6, "dept_only": "true"}).json()


def find(node, nid):
    if node.get("node_id") == nid:
        return node
    for c in node.get("children") or []:
        hit = find(c, nid)
        if hit:
            return hit
    return None


EM = S._dept_node_id(S.EXEC_DEPT)
MKT = S._dept_node_id("Marketing")
t = tree()
em_kids = [c["label"] for c in (find(t, EM) or {}).get("children") or []]

# ── Executive Management: main departments only ─────────────────────────────
ok("Marketing" in em_kids and "Customer Marketing" not in em_kids
   and "Advertising & Promotion" not in em_kids and "Advertising and Promotion" not in em_kids,
   f"em: sub-functions are not departments of their own ({sorted(em_kids)})")
mkt_kids = [c["label"] for c in (find(t, MKT) or {}).get("children") or []]
ok({"Customer Marketing", "Advertising & Promotion"} <= set(mkt_kids),
   f"em: they are sub-departments inside Marketing ({mkt_kids})")
ok("Creativity & Productivity Business" in em_kids,
   "em: a business unit that belongs to no function stays its own department")
ok(not any(w in " ".join(em_kids) for w in ("Marshall", "PepsiCo", "Former")),
   f"em: another organisation is never a department ({sorted(em_kids)})")

em_people = client.get("/executives", params={"job_id": job, "dept_id": EM, "limit": 200}).json()
em_names = {p["label"] for p in em_people["executives"]}
ok(not {"Indra Elsewhere", "Brad Dean"} & em_names,
   f"em: directors listed by their job elsewhere are not executives ({sorted(em_names)})")
board = client.get("/executives", params={"job_id": job, "dept_id": S._dept_node_id(S.BOARD_DEPT)}).json()
ok({"Indra Elsewhere", "Brad Dean"} <= {p["label"] for p in board["executives"]},
   "em: they stay on the Board")

leads = {p["label"]: p["metadata"].get("leads_department") for p in em_people["executives"]}
ok(leads.get("Cole Customer") == "Marketing › Customer Marketing"
   and leads.get("Mia Market") == "Marketing",
   f"em: the SVP runs the sub-department, the CMO the department ({leads})")

mkt = client.get("/executives", params={"job_id": job, "dept_id": MKT, "limit": 200}).json()
heads = [p["label"] for p in mkt["executives"] if p["metadata"].get("is_department_head")]
subs = {p["label"] for p in mkt["executives"] if p["metadata"].get("leads_sub_department")}
ok(heads == ["Mia Market"] and {"Cole Customer", "Ada Vert"} <= subs,
   f"drawer: Marketing shows its CMO as head and its sub-department heads in its team ({heads}, {subs})")

# ── Adding people to Marketing ──────────────────────────────────────────────
roster = [
    {"fullName": "Sam Seo", "jobTitle": "SEO Manager"},                                   # Marketing › Performance › SEO
    {"fullName": "Ann Count", "jobTitle": "Senior Accountant"},                           # Finance — elsewhere
    {"fullName": "Pat Profile", "jobTitle": "Manager",                                    # placed by "about"
     "about": "Leads paid social and SEO programs for our digital marketing team"},
    {"fullName": "Rory Role", "role": "Head of Brand"},                                    # role used as title
    {"fullName": "Una Sure", "jobTitle": "Associate"},                                     # uncertain
]
r = client.post("/ingest-json", params={"job_id": job, "scope": MKT}, json=roster).json()
job = r["job_id"]                     # each expansion is a new version of the chart
dag = A._JOBS[job].dag
c = r.get("ingested", {})
ok(c.get("added") == 5 and c.get("in_scope") == 3 and c.get("elsewhere") == 1 and c.get("uncertain") == 1,
   f"ingest: 3 placed in Marketing, 1 elsewhere, 1 for review ({c})")


def dept_of(name):
    nid = next(n for n, a in dag.G.nodes(data=True) if a.get("label") == name)
    return S._dept_path(dag, next(p for p in dag.G.predecessors(nid)
                                  if str(dag.G.nodes[p].get("node_type", "")).startswith("dept")
                                  or dag.G.nodes[p].get("node_type") == "ghost")), dag.G.nodes[nid]


def placed(name):
    """Department path of a person, looking through level placeholders."""
    nid = next(n for n, a in dag.G.nodes(data=True) if a.get("label") == name)
    cur = nid
    while True:
        parents = list(dag.G.predecessors(cur))
        if not parents:
            return [], dag.G.nodes[nid]
        p = parents[0]
        if str(dag.G.nodes[p].get("node_type", "")).startswith("dept"):
            return S._dept_path(dag, p), dag.G.nodes[nid]
        cur = p


path, _ = placed("Sam Seo")
ok(path == ["Marketing", "Performance & Digital Marketing", "SEO"],
   f"ingest: a confident match lands in its sub-department and sub-team ({path})")
path, _ = placed("Ann Count")
ok(path[:1] == ["Finance & Accounting"], f"ingest: another department's person goes there only ({path})")
path, _ = placed("Pat Profile")
ok(path[:2] == ["Marketing", "Performance & Digital Marketing"],
   f"ingest: 'about' places a vague title ({path})")
path, _ = placed("Rory Role")
ok(path[:2] == ["Marketing", "Brand & Creative"], f"ingest: 'role' is read when there is no title ({path})")
path, node = placed("Una Sure")
meta = node.get("metadata", {})
ok(path == ["Marketing"] and meta.get("placement_uncertain") is True and "best guess" in meta.get("placement_note", ""),
   f"ingest: an uncertain person stays in Marketing, flagged ({path}, {meta.get('placement_note')})")

mkt = client.get("/executives", params={"job_id": job, "dept_id": MKT, "limit": 200}).json()
flagged = [p["label"] for p in mkt["executives"] if p["metadata"].get("placement_uncertain")]
ok(flagged == ["Una Sure"], f"drawer: the flag reaches the department's list ({flagged})")

# ── Adding people to a sub-department ───────────────────────────────────────
CM = next(n for n, a in dag.G.nodes(data=True)
          if a.get("label") == "Customer Marketing" and str(a.get("node_type", "")).startswith("dept"))
r = client.post("/ingest-json", params={"job_id": job, "scope": CM}, json=[
    {"fullName": "Lou Loyal", "jobTitle": "Loyalty Program Manager"},
    {"fullName": "Mel Market", "jobTitle": "Marketing Manager"},
    {"fullName": "Bo Brand", "jobTitle": "Brand Manager"},
]).json()
job = r["job_id"]
dag = A._JOBS[job].dag
c = r.get("ingested", {})
path_lou, _ = placed("Lou Loyal")
path_mel, _ = placed("Mel Market")
path_bo, _ = placed("Bo Brand")
ok(path_lou == ["Marketing", "Customer Marketing", "Loyalty"],
   f"sub-dept: its own specialism becomes a sub-team ({path_lou})")
ok(path_mel[:2] == ["Marketing", "Customer Marketing"],
   f"sub-dept: a general marketer added here stays here ({path_mel})")
ok(path_bo[:2] == ["Marketing", "Brand & Creative"],
   f"sub-dept: another sub-department's person goes there ({path_bo}, {c})")

# ── Business unit: keeps its roster, by function ────────────────────────────
BU = S._dept_node_id("Creativity & Productivity Business")
job = client.post("/ingest-json", params={"job_id": job, "scope": BU}, json=[
    {"fullName": "Rae Search", "jobTitle": "Research Scientist"}]).json()["job_id"]
dag = A._JOBS[job].dag
path, _ = placed("Rae Search")
ok(path[:2] == ["Creativity & Productivity Business", "Research & Development"],
   f"unit: a business unit keeps its people, organised by function ({path})")

# ── Departments added by hand ───────────────────────────────────────────────
r = client.post("/departments", params={"job_id": job}, json={"name": "Data Science Office"})
d1 = r.json()
ok(r.status_code == 200 and d1.get("level") == "department" and d1.get("parent_id") == EM,
   f"add: a department goes under Executive Management ({d1})")
r = client.post("/departments", params={"job_id": job},
                json={"name": "Influencer Marketing", "parent_id": MKT})
d2 = r.json()
ok(r.status_code == 200 and d2.get("level") == "sub-department"
   and d2.get("path") == ["Marketing", "Influencer Marketing"], f"add: a sub-department inside Marketing ({d2})")
r = client.post("/departments", params={"job_id": job},
                json={"name": "TikTok", "parent_id": d2["node_id"]})
d3 = r.json()
ok(r.status_code == 200 and d3.get("level") == "sub-team"
   and d3.get("path") == ["Marketing", "Influencer Marketing", "TikTok"], f"add: a sub-team inside it ({d3})")
r = client.post("/departments", params={"job_id": job}, json={"name": "Deeper", "parent_id": d3["node_id"]})
ok(r.status_code == 422, "add: nothing below a sub-team")
r = client.post("/departments", params={"job_id": job}, json={"name": "influencer marketing", "parent_id": MKT})
ok(r.status_code == 409, "add: no duplicate name in the same place")
r = client.post("/departments", params={"job_id": job},
                json={"name": "X Team", "parent_id": S._dept_node_id(S.BOARD_DEPT)})
ok(r.status_code == 422, "add: the Board takes no departments")
t = tree()
ok(find(t, d3["node_id"]) is not None and (find(t, d1["node_id"]) or {}).get("metadata", {}).get("custom"),
   "add: they appear in the chart, marked as added by hand")
job = client.post("/ingest-json", params={"job_id": job, "scope": d2["node_id"]},
                  json=[{"fullName": "Ivy Influence", "jobTitle": "Influencer Marketing Manager"}]).json()["job_id"]
dag = A._JOBS[job].dag
path, _ = placed("Ivy Influence")
ok(path[:2] == ["Marketing", "Influencer Marketing"],
   f"add: people added to a hand-made sub-department land in it ({path})")

# ── A company's industry alone is not a confident placement ─────────────────
# On production, "Associate" at an e-commerce company was placed in Sales as if
# certain (the industry boost was its only signal).
from classifier import classify, CONFIDENT
ok(classify(job_title="Associate", industry="Ecommerce").confidence < CONFIDENT
   and classify(job_title="Sales Associate", industry="Ecommerce").confidence >= CONFIDENT,
   "industry: a title with no function is a guess; one that names it is not")

# ── The board is shown as "Board of Directors" ──────────────────────────────
ok((find(t, S._dept_node_id(S.BOARD_DEPT)) or {}).get("label") == "Board of Directors",
   "label: the board shows as Board of Directors")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
