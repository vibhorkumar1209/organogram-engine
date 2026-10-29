"""
LinkedIn profiles for Board and Executive Management members.

Every case reproduces something real search results did (Parallel Search,
Abbott and NVIDIA leadership, October 2026), with invented namesakes:

  - "Robert Ford Abbott LinkedIn" returned eight Robert Fords and a Robert
    FORDE who works at Abbott — none of them Abbott's CEO. The old resolver
    accepted any profile whose slug contained the surname.
  - An impostor "Jensen Huang - Founder and CEO, NVIDIA" (53 connections,
    employer "Active Recovery, LLC") sat next to the real profile; the
    damning detail was in only one of the two results that listed it.
  - Excerpts carry LinkedIn's "Other similar profiles" sidebar, naming other
    people and THEIR employers — not evidence about the profile itself, but
    a genuine corroboration for the person the sidebar names.
  - NVIDIA's "Debora Shoquist" is "Debbie Shoquist" on LinkedIn.

Run: python3 backend/test_linkedin_resolution.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import linkedin_resolver as L

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def res(url, title, excerpt=""):
    return {"url": url, "title": title, "excerpts": [excerpt] if excerpt else []}


def judge(name, title, kind, results, company="Abbott Laboratories", domain="abbott.com", hq=""):
    return L.judge({"name": name, "title": title, "kind": kind}, results, company, domain, hq)


# ── Names ────────────────────────────────────────────────────────────────────
ok(L.name_match("Robert B. Ford", "Robert Ford") == "exact", "name: middle initial may be missing")
ok(L.name_match("Robert Ford", "Robert Forde") == "", "name: surname must match exactly (Forde is not Ford)")
ok(L.name_match("Debora Shoquist", "Debbie Shoquist") == "nick", "name: Debbie is Debora")
ok(L.name_match("William Brown", "Bill Brown") == "nick", "name: Bill is William")
ok(L.name_match("Christopher Lee", "Chris Lee") == "nick", "name: a 3+ letter prefix is a short form")
ok(L.name_match("Philip Boudreau", "Phillip Boudreaux") == "", "name: a different surname spelling is a different person")
ok(L.name_match("Claire Babineaux-Fontenot", "Claire Babineaux-Fontenot") == "exact", "name: hyphenated surname")
ok(L.name_match("Jane Doe", "Jane Doe, PhD, CPA") == "exact", "name: credentials after a comma ignored")
ok(L.name_match("Dr. Jane Doe", "Jane Doe") == "exact", "name: honorific ignored")

# ── URLs ─────────────────────────────────────────────────────────────────────
ok(L.normalise_profile_url("https://uk.linkedin.com/in/jane-doe-123/?trk=x")
   == "https://www.linkedin.com/in/jane-doe-123", "url: country subdomain, query and slash normalised")
ok(L.normalise_profile_url("https://www.linkedin.com/company/abbott") == "", "url: company pages are not profiles")
ok(L.parse_result_title("Jane Doe - CFO - Acme | LinkedIn") == ("Jane Doe", "CFO - Acme"),
   "title: name and headline split")

# ── A common name with no match must be Not Found, not a guess ──────────────
ford = [
    res("https://linkedin.com/in/robert-forde-1", "Robert Forde - Abbott | LinkedIn",
        "Experience: Abbott · Location: Los Altos · 196 connections on LinkedIn."),
    res("https://linkedin.com/in/robertford2012", "Robert Ford - Founder & CEO | LinkedIn",
        "My passion lies in helping clients stand out as leaders."),
    res("https://linkedin.com/in/robertdford", "Robert Ford - Denver, Colorado, United States", ""),
    res("https://linkedin.com/in/robert-ford-2", "Robert Ford - Houston, Texas, United States", ""),
    res("https://linkedin.com/in/rob-ford-3", "Robert Ford - Executive-Level Technology & ...", ""),
]
r = judge("Robert B. Ford", "Chairman and Chief Executive Officer", "executive", ford)
ok(r["confidence"] == L.NOT_FOUND and r["linkedin_url"] == "",
   f"common name: no Robert Ford fits Abbott's CEO -> Not Found (got {r['confidence']} {r['linkedin_url']})")

# ── Exact name + company + leadership title = High ───────────────────────────
r = judge("Philip P. Boudreau", "EVP, Finance and Chief Financial Officer", "executive", [
    res("https://linkedin.com/in/philip-boudreau-8367543",
        "Philip Boudreau - Executive Vice President Finance, Chief Financial ...",
        "Executive Vice President Finance, Chief Financial Officer at Abbott · Experience: Abbott"),
    res("https://linkedin.com/in/phil-boudreau-x", "Phil Boudreau",
        "Experience: Boudreau and Boudreau, LLP · 163 connections on LinkedIn."),
])
ok(r["confidence"] == "High" and r["linkedin_url"].endswith("philip-boudreau-8367543"),
   f"verified: CFO found with High confidence (got {r['confidence']})")
ok(r["matched_name"] == "Philip Boudreau" and "Chief Financial" in r["matched_title"],
   "verified: matched name and title come from the profile itself")

# ── The impostor: evidence pooled across results for one URL ────────────────
r = judge("Jensen Huang", "President and Chief Executive Officer", "executive", [
    res("https://www.linkedin.com/in/jenhsunhuang", "Jensen Huang - Founder and CEO, NVIDIA | LinkedIn",
        "Founder and CEO, NVIDIA · In 1993, I founded NVIDIA."),
    res("https://www.linkedin.com/in/jensen-huang-fake", "Jensen Huang - Founder and CEO, NVIDIA NVIDIA | LinkedIn",
        "Founder and CEO, NVIDIA NVIDIA · Experience: Active Recovery, LLC. · 53 connections on LinkedIn."),
    # The same impostor again, this time with nothing but the headline.
    res("https://www.linkedin.com/in/jensen-huang-fake", "Jensen Huang - Founder and CEO, NVIDIA NVIDIA | LinkedIn",
        "Issued Apr 2017 Credential ID W2X5B9J2"),
], company="NVIDIA", domain="nvidia.com")
ok(r["linkedin_url"].endswith("/jenhsunhuang") and r["confidence"] == "High",
   f"impostor: the real profile wins, outright (got {r['linkedin_url']} {r['confidence']})")

# The same impostor when the search shows nothing damning about it: identical
# headline, no connection count. The chosen address breaks the tie.
r = judge("Jensen Huang", "President and Chief Executive Officer", "executive", [
    res("https://www.linkedin.com/in/jensen-huang-89a60594", "Jensen Huang - Founder and CEO, NVIDIA | LinkedIn",
        "Founder and CEO, NVIDIA"),
    res("https://www.linkedin.com/in/jenhsunhuang", "Jensen Huang - Founder and CEO, NVIDIA | LinkedIn",
        "Founder and CEO, NVIDIA"),
], company="NVIDIA", domain="nvidia.com")
ok(r["linkedin_url"].endswith("/jenhsunhuang") and r["confidence"] == "High",
   f"impostor: a chosen profile address wins a tie with a generated one (got {r['linkedin_url']} {r['confidence']})")

# ── A sidebar names other people's employers: not evidence for this profile ─
r = judge("Ron Jadin", "Chief Financial Officer", "executive", [
    res("https://www.linkedin.com/in/ron-jadin-1", "Ron Jadin - Grainger | LinkedIn",
        "Experience: Grainger · Other similar profiles * [Colette Kress](https://www.linkedin.com/in/colette-kress-22328346) "
        "### Colette Kress NVIDIA 8K followers Saratoga"),
], company="NVIDIA", domain="nvidia.com")
ok(r["confidence"] == L.NOT_FOUND,
   "sidebar: a company named in another profile's sidebar is not this profile's employer")

# ...but it IS LinkedIn vouching for the person it names.
r = judge("Colette Kress", "EVP and Chief Financial Officer", "executive", [
    res("https://linkedin.com/in/colette-kress-22328346", "Colette Kress",
        "- name: Southern Methodist University  # Seniority - seniority: Chief X Officer (CxO)"),
    res("https://www.linkedin.com/in/ron-jadin-1", "Ron Jadin - Grainger | LinkedIn",
        "Other similar profiles * [Colette Kress](https://www.linkedin.com/in/colette-kress-22328346) "
        "### Colette Kress NVIDIA 8K followers Saratoga"),
    res("https://linkedin.com/in/colette-kessler", "Colette Kessler - United States", ""),
], company="NVIDIA", domain="nvidia.com")
ok(r["linkedin_url"].endswith("colette-kress-22328346") and r["confidence"] in ("High", "Medium"),
   f"sidebar: corroborates the named person's profile (got {r['confidence']})")

# ── A namesake in a junior role at the same company is not the executive ───
r = judge("Robert Ford", "Chief Executive Officer", "executive", [
    res("https://linkedin.com/in/robert-ford-sales", "Robert Ford - Sales Representative at Abbott | LinkedIn",
        "Experience: Abbott · 500+ connections"),
])
ok(r["confidence"] == L.NOT_FOUND, "junior: a sales rep at the company is not its CEO")

r = judge("Ann Lee", "Chief Operating Officer", "executive", [
    res("https://linkedin.com/in/ann-lee-ea", "Ann Lee - Executive Assistant to the CEO | LinkedIn",
        "Experience: Abbott"),
])
ok(r["confidence"] != "High", "junior: an executive ASSISTANT is not a leadership title")

# ── Nickname profile ────────────────────────────────────────────────────────
r = judge("Debora Shoquist", "EVP, Operations", "executive", [
    res("https://linkedin.com/in/debbie-shoquist-620a813", "Debbie Shoquist - EVP Operations NVIDIA", ""),
    res("https://linkedin.com/in/debbie-shoquist-other", "Debbie Shoquist - NVIDIA", ""),
], company="NVIDIA", domain="nvidia.com")
ok(r["linkedin_url"].endswith("620a813") and r["confidence"] == "High",
   f"nickname: Debbie Shoquist, EVP Operations at NVIDIA, is Debora Shoquist (got {r['confidence']})")

# ── Directors: outside careers ──────────────────────────────────────────────
r = judge("Sally E. Blount", "Director", "board", [
    res("https://linkedin.com/in/sallyblount", "Sally Blount - CEO, Catholic Charities of Chicago",
        "Board of Directors, Abbott Laboratories · CEO, Catholic Charities"),
])
ok(r["confidence"] == "High", f"board: outside headline + the company on the profile = High (got {r['confidence']})")

r = judge("Nancy McKinstry", "Director", "board", [
    res("https://www.linkedin.com/in/nancy-mckinstry-4a25009", "Nancy McKinstry - Board Director / Advisor | LinkedIn", ""),
    res("https://linkedin.com/in/nancy-mckinstry-b", "Nancy McKinstry - HSBC Bank Canada | LinkedIn", ""),
])
ok(r["confidence"] == "Low" and r["linkedin_url"].endswith("4a25009"),
   f"board: the only senior namesake, company not shown = Low, not High (got {r['confidence']})")

# "Director of Sales" is a functional title, not a board seat. A live NVIDIA
# run matched director Tench Coxe to a sales director with the same name.
r = judge("Tench Coxe", "Director", "board", [
    res("https://linkedin.com/in/tench-coxe-sales", "Tench Coxe - Director of Sales at Ripley's Mountain Adventures",
        "Denver, Colorado, United States 2 followers."),
], company="NVIDIA", domain="nvidia.com")
ok(r["confidence"] == L.NOT_FOUND, f"board: a Director OF SALES is not a board director (got {r['confidence']})")

# "Melissa Lora - LinkedIn": the heading has no headline; the role and the
# company are in the profile's own text. The same live run picked an HR
# namesake instead.
r = judge("Melissa B. Lora", "Director", "board", [
    res("https://www.linkedin.com/in/melissalora", "Melissa Lora - LinkedIn",
        "Experience Member Board of Directors NVIDIA Jul 2023 - Present Santa Clara, California"),
    res("https://linkedin.com/in/melissa-lora-hr", "Melissa Lora, BASc, PHR, SHRM-CP - HR & People ...",
        "I'm a people operations and systems leader."),
], company="NVIDIA", domain="nvidia.com")
ok(r["linkedin_url"].endswith("/melissalora") and r["confidence"] == "High",
   f"board: role + company read from the profile's own text (got {r['linkedin_url']} {r['confidence']})")
ok(L.parse_result_title("Melissa Lora - LinkedIn") == ("Melissa Lora", ""),
   "title: a trailing 'LinkedIn' segment is not a headline")

# ── Role restated exactly, company not shown ────────────────────────────────
r = judge("Timothy S. Teter", "EVP, General Counsel and Secretary", "executive", [
    res("https://www.linkedin.com/in/timothy-s-teter-0244604",
        "Timothy S. Teter - Executive Vice President and General ...", ""),
    res("https://linkedin.com/in/timothy-teter-2", "Timothy Teter - Building relationships ...", ""),
], company="NVIDIA", domain="nvidia.com")
ok(r["confidence"] == "Medium", f"role: headline restating the exact role = Medium (got {r['confidence']})")

# ── Two equally good, different profiles: say so ────────────────────────────
r = judge("Mary Major", "Chief Marketing Officer", "executive", [
    res("https://linkedin.com/in/mary-major-1", "Mary Major - Chief Marketing Officer at Abbott", ""),
    res("https://linkedin.com/in/mary-major-2", "Mary Major - Chief Marketing Officer at Abbott", ""),
])
ok(r["confidence"] == "Low" and "another profile fits" in r["matched_title"],
   f"ambiguous: two identical fits are flagged Low (got {r['confidence']})")

# ── Location-only headline is labelled as such ──────────────────────────────
r = judge("Daniel G. Salvadori", "EVP, Nutritional Products", "executive", [
    res("https://linkedin.com/in/danielsalvadori", "Daniel Salvadori - North Chicago, Illinois, United States",
        "Location: North Chicago · 500+ connections on LinkedIn."),
], hq="North Chicago, Illinois")
ok(r["confidence"] == "Low" and r["matched_title"].startswith("No title shown"),
   f"location: the only namesake at HQ, no title = Low, labelled (got {r['confidence']} / {r['matched_title']})")

# ── Company website link: a candidate the search can confirm or replace ─────
r = L.judge({"name": "Hubert L. Allen", "title": "General Counsel", "kind": "executive",
             "site_linkedin": "https://www.linkedin.com/in/hubert-allen-abt"}, [],
            "Abbott Laboratories", "abbott.com")
ok(r["linkedin_url"].endswith("hubert-allen-abt") and r["source"] == "company_website",
   "site: a company-website link stands when the search finds nothing")
r = L.judge({"name": "Hubert L. Allen", "title": "General Counsel", "kind": "executive",
             "site_linkedin": "https://www.linkedin.com/in/someone-else"}, [],
            "Abbott Laboratories", "abbott.com")
ok(r["confidence"] == L.NOT_FOUND, "site: a link whose slug isn't this person's name is not trusted")

# ── Batch: one profile cannot belong to two people; second pass only if weak ─
_calls: list[tuple[str, bool]] = []
_FAKE = {
    ("Jane Smith", False): [res("https://linkedin.com/in/jane-smith-acme", "Jane Smith - CEO at Acme")],
    ("Jan Smith", False): [res("https://linkedin.com/in/jane-smith-acme", "Jan Smith - CEO at Acme")],
    ("Lee Park", False): [],
    ("Lee Park", True): [res("https://linkedin.com/in/lee-park-acme", "Lee Park - Director at Acme")],
}


_modes: list[tuple[str, bool, str]] = []


def _fake_search(person, company, api_key, second_pass=False, mode="advanced"):
    _calls.append((person["name"], second_pass))
    _modes.append((person["name"], second_pass, mode))
    return _FAKE.get((person["name"], second_pass), [])


L._search = _fake_search
os.environ["PARALLEL_API_KEY"] = "test"
out = L.resolve_people([
    {"key": "a", "name": "Jane Smith", "title": "CEO", "kind": "executive"},
    {"key": "b", "name": "Lee Park", "title": "Director", "kind": "board"},
], "Acme Inc", "acme.com")
ok(out["a"]["confidence"] == "High", "batch: strong first-pass answer")
ok(("Jane Smith", True) not in _calls, "batch: a High answer does not pay for a second search")
ok(("Lee Park", True) in _calls and out["b"]["linkedin_url"].endswith("lee-park-acme"),
   "batch: an unresolved person gets a second, role-led search")
# Cost tiers: turbo first; advanced (the lite price) only for what turbo
# didn't settle; nothing else.
ok([m for n, sp, m in _modes if n == "Jane Smith"] == ["turbo"],
   "tiers: a person turbo settles costs one turbo search")
ok([(sp, m) for n, sp, m in _modes if n == "Lee Park"]
   == [(False, "turbo"), (False, "advanced"), (True, "advanced")],
   "tiers: escalation is turbo -> advanced -> advanced role-led")
ok({m for _, _, m in _modes} <= {"turbo", "advanced"}, "tiers: no other search mode is ever used")

import parallel_client as PC
for asked, want in (("core", "base-fast"), ("pro", "base-fast"), ("ultra8x", "base-fast"),
                   ("core-fast", "base-fast"), ("", "base-fast"), ("base", "base"),
                   ("base-fast", "base-fast"), ("lite", "lite")):
    os.environ["ORGANOGRAM_PARALLEL_PROCESSOR"] = asked
    ok(PC.task_processor() == want, f"tiers: processor {asked!r} -> {want} (capped at the base price)")
ok(PC.TASK_COST["base-fast"] == PC.TASK_COST["base"], "tiers: base-fast costs the same as base")
os.environ.pop("ORGANOGRAM_PARALLEL_PROCESSOR")

os.environ["PARALLEL_API_KEY"] = ""
ok(L.resolve_people([{"key": "x", "name": "A B", "title": "", "kind": "board"}], "Acme") == {},
   "batch: no API key -> nothing searched, nothing guessed")

# ── Pipeline: the chart's Board + EM members get profiles ───────────────────
import structural_engine as S
from fastapi.testclient import TestClient
import api_server as A
os.environ["PARALLEL_API_KEY"] = ""          # .env is loaded by api_server; no live calls
client = TestClient(A.app)

job = client.post("/company-chart", json={"company_name": "Acme Inc", "domain": "acme.com",
                                          "hq_location": "Springfield"}).json()["job_id"]
dag = A._JOBS[job].dag
import uuid as _uuid
from inference_logic import ClassifiedRecord
for name, title, dept, layer, li in [
    ("Jane Smith", "Chairman and Chief Executive Officer", S.EXEC_DEPT, 1, ""),
    ("Jane Smith", "Chairman of the Board", S.BOARD_DEPT, 0, ""),
    ("Lee Park", "Independent Director", S.BOARD_DEPT, 2, ""),
    ("Omar Diaz", "Executive Vice President and Chief Financial Officer", S.EXEC_DEPT, 1,
     "https://www.linkedin.com/in/omar-from-upload"),
    ("Kim Tran", "Chief Technology Officer", S.EXEC_DEPT, 1, ""),
    ("Al Fellow", "Co-founder and Acme Fellow", S.EXEC_DEPT, 1, ""),
    ("Deb Ops", "Executive Vice President of Operations", S.EXEC_DEPT, 1, ""),
]:
    dag.insert_person(ClassifiedRecord(
        id="llm_" + _uuid.uuid4().hex[:8], full_name=name, designation=title,
        company="Acme Inc", linkedin_url=li, location="", country="", sector="Private",
        region="Global HQ", layer=layer, dept_primary=dept, dept_secondary="",
        dept_tertiary="", nlp_confidence=0.9, nlp_industry="llm",
        nlp_method="llm_leadership_web"))
dag.repair_governance_edges()
S.split_executive_departments(dag)

os.environ["PARALLEL_API_KEY"] = "test"
_calls.clear()
summary = S.resolve_leadership_linkedin(dag, "Acme Inc", "acme.com")
by_name: dict[str, list[dict]] = {}
for nid, attrs in dag.G.nodes(data=True):
    if attrs.get("node_type") == "person":
        by_name.setdefault(attrs.get("label"), []).append(attrs.get("metadata", {}))

ok([n for n, _ in _calls].count("Jane Smith") == 1,
   "pipeline: a person in both panels is searched once")
ok(len(by_name["Jane Smith"]) == 2 and all(
    m.get("linkedin_url", "").endswith("jane-smith-acme") for m in by_name["Jane Smith"]),
   "pipeline: both of the dual-role CEO's nodes get the profile")
ok(by_name["Omar Diaz"][0].get("linkedin_url") == "https://www.linkedin.com/in/omar-from-upload"
   and by_name["Omar Diaz"][0].get("linkedin_confidence") == "Provided",
   "pipeline: a URL from the user's data is kept, marked Provided, never searched")
ok("Omar Diaz" not in [n for n, _ in _calls], "pipeline: the uploaded profile is not re-searched")
ok(by_name["Kim Tran"][0].get("linkedin_confidence") == L.NOT_FOUND,
   "pipeline: an unresolved executive is recorded as Not Found, explicitly")
ok(summary["provided"] == 1 and summary["found"] >= 2, f"pipeline: summary counts ({summary})")

_calls.clear()
S.resolve_leadership_linkedin(dag, "Acme Inc", "acme.com")
ok(not _calls, "pipeline: a second run only searches people not yet resolved")

# ── Each executive shows the department they run ────────────────────────────
ok(by_name["Omar Diaz"][0].get("leads_department") == "Finance & Accounting",
   f"department: the CFO is marked as leading Finance & Accounting "
   f"(got {by_name['Omar Diaz'][0].get('leads_department')})")
ok(by_name["Kim Tran"][0].get("leads_department") == S._exec_department("Chief Technology Officer")
   and by_name["Kim Tran"][0].get("leads_department_id") in dag.G,
   "department: the CTO is marked with the taxonomy's department, and that node exists")
# A title that names no function runs no department. The classifier's
# last-resort Operations guess made an NVIDIA co-founder/Fellow the head of
# Operations, ahead of the EVP of Operations.
ok(not by_name["Al Fellow"][0].get("leads_department"),
   "department: 'Co-founder and Fellow' leads no department (no guessed Operations)")
_ops = dag.G.nodes[S._dept_node_id("Operations")].get("metadata", {})
ok(_ops.get("head_name") == "Deb Ops",
   f"department: Operations is led by the EVP of Operations (got {_ops.get('head_name')})")
board_meta = [m for m in by_name["Lee Park"]]
ok(not board_meta[0].get("leads_department"), "department: a director leads no department")

# ── Report endpoint, in the requested table format ──────────────────────────
rep = client.get("/linkedin-report", params={"job_id": job}).json()
cols = rep["columns"]
ok(cols == ["Full Name", "Expected Title/Role", "Company", "LinkedIn Profile URL",
            "Confidence Level", "Matched Name and Title on LinkedIn"], "report: the six requested columns")
names = [r["Full Name"] for r in rep["rows"]]
ok(names.count("Jane Smith") == 1, "report: the dual-role CEO is one row")
ok(rep["rows"][0]["Panel"].startswith("Board"), "report: board rows first")
kim = next(r for r in rep["rows"] if r["Full Name"] == "Kim Tran")
ok(kim["LinkedIn Profile URL"] == "Not Found" and kim["Confidence Level"] == L.NOT_FOUND,
   "report: unresolved -> 'Not Found', not blank")
ok(rep["markdown"].splitlines()[0].startswith("| Full Name |") and len(rep["markdown"].splitlines()) == len(rep["rows"]) + 2,
   "report: markdown table has a header, a rule and one line per person")

ready = client.get("/leadership-ready", params={"job_id": job}).json()
ok("linkedin_done" in ready and "linkedin" in ready, "ready: LinkedIn progress is reported")

os.environ["PARALLEL_API_KEY"] = ""
print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
