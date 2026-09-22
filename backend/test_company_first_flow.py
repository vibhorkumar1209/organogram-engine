"""
Company-first flow: identity in, leadership chart out, roster ingested against it.

The chart now starts from company name + domain + HQ location rather than a
file. Once the Board of Directors and Executive Management are found, the user
selects departments/executives and ingests a roster — by file, by JSON POST, or
from a URL this server pulls. People already in the chart are merged, never
duplicated.

Run: python3 backend/test_company_first_flow.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient

import api_server as A
import structural_engine as S

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


client = TestClient(A.app)


def _people(job_id):
    """{name: department} for every person in the chart."""
    dag = A._JOBS[job_id].dag
    out = {}
    for nid in dag.G.nodes:
        if dag.G.nodes[nid].get("node_type") != "person":
            continue
        dept = ""
        for parent in dag.G.predecessors(nid):
            if str(dag.G.nodes[parent].get("node_type", "")).startswith("dept"):
                dept = dag.G.nodes[parent].get("label", "")
                break
        out[dag.G.nodes[nid].get("label", "")] = dept
    return out


def _new_chart(name="Acme Corp", domain="https://www.acme.com/about", hq="London, UK"):
    r = client.post("/company-chart",
                    json={"company_name": name, "domain": domain, "hq_location": hq})
    return r


# ── Stage 1: company identity creates a chart ───────────────────────────────
r = _new_chart()
ok(r.status_code == 200, f"company-chart: created (got {r.status_code})")
body = r.json()
job = body.get("job_id")
ok(bool(job), "company-chart: returns a server-minted job_id")
ok(body.get("domain") == "acme.com", "company-chart: domain normalised from a full URL")
ok(body.get("hq_location") == "London, UK", "company-chart: HQ echoed back")

meta = A._JOBS[job].dag.G.nodes["root_global"].get("metadata", {})
ok(meta.get("hq_location") == "London, UK", "company-chart: HQ stored on the root node")
ok(meta.get("domain") == "acme.com", "company-chart: domain stored on the root node")

r = client.post("/company-chart", json={"company_name": "A", "domain": "", "hq_location": ""})
ok(r.status_code == 422, "company-chart: rejects a company name that is too short")

# No file was uploaded, so there are no columns to complain about. This warning
# appearing on a brand-new company chart is what the live UI showed first.
ok(body.get("canonical_missing") == [],
   "company-chart: no missing-column warning when there is no file")

# ── Stage 2: selectable targets — Executive Management only ─────────────────
sel = client.get("/selectable", params={"job_id": job})
ok(sel.status_code == 200, "selectable: responds for a live job")
ok("departments" in sel.json() and "executives" in sel.json(),
   "selectable: returns departments and executives")


def _seed_leadership(job_id, people):
    """Inject people the way the leadership search does, so /selectable sees
    what production would: the ingest path buckets EVP/SVP titles into
    functional departments instead, which is not what this stage reads."""
    import uuid as _uuid
    from inference_logic import ClassifiedRecord
    dag = A._JOBS[job_id].dag
    for name, title, dept, layer in people:
        dag.insert_person(ClassifiedRecord(
            id="llm_" + _uuid.uuid4().hex[:8], full_name=name, designation=title,
            company="Acme Corp", linkedin_url="", location="", country="",
            sector="Private", region="Global HQ", layer=layer, dept_primary=dept,
            dept_secondary="", dept_tertiary="", nlp_confidence=0.9,
            nlp_industry="llm", nlp_method="llm_leadership_web"))
    dag.repair_governance_edges()


em_job = _new_chart("Split Co", "split.example", "Berlin").json()["job_id"]
_seed_leadership(em_job, [
    ("Dana Boardman", "Independent Director", S.BOARD_DEPT, 2),
    ("Ravi Chair", "Chair of the Board", S.BOARD_DEPT, 0),
    ("Robin Apex", "Chairman and Chief Executive Officer", S.EXEC_DEPT, 1),
    ("Fin Tanaka", "Executive Vice President, Finance and Chief Financial Officer",
     S.EXEC_DEPT, 1),
    ("Hana Ruiz", "Executive Vice President, Human Resources", S.EXEC_DEPT, 1),
    ("Omar Diaz", "Executive Vice President, Core Diagnostics", S.EXEC_DEPT, 1),
    ("Lena Vogt", "Senior Vice President, Structural Heart", S.EXEC_DEPT, 1),
])
sel = client.get("/selectable", params={"job_id": em_job}).json()
exec_names = [e["label"] for e in sel["executives"]]
dept_by_exec = {e["label"]: e["department"] for e in sel["executives"]}

ok("Dana Boardman" not in exec_names and "Ravi Chair" not in exec_names,
   "selectable: board members are NOT offered for ingestion")
ok(len(exec_names) == 5, f"selectable: all 5 Executive Management members listed (got {len(exec_names)})")
ok(dept_by_exec.get("Fin Tanaka") == "Finance & Accounting",
   "split: CFO runs Finance & Accounting")
ok(dept_by_exec.get("Hana Ruiz") == "Human Resources",
   "split: HR executive runs Human Resources")
ok(dept_by_exec.get("Omar Diaz") == "Core Diagnostics",
   "split: business-unit executive runs that unit, not a generic bucket")

# A job title is not a department. These previously became departments called
# "Commercial Officer", "Operations Officer" and "People Officer"; they must
# map through the canonical Department/Designation taxonomy instead.
for title, expect in [
    ("Chief Commercial Officer", "Sales & Business Development"),
    ("Chief Operations Officer", "Operations"),
    ("Chief People Officer", "Human Resources"),
    ("Chief Operating Officer", "Operations"),
    ("Chief Revenue Officer", "Sales & Business Development"),
    ("Chief Product Officer", "Product Management"),
    ("Chief Data Officer", "Information Technology"),
    ("Chief Risk Officer", "Legal, Risk & Compliance"),
    ("Chief Customer Officer", "Customer Success & Service"),
    ("Chief Sustainability Officer", "Sustainability"),
]:
    got = S._exec_department(title)
    ok(got == expect, f"taxonomy: {title} -> {got}")
ok(not any(S._exec_department(t).lower().endswith("officer")
           for t in ["Chief Commercial Officer", "Chief Operations Officer",
                     "Chief People Officer", "Chief Security Officer"]),
   "taxonomy: no department is named after a job title")

# Executive Management branches into those departments in the chart itself.
em_tree = client.get("/tree", params={"job_id": em_job, "dept_only": "true"}).json()


def _find(node, label):
    if node.get("label") == label:
        return node
    for kid in node.get("children") or []:
        hit = _find(kid, label)
        if hit:
            return hit
    return None


em_node = _find(em_tree if "label" in em_tree else em_tree.get("tree", em_tree), S.EXEC_DEPT)
ok(em_node is not None, "branch: Executive Management is in the tree")
branch_labels = [c.get("label") for c in (em_node.get("children") or [])] if em_node else []
ok("Finance & Accounting" in branch_labels,
   f"branch: departments hang off Executive Management (got {branch_labels})")

# Executive Management must appear ONCE. Giving it a second parent edge made
# the chart draw it twice — a tree renders a node once per parent.
_dag = A._JOBS[em_job].dag
_em_nodes = [n for n in _dag.G.nodes if _dag.G.nodes[n].get("label") == S.EXEC_DEPT]
ok(len(_em_nodes) == 1, f"branch: one Executive Management node (got {len(_em_nodes)})")
ok(len(list(_dag.G.predecessors(_em_nodes[0]))) == 1,
   "branch: Executive Management has exactly one parent")

# The core departments appear whether or not an executive was found to run
# them — a company has an IT function even if its leadership page named no CTO.
_core_labels = [d["label"] for d in
                client.get("/selectable", params={"job_id": em_job}).json()["departments"]]
for _expected in ["Information Technology", "Human Resources", "Marketing",
                  "Sales & Business Development", "Operations", "Engineering",
                  "Finance & Accounting", "Product Management"]:
    ok(_expected in _core_labels, f"core: {_expected} offered as a department")
ok("Core Diagnostics" in _core_labels,
   "core: a business unit an executive runs is offered alongside the core set")
ok(not any(l in (S.BOARD_DEPT, S.EXEC_DEPT) for l in _core_labels),
   "core: neither panel is offered as a department")
_headed = {d["label"]: d.get("head_name") for d in
           client.get("/selectable", params={"job_id": em_job}).json()["departments"]}
ok(_headed.get("Finance & Accounting") == "Fin Tanaka",
   "core: a department an executive runs still records its head")
ok(_headed.get("Sustainability") in ("", None),
   "core: a department with no executive has no head")
ok(dept_by_exec.get("Lena Vogt") == "Structural Heart",
   "split: second business unit kept distinct")
ok(dept_by_exec.get("Robin Apex") == "",
   "split: the CEO runs the company, so has no single department")

dept_labels = [d["label"] for d in sel["departments"]]
# The core set is always offered, plus any business unit an executive runs,
# so the count is the core list plus those units — not one per executive.
ok(len(dept_labels) >= 15,
   f"split: the core department set is offered (got {len(dept_labels)})")
ok("Core Diagnostics" in dept_labels and "Structural Heart" in dept_labels,
   "split: both business units kept distinct alongside the core set")
ok(S.BOARD_DEPT not in dept_labels and S.EXEC_DEPT not in dept_labels,
   "split: the BOD/EM panels are not themselves ingestion targets")
heads = {d["label"]: d.get("head_name") for d in sel["departments"]}
ok(heads.get("Finance & Accounting") == "Fin Tanaka",
   "split: each department records the executive who runs it")

# The derived departments are real nodes, so a roster classified into one
# lands under it rather than creating a parallel department.
fin_id = next(d["id"] for d in sel["departments"] if d["label"] == "Finance & Accounting")
r = client.post("/ingest-json", params={"job_id": em_job, "scope": fin_id}, json=[
    {"full_name": "Pia Lindqvist", "job_title": "Director of Financial Planning",
     "company": "Acme Corp"},
    {"full_name": "Sam Oduya", "job_title": "Senior Accountant", "company": "Acme Corp"},
])
ok(r.status_code == 200, "split: roster ingested against a derived department")
after_sel = client.get("/selectable", params={"job_id": em_job}).json()
fin = next(d for d in after_sel["departments"] if d["label"] == "Finance & Accounting")
ok(fin["people"] >= 1,
   f"split: ingested people counted under the department (got {fin['people']})")

# ── Departments branch from EM even when the search finds almost nobody ─────
# Executive Management is only created when someone is placed in it. A live
# NVIDIA run returned 0 directors and 1 executive who landed in a functional
# department, so no EM node existed when the split ran and all 15 departments
# were attached to the ROOT. EM was then created moments later by the
# uploaded-data fallback, leaving it in the chart with no branches — the exact
# symptom reported, and invisible to a test that seeded EM members first.
def _branches_for(roster):
    import uuid as _u
    from structural_engine import OrganogramDAG as _D, split_executive_departments as _sp
    from inference_logic import ClassifiedRecord as _R
    dag = _D(company_name="Thin Co")
    for _n, _t, _d, _l in roster:
        dag.insert_person(_R(
            id="llm_" + _u.uuid4().hex[:8], full_name=_n, designation=_t,
            company="Thin Co", linkedin_url="", location="", country="",
            sector="Private", region="Global HQ", layer=_l, dept_primary=_d,
            dept_secondary="", dept_tertiary="", nlp_confidence=0.9,
            nlp_industry="llm", nlp_method="llm_leadership_web"))
    dag.repair_governance_edges()
    _sp(dag)
    _em = dag._node_id("dept", S.EXEC_DEPT)
    if _em not in dag.G:
        return -1, -1
    kids = sum(1 for k in dag.G.successors(_em)
               if str(dag.G.nodes[k].get("node_type", "")).startswith("dept"))
    root_depts = sum(1 for k in dag.G.successors("root_global")
                     if str(dag.G.nodes[k].get("node_type", "")).startswith("dept"))
    return kids, root_depts


# NB: not named _people — that is the module-level helper, and shadowing it
# here broke every later call to it with "'list' object is not callable".
for _label, _roster in [
    ("nobody found at all", []),
    ("one person in a functional department", [("A", "Software Engineer", "Engineering", 5)]),
    ("one director, no executives", [("B", "Independent Director", S.BOARD_DEPT, 2)]),
    ("one executive, no directors", [("A", "Chief Technology Officer", S.EXEC_DEPT, 1)]),
]:
    _kids, _root = _branches_for(_roster)
    ok(_kids >= 15,
       f"thin: departments branch from Executive Management — {_label} (got {_kids})")
    ok(_root <= 2,
       f"thin: departments are not dumped on the root — {_label} (root has {_root})")

# ── A failing leadership search must not skip everything after it ───────────
# _enrich_with_llm_leadership injects people and THEN does more work of its
# own. When that tail raised, one surrounding try meant the department split
# never ran and the chart was never persisted — while the people already
# injected made the job look enriched. The live NVIDIA chart showed exactly
# that: 9 directors, 10 executives, and zero departments under EM.
import structural_engine as _se_mod

_orig_enrich = A._enrich_with_llm_leadership
_orig_split = A.split_executive_departments
_split_calls = {"n": 0}


def _boom(dag, classified, company_name, domain=""):
    """Inject leadership, then fail — exactly how the real tail broke."""
    import uuid as _u
    from inference_logic import ClassifiedRecord as _R
    for _n, _t in [("Kagan Test", "Chief Technology Officer"),
                   ("Kress Test", "Chief Financial Officer")]:
        dag.insert_person(_R(
            id="llm_" + _u.uuid4().hex[:8], full_name=_n, designation=_t,
            company=company_name, linkedin_url="", location="", country="",
            sector="Private", region="Global HQ", layer=1,
            dept_primary=_se_mod.EXEC_DEPT, dept_secondary="", dept_tertiary="",
            nlp_confidence=0.9, nlp_industry="llm", nlp_method="llm_leadership_web"))
    dag.repair_governance_edges()
    raise RuntimeError("LinkedIn backfill blew up")


def _counting_split(dag):
    _split_calls["n"] += 1
    return _orig_split(dag)


A._enrich_with_llm_leadership = _boom
A.split_executive_departments = _counting_split
try:
    boom_job = _new_chart("Boom Co", "boom.example", "X").json()["job_id"]
finally:
    A._enrich_with_llm_leadership = _orig_enrich
    A.split_executive_departments = _orig_split

ok(_split_calls["n"] >= 1,
   "resilience: the department split still ran after the search raised")
_boom_dag = A._JOBS[boom_job].dag
_boom_em = _boom_dag._node_id("dept", "Executive Management")
_boom_depts = [k for k in _boom_dag.G.successors(_boom_em)
               if str(_boom_dag.G.nodes[k].get("node_type", "")).startswith("dept")] \
    if _boom_em in _boom_dag.G else []
ok(len(_boom_depts) >= 15,
   f"resilience: departments exist despite the failure (got {len(_boom_depts)})")
_boom_people = [n for n in _boom_dag.G.nodes
                if _boom_dag.G.nodes[n].get("node_type") == "person"]
ok(len(_boom_people) >= 2,
   "resilience: people the search injected before failing are kept")
_boom_sel = client.get("/selectable", params={"job_id": boom_job}).json()
ok(len(_boom_sel["departments"]) >= 15,
   "resilience: those departments are selectable without a second split call")

# ── Executive Management renders once, whatever the insertion order ─────────
# It is parented to root when created before Board of Management exists; once
# BOD appeared, the next executive inserted attached it under BOD as well,
# leaving two parent edges. A tree draws a node once per parent, so the chart
# showed two Executive Management cards. Order EXEC, BOARD, EXEC triggers it.
import itertools as _it
from structural_engine import OrganogramDAG as _DAG
from inference_logic import ClassifiedRecord as _CR
import uuid as _uu


def _spine_parents(order):
    dag = _DAG(company_name="Spine Co")
    for name, title, dept, layer in order:
        dag.insert_person(_CR(
            id="llm_" + _uu.uuid4().hex[:8], full_name=name, designation=title,
            company="Spine Co", linkedin_url="", location="", country="",
            sector="Private", region="Global HQ", layer=layer, dept_primary=dept,
            dept_secondary="", dept_tertiary="", nlp_confidence=0.9,
            nlp_industry="llm", nlp_method="llm_leadership_web"))
    em = dag._node_id("dept", "Executive Management")
    bod = dag._node_id("dept", "Board of Management")
    return (len(list(dag.G.predecessors(em))) if em in dag.G else 0,
            len(list(dag.G.predecessors(bod))) if bod in dag.G else 0)


_specs = [("E1", "Chief Technology Officer", S.EXEC_DEPT, 1),
          ("E2", "Chief Financial Officer", S.EXEC_DEPT, 1),
          ("B1", "Independent Director", S.BOARD_DEPT, 2),
          ("B2", "Chair of the Board", S.BOARD_DEPT, 0)]
_violations = [p for p in _it.permutations(_specs)
               if _spine_parents(p) != (1, 1)]
ok(not _violations,
   f"spine: one parent edge for both panels across all 24 insertion orders "
   f"({len(_violations)} bad)")
ok(_spine_parents((_specs[0], _specs[2], _specs[1])) == (1, 1),
   "spine: the EXEC, BOARD, EXEC order that caused the duplicate is clean")

# ── A dual-role CEO and a director with an outside C-suite past ─────────────
# Both were reported from a live NVIDIA chart: Jensen Huang listed twice under
# Executive Management, and Dawn Hudson — an NVIDIA director whose title
# describes her career at the NFL and PepsiCo — listed as an NVIDIA executive.
# One cause: insert_person moved a board member into Executive Management
# whenever their title mentioned a C-suite role, overruling the leadership
# search that had just placed them.
dual_job = _new_chart("Dual Role Co", "dual.example", "Santa Clara").json()["job_id"]
_seed_leadership(dual_job, [
    ("Jensen Huang", "Co-founder, President and Chief Executive Officer", S.EXEC_DEPT, 1),
    ("Jensen Huang", "Founder, President, and Chief Executive Officer", S.BOARD_DEPT, 0),
    ("Dawn Hudson",
     "Former Chief Marketing Officer, National Football League & Former CEO "
     "Pepsi-Cola North America", S.BOARD_DEPT, 2),
    ("Michael Kagan", "Chief Technology Officer", S.EXEC_DEPT, 1),
])
dual_sel = client.get("/selectable", params={"job_id": dual_job}).json()
dual_names = [e["label"] for e in dual_sel["executives"]]
ok(dual_names.count("Jensen Huang") == 1,
   f"dual: the CEO appears once in Executive Management (got {dual_names.count('Jensen Huang')})")
ok("Dawn Hudson" not in dual_names,
   "dual: a director whose title names an outside C-suite role stays off the exec panel")
ok("Michael Kagan" in dual_names, "dual: real executives still listed")

_dual_dag = A._JOBS[dual_job].dag
_where = {}
for _nid in _dual_dag.G.nodes:
    if _dual_dag.G.nodes[_nid].get("node_type") != "person":
        continue
    _lbl = _dual_dag.G.nodes[_nid].get("label", "")
    for _p in _dual_dag.G.predecessors(_nid):
        if str(_dual_dag.G.nodes[_p].get("node_type", "")).startswith("dept"):
            _where.setdefault(_lbl, []).append(_dual_dag.G.nodes[_p].get("label", ""))
ok(sorted(_where.get("Jensen Huang", [])) == [S.BOARD_DEPT, S.EXEC_DEPT],
   f"dual: the CEO sits in both panels, once each (got {_where.get('Jensen Huang')})")
ok(_where.get("Dawn Hudson") == [S.BOARD_DEPT],
   f"dual: the director sits only on the board (got {_where.get('Dawn Hudson')})")

# An ordinary uploaded row with a board-sounding title must still be kept out
# of the panels — the guard that protects them is unchanged.
_csv_job = _new_chart("CSV Guard Co", "csv.example", "X").json()["job_id"]
client.post("/ingest-json", params={"job_id": _csv_job}, json=[
    {"full_name": "Pat Ordinary", "job_title": "Chief of Staff", "company": "CSV Guard Co"}])
_csv_sel = client.get("/selectable", params={"job_id": _csv_job}).json()
ok("Pat Ordinary" not in [e["label"] for e in _csv_sel["executives"]],
   "dual: an ordinary roster row does not reach Executive Management")

# ── Stage 3: ingest a roster, scoped to a selection ─────────────────────────
roster = [
    {"full_name": "Ana Silva", "job_title": "Chair of the Board", "company": "Acme Corp"},
    {"full_name": "Kenji Sato", "job_title": "Independent Non-Executive Director",
     "company": "Acme Corp"},
    {"full_name": "Jane Okonjo", "job_title": "Chief Executive Officer", "company": "Acme Corp"},
    {"full_name": "Mia Fox", "job_title": "Head of Marketing", "company": "Acme Corp"},
    {"full_name": "Sam Ray", "job_title": "Chief of Staff", "company": "Acme Corp"},
    {"full_name": "Tom Reed", "job_title": "VP Engineering", "company": "Acme Corp"},
]
r = client.post("/ingest-json", params={"job_id": job}, json=roster)
ok(r.status_code == 200, f"ingest-json: accepted (got {r.status_code})")
ok(r.json()["ingested"]["added"] == 6, "ingest-json: all six people added")

people = _people(job)
ok(people.get("Ana Silva") == S.BOARD_DEPT,
   "routing: board chair filed under Board of Management")
ok(people.get("Kenji Sato") == S.BOARD_DEPT,
   "routing: non-executive director filed under Board of Management")
ok(people.get("Jane Okonjo") == S.EXEC_DEPT,
   "routing: CEO filed under Executive Management")
ok(people.get("Mia Fox") not in (S.BOARD_DEPT, S.EXEC_DEPT),
   "routing: ordinary title goes to a functional department")
ok(people.get("Sam Ray") not in (S.BOARD_DEPT, S.EXEC_DEPT),
   "routing: 'Chief of Staff' does not sneak into Executive Management")

# ── Deduplication: the same people arriving again ───────────────────────────
before = len(_people(job))
again = [
    {"full_name": "Dr. Jane Okonjo", "job_title": "Chief Executive Officer",
     "company": "Acme Corp", "linkedin_url": "https://linkedin.com/in/jane"},
    {"full_name": "Ana Silva", "job_title": "Chair of the Board", "company": "Acme Corp"},
    {"full_name": "Priya Nair", "job_title": "Chief Financial Officer", "company": "Acme Corp"},
]
r = client.post("/ingest-json", params={"job_id": job}, json=again)
counts = r.json()["ingested"]
ok(counts["merged"] == 2, f"dedup: two known people merged, not re-added (got {counts['merged']})")
ok(counts["added"] == 1, "dedup: only the genuinely new person is added")
after = _people(job)
ok(len(after) == before + 1,
   f"dedup: chart grew by exactly one ({before} -> {len(after)})")
ok(sum(1 for n in after if "Jane" in n) == 1, "dedup: honorific spelling did not create a second Jane")
ok(after.get("Priya Nair") == S.EXEC_DEPT, "dedup: the new CFO still routes to Executive Management")

# the merged record's detail is carried onto the existing node
dag = A._JOBS[job].dag
jane = next(n for n in dag.G.nodes if dag.G.nodes[n].get("label", "").endswith("Jane Okonjo")
            or dag.G.nodes[n].get("label") == "Jane Okonjo")
ok("linkedin.com/in/jane" in str((dag.G.nodes[jane].get("metadata") or {}).get("linkedin_url", "")),
   "dedup: LinkedIn URL from the upload enriched the existing node")

# duplicates inside one uploaded file collapse too
job2 = _new_chart("Beta Ltd", "beta.com", "Berlin").json()["job_id"]
r = client.post("/ingest-json", params={"job_id": job2}, json=[
    {"full_name": "Tom Reed", "job_title": "VP Engineering", "company": "Beta Ltd"},
    {"full_name": "Tom Reed", "job_title": "VP Engineering", "company": "Beta Ltd"},
])
ok(r.json()["ingested"]["added"] == 1 and r.json()["ingested"]["merged"] == 1,
   "dedup: the same person twice in one file yields one node")

# ── File ingest ─────────────────────────────────────────────────────────────
csv = (b"Full Name,Designation,Company\n"
       b"Li Wei,Chief Technology Officer,Acme Corp\n"
       b"Ana Silva,Chair of the Board,Acme Corp\n")
r = client.post("/ingest", params={"job_id": job},
                files={"file": ("team.csv", csv, "text/csv")})
ok(r.status_code == 200, "ingest: CSV file accepted")
ok(r.json()["ingested"]["merged"] == 1, "ingest: the already-known chair merged from CSV")
ok(_people(job).get("Li Wei") == S.EXEC_DEPT, "ingest: new CTO routed to Executive Management")

r = client.post("/ingest", params={"job_id": job},
                files={"file": ("x.txt", b"nope", "text/plain")})
ok(r.status_code == 400, "ingest: unsupported file type rejected")

# ── URL ingest is guarded against SSRF ──────────────────────────────────────
for bad, why in [
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata address"),
    ("http://127.0.0.1:8000/admin", "loopback"),
    ("file:///etc/passwd", "non-HTTP scheme"),
    ("http://localhost/x", "localhost by name"),
]:
    r = client.post("/ingest", params={"job_id": job, "source_url": bad})
    ok(r.status_code == 422, f"ssrf: refused {why}")

r = client.post("/ingest", params={"job_id": job})
ok(r.status_code == 422, "ingest: no file and no URL is an error")

# ── Scope is accepted and echoed (a hint, not a hard parent) ────────────────
sel = client.get("/selectable", params={"job_id": job}).json()
ok(len(sel["departments"]) > 0, "selectable: departments appear once people exist")
ok(all(e["department"] != S.EXEC_DEPT for e in sel["executives"]),
   "selectable: an executive's department is the function they run, not the panel")
scope_ids = ",".join(d["id"] for d in sel["departments"][:2])
r = client.post("/ingest-json", params={"job_id": job, "scope": scope_ids},
                json=[{"full_name": "Noor Haddad", "job_title": "Head of Legal",
                       "company": "Acme Corp"}])
ok(r.status_code == 200, "scope: ingest accepts selected node ids")
ok(r.json()["scope"] == scope_ids.split(","), "scope: selection echoed back")
ok(_people(job).get("Noor Haddad") not in ("", None),
   "scope: the classifier still placed the person (selection is a hint)")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
