"""
Accenture: wrong leader, missing board. And a web-search backup for
organisations that publish no leadership at all.

From production, October 2026:
  - The leader of Executive Management was "Representative Chairman of
    Accenture Japan and Co-CEO of Asia Pacific" — not Julie Sweet, Chair and
    CEO. Both titles contain "CEO", and the tie went alphabetically.
  - The board came back empty. Gemini was out of credits (HTTP 402), the
    Claude cross-check only read the first three chunks, and the board page
    was the eighth page read. accenture.com 404s /sitemap.xml and declares
    /sitemap-index.xml in robots.txt; the board page is in
    investor.accenture.com's own sitemap, which was never read.
  - Pages under www.accenture.com and accenture.com were each read twice.

Run: python3 backend/test_accenture_and_backup.py
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

# ── The leader: the Chair and CEO, not a co-CEO or a regional CEO ───────────
for title, apex in [("Chair and CEO", True),
                    ("Representative Chairman of Accenture Japan and Co-CEO of Asia Pacific", False),
                    ("CEO – Americas", False), ("CEO – EMEA", False),
                    ("Co-CEO of Asia Pacific and CEO of Asia Oceania", False),
                    ("Co-CEO", False), ("Chief Executive Officer, Australia", False),
                    ("Co-founder and CEO", True), ("Group CEO", True)]:
    ok(S._is_ceo(title) == apex, f"apex test: {title!r} -> {apex}")

job = client.post("/company-chart", json={"company_name": "Accenture", "domain": "",
                                          "hq_location": "Dublin, Ireland"}).json()["job_id"]
dag = A._JOBS[job].dag
for name, title, dept, layer in [
    ("Atsushi Egawa", "Representative Chairman of Accenture Japan and Co-CEO of Asia Pacific", S.EXEC_DEPT, 1),
    ("Angie Park", "Chief Financial Officer", S.EXEC_DEPT, 1),
    ("Julie Sweet", "Chair and CEO", S.EXEC_DEPT, 1),
    ("John Walsh", "CEO – Americas", S.EXEC_DEPT, 2),
    ("Kate Hogan", "Chief Operating Officer", S.EXEC_DEPT, 1),
    ("Julie Sweet", "Chair & CEO", S.BOARD_DEPT, 0),
    ("Arun Sarin", "Independent Lead Director", S.BOARD_DEPT, 1),
]:
    dag.insert_person(ClassifiedRecord(
        id="llm_" + uuid.uuid4().hex[:8], full_name=name, designation=title, company="Accenture",
        linkedin_url="", location="", country="", sector="Private", region="Global HQ",
        layer=layer, dept_primary=dept, dept_secondary="", dept_tertiary="", nlp_confidence=0.9,
        nlp_industry="llm", nlp_method="llm_leadership_web"))
dag.repair_governance_edges()
S.split_executive_departments(dag)
apex = [a["label"] for _, a in dag.G.nodes(data=True)
        if a.get("node_type") == "person" and (a.get("metadata") or {}).get("is_apex")]
ok(apex == ["Julie Sweet"], f"apex: Julie Sweet leads Executive Management (got {apex})")
em = dag.G.nodes[S._dept_node_id(S.EXEC_DEPT)].get("metadata", {})
ok(em.get("head_name") == "Julie Sweet", "apex: the Executive Management card names Julie Sweet")

# Two genuine candidates: the one on the board wins, not the alphabetically first.
j2 = client.post("/company-chart", json={"company_name": "Two CEOs Co", "domain": "",
                                         "hq_location": ""}).json()["job_id"]
d2 = A._JOBS[j2].dag
for name, title, dept, layer in [("Aaron Able", "Chief Executive Officer, Products", S.EXEC_DEPT, 1),
                                 ("Zoe Zeal", "Chief Executive Officer", S.EXEC_DEPT, 1),
                                 ("Zoe Zeal", "Director", S.BOARD_DEPT, 2)]:
    d2.insert_person(ClassifiedRecord(
        id="llm_" + uuid.uuid4().hex[:8], full_name=name, designation=title, company="X",
        linkedin_url="", location="", country="", sector="Private", region="Global HQ",
        layer=layer, dept_primary=dept, dept_secondary="", dept_tertiary="", nlp_confidence=0.9,
        nlp_industry="llm", nlp_method="llm_leadership_web"))
d2.repair_governance_edges()
S.split_executive_departments(d2)
apex2 = [a["label"] for _, a in d2.G.nodes(data=True)
         if a.get("node_type") == "person" and (a.get("metadata") or {}).get("is_apex")]
ok(apex2 == ["Zoe Zeal"], f"apex: the CEO who also sits on the board wins (got {apex2})")

# ── Sitemaps: robots.txt, index spellings, the investor site ────────────────
class _R:
    def __init__(self, code, text): self.status_code, self.text = code, text


PAGES = {
    "https://www.acme.com/robots.txt": _R(200, "User-agent: *\nSitemap: https://www.acme.com/sitemap-index.xml\n"),
    "https://www.acme.com/sitemap-index.xml": _R(200, "<sitemapindex><sitemap><loc>https://www.acme.com/en-us/pages.xml</loc></sitemap>"
                                                     "<sitemap><loc>https://www.acme.com/products.xml</loc></sitemap></sitemapindex>"),
    "https://www.acme.com/en-us/pages.xml": _R(200, "<urlset>"
        "<url><loc>https://www.acme.com/ae-en/about/leadership</loc></url>"
        "<url><loc>https://www.acme.com/us-en/about/leadership</loc></url>"
        "<url><loc>https://www.acme.com/au-en/about/leadership</loc></url>"
        "<url><loc>https://www.acme.com/us-en/about/leadership-emea</loc></url>"
        "<url><loc>https://www.acme.com/us-en/about/leadership/jane-roe</loc></url></urlset>"),
    "https://www.acme.com/products.xml": _R(200, "<urlset><url><loc>https://www.acme.com/products/board-games</loc></url></urlset>"),
    "https://investor.acme.com/sitemap.xml": _R(200, "<urlset><url><loc>https://investor.acme.com/corporate-governance/board-of-directors</loc></url></urlset>"),
}
import httpx
_orig_get = httpx.get
httpx.get = lambda url, **kw: PAGES.get(url, _R(404, "not found"))
urls = F._sitemap_leadership_urls("acme.com")
httpx.get = _orig_get
ok("https://investor.acme.com/corporate-governance/board-of-directors" in urls,
   "sitemap: the investor site's sitemap is read, and its board page found")
ok(urls and "board-of-directors" in urls[0], f"sitemap: the board page is first in line ({urls[:2]})")
ok(sum("/about/leadership" in u and "jane" not in u and "emea" not in u for u in urls) == 1
   and "https://www.acme.com/us-en/about/leadership" in urls,
   "sitemap: one copy per page across country editions, the US edition preferred")
ok(not any("leadership-emea" in u for u in urls), "sitemap: a regional team's page is skipped")
ok(not any("board-games" in u for u in urls), "sitemap: product sitemaps are not read")

# ── The same page under www. and the bare host is read once ─────────────────
_orig_bounded = F._get_bounded
F._get_bounded = lambda url, timeout=6, final_url=None: (
    final_url.append(url) if final_url is not None else None) or (
    "<h2>Leadership</h2><p>Jane Roe, Chief Executive Officer</p>" + " filler" * 80)
h = F._Harvester()
ok(h.fetch("https://www.acme.com/en/about/leadership"), "dedupe: first copy harvested")
ok(not h.fetch("https://acme.com/en/about/leadership"), "dedupe: the bare-host copy is not read again")
F._get_bounded = _orig_bounded

# ── Gemini out of credits: Claude reads every chunk instead ─────────────────
claude_calls = []
F._gemini_discover_leadership_urls = lambda *a, **k: ([], "")
F._fetch_static_leadership = lambda domain, harvester=None, seed_url="": [
    harvester.blocks.append(f"[Page: https://acme.com/p{i}]\nPerson {i} — Director") or
    harvester.canonical_blocks.append(f"[Page: https://acme.com/p{i}]\nPerson {i} — Director")
    for i in range(3)]
F._chunk_for_synthesis = lambda text: ["chunk-a", "chunk-b", "chunk-c"]
F._gemini_synthesize_chunk = lambda *a, **k: None       # HTTP 402 on every chunk
F._corroborate_canonical = lambda *a, **k: []
F._gemini_board_retry = lambda *a, **k: {}
F._call_claude = lambda system, user_msg, label, source_text="": (
    claude_calls.append(source_text) or
    {"board": [{"name": {"a": "Ann Abbot", "b": "Ben Brook", "c": "Cara Cole"}[source_text[-1]],
                "title": "Director"}],
     "executives": [], "senior_leadership": []})
res = F._gemini_fetch_leadership("Acme", "acme.com", "key")
ok(sorted(claude_calls) == ["chunk-a", "chunk-b", "chunk-c"],
   f"gemini down: Claude reads every chunk, not just three canonical ones ({claude_calls})")
ok(len(res.get("board", [])) == 3 and res["_harvest"]["stages"]["gemini_failed_chunks"] == 3,
   "gemini down: the board is extracted, and diagnostics count the failed chunks")

# ── Parallel backup: only for thin sections, never padding a full team ──────
calls = []


def _fake_parallel(company, domain, location, want_board, want_execs):
    calls.append((want_board, want_execs))
    return {"board": [{"name": "Xu Yangtian", "title": "Chairman"}],
            "executives": [{"name": "Lei Gui", "title": "Chief Financial Officer"}]}


F._parallel_leadership = _fake_parallel
os.environ["PARALLEL_API_KEY"] = "test"
full_team = [{"name": n, "title": "EVP"} for n in
             ("Ann Abbot", "Ben Brook", "Cara Cole", "Dev Dutt", "Eli Egan")]
r = F._with_parallel_backup({"board": [], "executives": list(full_team)}, "SHEIN", "shein.com")
ok(calls == [(True, False)], f"backup: asked only for the missing board ({calls})")
ok([p["name"] for p in r["board"]] == ["Xu Yangtian"] and len(r["executives"]) == 5,
   "backup: board filled, the complete executive team left as it was")
calls.clear()
r = F._with_parallel_backup({"board": [{"name": n, "title": "Director"} for n in
                                       ("Fay Fox", "Gil Grey", "Hal Hart", "Ivy Ince")],
                             "executives": list(full_team)}, "Acme", "acme.com")
ok(calls == [] and "parallel_backup" not in r.get("_harvest", {}),
   "backup: not run when both sections are already found")
os.environ["PARALLEL_API_KEY"] = ""
calls.clear()
F._with_parallel_backup({"board": [], "executives": []}, "Acme", "acme.com")
ok(calls == [], "backup: no API key -> no search")

# ── Directors who run the business are executives too ───────────────────────
# SHEIN's founders appear as "Executive Director and Chief Operating Officer";
# whether the extraction also listed them as executives varied run to run.
out = F._promote_operating_directors({
    "board": [{"name": "Yangtian Xu", "title": "Chairman of the Board, Executive Director and Chief Executive Officer"},
              {"name": "Miao Miao", "title": "Executive Director and Chief Operating Officer"},
              {"name": "Xiaoqing Ren", "title": "Executive Director"},
              {"name": "Hongbin Cai", "title": "Independent Non-Executive Director"}],
    "executives": []})
names = [p["name"] for p in out["executives"]]
ok(names == ["Yangtian Xu", "Miao Miao"],
   f"directors: CEO and COO directors join Executive Management; bare/independent ones don't ({names})")
ok(out["executives"][1]["title"] == "Chief Operating Officer",
   "directors: the executive panel shows the operating title")
again = F._promote_operating_directors(out)
ok(len(again["executives"]) == 2, "directors: never added twice")

# ── A regional head runs a territory, not a department ──────────────────────
for title, dept in [("Co-CEO of Asia Pacific and CEO of Asia Oceania", ""),
                    ("CEO – Americas", ""),
                    ("Chief Financial Officer – EMEA", S._exec_department("Chief Financial Officer"))]:
    got = S._exec_department(title)
    ok(got == dept, f"dept: {title!r} -> {dept!r} (got {got!r})")

# ── One concurrent round: two Parallel passes + scraping, union ────────────
# The sequential pipeline (list → who's missing → scraping → current-role
# check) took ~11.5 minutes on Accenture. Now everything runs at once inside a
# budget; scraped names count only when a roster page names them.
import importlib, time as _time
F2 = importlib.reload(F)
os.environ["PARALLEL_API_KEY"] = "test"
calls = []
HI = {"_conf": "high", "_evidenced": True}
BOARD11 = [{"name": n, "title": "Director", **HI} for n in (
    "Tench Coxe", "John Dabiri", "Jensen Huang", "Dawn Hudson", "Harvey Jones", "Melissa Lora",
    "Stephen Neal", "Brooke Seawell", "Aarti Shah", "Mark Stevens", "Suzanne Nora Johnson")]


def _fake_tasks(company, domain="", location="", site_url="", variant="pages",
                sections=("board", "executives"), processor=""):
    calls.append(variant)
    if processor == "base":
        return {"board": list(BOARD11),
                "executives": [{"name": "Jensen Huang", "title": "President and CEO", **HI},
                               {"name": "Colette Kress", "title": "CFO", **HI}],
                "_parallel": {"ran": True}}
    return {"board": [{"name": "Persis Drell", "title": "Director", **HI},
                      {"name": "Tench Coxe", "title": "Director", **HI}],          # already listed
            "executives": [{"name": "Debora Shoquist", "title": "EVP, Operations", **HI},
                           {"name": "Colette M. Kress", "title": "EVP and CFO", **HI}],  # same person
            "_parallel": {"ran": True}}


ROSTER = ("[Page: https://nvidia.com/leadership] Jensen Huang, President and CEO. Colette Kress, "
          "CFO. Michael Kagan, Chief Technology Officer. Debora Shoquist, EVP Operations.")
STALE_BIO = "[Page: https://nvidia.com/board/rob-burgess] Rob Burgess has served on the board."


def _fake_scrape(company, domain="", site_url="", location="", search_backup=True):
    calls.append("scrape")
    return {"board": [{"name": "Rob Burgess", "title": "Director"}],
            "executives": [{"name": "Michael Kagan", "title": "Chief Technology Officer"},
                           {"name": "Jensen Huang", "title": "CEO"},
                           {"name": "Colette Kress", "title": "CFO"},
                           {"name": "Debora Shoquist", "title": "EVP"}],
            "_blocks": [ROSTER, STALE_BIO]}


F2._parallel_task_leadership = _fake_tasks
F2._scrape_leadership = _fake_scrape
F2._LEADERSHIP_CACHE.clear()
t0 = _time.monotonic()
res = F2.llm_fetch_leadership("NVIDIA", "nvidia.com", location="Santa Clara")
board = [p["name"] for p in res["board"]]
execs = [p["name"] for p in res["executives"]]
ok(sorted(calls) == ["filings", "pages", "pages", "pages", "scrape"],
   f"round: the base run, three fast runs and scraping run at once ({calls})")
ok("Persis Drell" in board and board.count("Tench Coxe") == 1 and len(board) == 12,
   f"union: a director the base run missed, agreed by the fast runs, is added once ({len(board)})")
ok(execs.count("Colette Kress") + execs.count("Colette M. Kress") == 1 and "Debora Shoquist" in execs,
   f"union: the same person under two spellings is one entry ({execs})")
ok("Michael Kagan" in execs, "scraping: a name on a roster page is added")
ok("Rob Burgess" not in board,
   "scraping: a name found only on its own (stale) bio page is not added")

# Voting, on the real NVIDIA pattern: stale directors come back medium-
# confidence without a supporting excerpt, in one run.
def _vote_tasks(company, domain="", location="", site_url="", variant="pages",
                sections=("board", "executives"), processor=""):
    MED = {"_conf": "medium", "_evidenced": False}
    if processor == "base":
        return {"board": [], "executives": [], "_parallel": {"ran": False}}   # base is late
    run = len([c for c in calls if c == "vote"])
    calls.append("vote")
    board = [{"name": "Tench Coxe", "title": "Director", **HI},
             {"name": "Melissa Lora", "title": "Director", **(HI if run == 1 else MED)},
             {"name": "Kevin Allen", "title": "Director", **MED}]          # all 3 runs, never high
    if run == 2:
        board += [{"name": "Persis Drell", "title": "Director", **MED},      # stale, once
                  {"name": "Ellen Ochoa", "title": "Director", **MED}]
    if run in (0, 2):
        board.append({"name": "Ellen Ochoa", "title": "Director",
                      **({"_conf": "medium", "_evidenced": True} if run == 0 else MED)})
    return {"board": board, "executives": [], "_parallel": {"ran": True}}


calls.clear()
F2._parallel_task_leadership = _vote_tasks
F2._scrape_leadership = lambda *a, **k: {}
F2._LEADERSHIP_CACHE.clear()
res = F2.llm_fetch_leadership("NVIDIA", "nvidia.com", location="Santa Clara")
vb = [p["name"] for p in res["board"]]
ok("Tench Coxe" in vb and "Melissa Lora" in vb,
   f"vote: high-confidence, evidenced names are kept ({vb})")
ok("Kevin Allen" in vb, "vote: a name every run lists is kept even without high confidence")
ok("Persis Drell" not in vb and "Ellen Ochoa" not in vb,
   "vote: a stale director (medium, in 1-2 of 3 runs) is rejected")

# With the base run in, a fast-only name needs two fast runs AND evidence.
def _authority_tasks(company, domain="", location="", site_url="", variant="pages",
                     sections=("board", "executives"), processor=""):
    MED = {"_conf": "medium", "_evidenced": False}
    if processor == "base":
        return {"board": [{"name": "Harvey Jones", "title": "Director", **MED},
                          {"name": "Stephen Neal", "title": "Director", **MED}],
                "executives": [], "_parallel": {"ran": True}}
    run = len([c for c in calls if c == "auth"])
    calls.append("auth")
    board = [{"name": "Robert Burgess", "title": "Director", **(HI if run == 0 else MED)}]
    if run in (0, 1):
        board.append({"name": "Aarti Shah", "title": "Director", **HI})
    return {"board": board, "executives": [], "_parallel": {"ran": True}}


calls.clear()
F2._parallel_task_leadership = _authority_tasks
F2._LEADERSHIP_CACHE.clear()
ab = [p["name"] for p in F2.llm_fetch_leadership("NVIDIA", "nvidia.com", location="X")["board"]]
ok("Harvey Jones" in ab and "Stephen Neal" in ab,
   f"authority: everything the base run lists is kept, whatever its confidence ({ab})")
ok("Aarti Shah" in ab, "authority: a fast-only name evidenced in 2 runs is added")
ok("Robert Burgess" not in ab,
   "authority: a name the base run left out and only one fast run is sure of is rejected")
F2._parallel_task_leadership = _fake_tasks

# A source slower than the budget is not waited for.
calls.clear()


def _slow_scrape(*a, **k):
    _time.sleep(3)
    return {"board": [{"name": "Late Person", "title": "Director"}], "executives": [],
            "_blocks": []}


F2._scrape_leadership = _slow_scrape
F2._RESEARCH_BUDGET_S = 1
F2._LEADERSHIP_CACHE.clear()
t0 = _time.monotonic()
res = F2.llm_fetch_leadership("NVIDIA", "nvidia.com", location="Santa Clara")
took = _time.monotonic() - t0
ok(took < 2.5 and "scrape" in res["_harvest"]["late"] and
   not any(p["name"] == "Late Person" for p in res["board"]),
   f"budget: a late source is skipped, not waited for ({took:.1f}s, late={res['_harvest']['late']})")
os.environ["PARALLEL_API_KEY"] = ""

# ── Board levels: committee chairs and the lead director sit under the Chair ─
jb = client.post("/company-chart", json={"company_name": "Board Co", "domain": "",
                                         "hq_location": ""}).json()["job_id"]
db_ = A._JOBS[jb].dag
_orig_fetch = F2.llm_fetch_leadership
for name, title, layer, comm in [
        ("Julie Sweet", "Chair & CEO", 0, []),
        ("Arun Sarin", "Independent Lead Director", 1, []),
        ("Tracey Travis", "Independent Director", 1, [{"name": "Audit Committee", "role": "Chair"}]),
        ("Paula Price", "Independent Director", 2, [{"name": "Audit Committee", "role": "Member"}])]:
    rec = ClassifiedRecord(
        id="llm_" + uuid.uuid4().hex[:8], full_name=name, designation=title, company="X",
        linkedin_url="", location="", country="", sector="Private", region="Global HQ",
        layer=layer, dept_primary=S.BOARD_DEPT, dept_secondary="", dept_tertiary="",
        nlp_confidence=0.9, nlp_industry="llm", nlp_method="llm_leadership_web")
    db_.insert_person(rec)
_four = {"Julie Sweet", "Arun Sarin", "Tracey Travis", "Paula Price"}
lv = {a["label"]: a.get("layer") for _, a in db_.G.nodes(data=True)
      if a.get("node_type") == "person" and a["label"] in _four}
ok(lv == {"Julie Sweet": 0, "Arun Sarin": 1, "Tracey Travis": 1, "Paula Price": 2},
   f"board levels: chair 0, lead director and committee chair 1, member 2 ({lv})")
ok(S._is_board_chairman("Chair & CEO") and not S._is_board_chairman("Chairman of Accenture Japan"),
   "board chair: 'Chair & CEO' is the chair; 'Chairman of Accenture Japan' is regional")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
