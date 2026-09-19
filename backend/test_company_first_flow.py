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
ok(dept_by_exec.get("Lena Vogt") == "Structural Heart",
   "split: second business unit kept distinct")
ok(dept_by_exec.get("Robin Apex") == "",
   "split: the CEO runs the company, so has no single department")

dept_labels = [d["label"] for d in sel["departments"]]
ok(len(dept_labels) == 4, f"split: four departments offered (got {len(dept_labels)})")
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
