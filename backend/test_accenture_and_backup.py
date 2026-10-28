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

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
