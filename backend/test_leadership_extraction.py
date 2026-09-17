"""Functional tests for the improved leadership extraction. No network."""
import json, sys, types
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_fallback as L

ok = lambda c, m: print(("PASS  " if c else "FAIL  ") + m) or (0 if c else fails.append(m))
fails = []

# ── 1. img alt / aria-label names (photo-grid leadership pages) ──────────────
html = '''
<img src="a.jpg" alt="Jane Okonjo, Chief Financial Officer">
<img src="logo.png" alt="Company logo">
<a aria-label="Raj Patel - Group General Counsel" href="/x">more</a>
<img src="b.jpg" alt="arrow icon">
'''
attrs = L._extract_attr_names(html)
ok("Jane Okonjo" in attrs, "attr: name from img alt")
ok("Raj Patel" in attrs, "attr: name from aria-label")
ok("Company logo" not in attrs and "arrow" not in attrs, "attr: noise rejected")

# ── 2. JSON-LD @graph + OrganizationRole ─────────────────────────────────────
ld = {"@graph": [
    {"@type": "Organization", "member": [
        {"@type": "OrganizationRole", "roleName": "Chair of the Board",
         "member": {"@type": "Person", "name": "Ana Silva"}},
        {"@type": "Person", "name": "Tom Reed", "jobTitle": "Chief Executive Officer"}]},
    {"@type": "Person", "name": "Li Wei", "jobTitle": "Chief Technology Officer"}]}
out = L._extract_json_ld(f'<script type="application/ld+json">{json.dumps(ld)}</script>')
ok("Ana Silva — Chair of the Board" in out, "ld+json: @graph OrganizationRole")
ok("Tom Reed" in out and "Li Wei" in out, "ld+json: @graph nested + top-level Person")

# ── 3. embedded JSON, mixed key spellings, deep nesting ──────────────────────
deep = {"props": {"pageProps": {"data": {"content": {"blocks": {"board": [
    {"fullName": "Maria Gomez", "positionTitle": "Independent Director"},
    {"DisplayName": "Kenji Sato", "designation": "Non-Executive Director"}]}}}}}}
js = L._extract_js_data(
    f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(deep)}</script>')
ok("Maria Gomez — Independent Director" in js, "js: positionTitle key, depth 7")
ok("Kenji Sato" in js, "js: case-insensitive DisplayName key")

# ── 4. bio-link following (same host only) ───────────────────────────────────
page = '''
<a href="/leadership/jane-okonjo">Jane Okonjo</a>
<a href="/about/team/raj-patel/">Raj Patel</a>
<a href="https://linkedin.com/in/x">LinkedIn</a>
<a href="/reports/annual.pdf">Annual report</a>
<a href="/careers">Careers</a>
'''
links = L._bio_links(page, "https://www.acme.com/leadership")
ok(any("jane-okonjo" in l for l in links), "bio: follows person link")
ok(any("raj-patel" in l for l in links), "bio: follows /team/ link")
ok(not any("linkedin.com" in l for l in links), "bio: off-host rejected")
ok(not any(".pdf" in l for l in links), "bio: pdf rejected")

# ── 5. chunking: page-boundary split, chunk cap ──────────────────────────────
big = "\n".join(f"[Page: https://acme.com/p{i}]\n" + ("x" * 9000) for i in range(6))
chunks = L._chunk_for_synthesis(big)
ok(len(chunks) > 1, f"chunk: split into {len(chunks)} (was one 22K truncation)")
ok(all(len(c) <= L._SYNTHESIS_CHUNK for c in chunks), "chunk: each within cap")
ok(len(chunks) <= L._MAX_SYNTHESIS_CHUNKS, "chunk: honours max-chunk ceiling")
kept = sum(len(c) for c in chunks)
ok(kept > 22_000, f"chunk: keeps {kept} chars vs 22000 before")

# ── 6. truncated-JSON recovery ───────────────────────────────────────────────
truncated = ('{"board": [{"name": "Ana Silva", "title": "Chair"}, '
             '{"name": "Tom Reed", "title": "Director"}, {"name": "Li W')
rec = L._loads_lenient(truncated)
ok(rec is not None and len(rec["board"]) == 2, "lenient: recovers 2 of 3 from cut-off JSON")
ok(L._loads_lenient('{"board": []}')["board"] == [], "lenient: valid JSON unaffected")

# ── 7. merge + dedupe across chunks ──────────────────────────────────────────
r1 = {"board": [{"name": "Ana Silva", "title": "Chair"}], "executives": [], "senior_leadership": []}
r2 = {"board": [{"name": "Ana  Silva", "title": "Chair of the Board",
                 "director_type": "Independent"},
                {"name": "Kenji Sato", "title": "Director"}],
      "executives": [{"name": "Tom Reed", "title": "CEO"}], "senior_leadership": []}
m = L._merge_leadership([r1, r2])
ok(len(m["board"]) == 2, f"merge: deduped to 2 board (got {len(m['board'])})")
ana = [b for b in m["board"] if "Ana" in b["name"]][0]
ok(ana["title"] == "Chair of the Board", "merge: keeps richer entry")
ok(len(m["executives"]) == 1, "merge: executives unioned")

# ── 7b. name-alias dedupe (real duplicates seen in production) ───────────────
dupes = [{"name": 'William "Bill" Brown', "title": "Chairman and CEO"},
         {"name": "Bill Brown", "title": "Chairman and CEO of 3M"},
         {"name": "Dr. John Banovetz", "title": "EVP, CTO"},
         {"name": "John Banovetz", "title": "Executive Vice President, CTO and ER"},
         {"name": "Wendy Bauer", "title": "Group President"}]
md = L._merge_leadership([{"executives": [p]} for p in dupes])
ok(len(md["executives"]) == 3, f'alias: 5 entries -> 3 people (got {len(md["executives"])})')
ok(L._name_aliases("Neil G. Mitchill, Jr.") == L._name_aliases("Neil Mitchill"),
   "alias: suffix + middle initial collapse")
ok("bill brown" in L._name_aliases('William "Bill" Brown'), "alias: nickname key")
ok("william brown" in L._name_aliases('William "Bill" Brown'), "alias: legal-name key")
ok(L._name_aliases("Ana Silva") != L._name_aliases("Ana Costa"), "alias: distinct people stay distinct")

# ── 7c. duplicates the live deploy actually produced ────────────────────────
ok("bill brown" in L._name_aliases("William \u201cBill\u201d Brown"),
   "alias: curly-quote nickname (ascii-fold used to eat the quotes)")
live = [{"name": "Bill Brown", "title": "Chairman and CEO"},
        {"name": "William \u201cBill\u201d Brown", "title": "Chairman and CEO"},
        {"name": "Christian Goralski", "title": "Group President of Safety & Industrial"},
        {"name": "Chris Goralski", "title": "Group President, Safety & Industrial"},
        {"name": "Dr. John Banovetz", "title": "EVP CTO"},
        {"name": "John Banovetz", "title": "EVP, CTO and Environmental Responsibility"},
        {"name": "Wendy Bauer", "title": "Group President"}]
ml = L._merge_leadership([{"executives": [p]} for p in live])
ok(len(ml["executives"]) == 4,
   f'live: 7 production entries -> 4 people (got {len(ml["executives"])})')
ok(L._prefix_match({"ana silva"}, {"anton silva": 0}) is None,
   "prefix: distinct given names not merged")
ok(L._prefix_match({"chris goralski"}, {"christian goralski": 0}) == 0,
   "prefix: unquoted nickname merged")

# ── 7d. stale-executive resolution ──────────────────────────────────────────
ok(L._role_keys("Chairman and CEO") == {"ceo", "chairman"}, "role: dual role recognised")
ok(L._role_keys("Executive Vice President, Chief Information and Digital Officer") == {"cio"},
   "role: EVP prefix stripped, not read as scope qualifier")
ok(L._role_keys("Group President, Consumer") == set(), "role: BU president is not singular")
ok(L._role_keys("CEO Australia") == set(), "role: regional CEO is not singular")
ok(L._role_keys("Deputy CFO") == set() and L._role_keys("Vice Chairman") == set(),
   "role: deputy/vice excluded")
ok(L._role_keys("Chair of the Audit Committee") == set(), "role: committee chair excluded")

canon = ("Bill Brown Chairman and Chief Executive Officer\n"
         "Bobby George Chief Information and Digital Officer\n"
         "Jennifer Cunningham Chief Human Resources Officer")
stale = {"executives": [
    {"name": "Bill Brown", "title": "Chairman and CEO", "_mentions": 4},
    {"name": "Michael Roman", "title": "Executive Chairman of the Board", "_mentions": 1},
    {"name": "Bobby George", "title": "EVP, Chief Information and Digital Officer", "_mentions": 3},
    {"name": "Mark Murphy", "title": "EVP, Chief Information and Digital Officer", "_mentions": 1},
    {"name": "Jennifer Cunningham", "title": "EVP, Chief Human Resources Officer", "_mentions": 2},
    {"name": "Zoe Dickson", "title": "EVP and Chief Human Resources Officer", "_mentions": 1},
], "board": [], "senior_leadership": []}
rs = L._resolve_stale(stale, canon)
names = {p["name"] for p in rs["executives"]}
ok(len(names) == 3, f"stale: 6 -> 3 current office-holders (got {len(names)})")
ok(names == {"Bill Brown", "Bobby George", "Jennifer Cunningham"}, "stale: kept the current ones")

# Safety: nothing is dropped without a better-evidenced rival
solo = {"executives": [{"name": "Ana Silva", "title": "Chief Financial Officer"}],
        "board": [], "senior_leadership": []}
ok(len(L._resolve_stale(solo, canon)["executives"]) == 1, "stale: lone role-holder kept")

equal = {"executives": [{"name": "Ana Silva", "title": "Chief Executive Officer", "_mentions": 2},
                        {"name": "Tom Reed", "title": "Chief Executive Officer", "_mentions": 2}],
         "board": [], "senior_leadership": []}
ok(len(L._resolve_stale(equal, "")["executives"]) == 2,
   "stale: no canonical evidence -> no-op")
ok(len(L._resolve_stale(equal, "nobody here")["executives"]) == 2,
   "stale: indistinguishable rivals both kept (co-CEOs)")

many = {"board": [{"name": f"Dir {i}", "title": "Director"} for i in range(9)],
        "executives": [], "senior_leadership": []}
ok(len(L._resolve_stale(many, canon)["board"]) == 9, "stale: 9 directors untouched")

gp = {"executives": [{"name": "Wendy Bauer", "title": "Group President, Transportation"},
                     {"name": "Karina Chavez", "title": "Group President, Consumer"}],
      "board": [], "senior_leadership": []}
ok(len(L._resolve_stale(gp, canon)["executives"]) == 2, "stale: multiple group presidents kept")

# archival URL gate
ok(L._is_archival("https://x.com/news/2019/new-cio-named"), "archival: news/date path")
ok(L._is_archival("https://x.com/press-releases/ceo-named"), "archival: press release")
ok(not L._is_archival("https://x.com/governance/board-of-directors"), "archival: governance kept")
ok(not L._is_archival("https://x.com/investors/governance"), "archival: IR governance kept")

# bookkeeping never leaks downstream
L._strip_internal_fields(rs)
ok(all("_mentions" not in p for p in rs["executives"]), "stale: _mentions stripped from output")

# ── 7e. corroboration pass gating (no network) ──────────────────────────────
import os as _os
_prev = _os.environ.get("ORGANOGRAM_CORROBORATE")
_os.environ["ORGANOGRAM_CORROBORATE"] = "0"
ok(L._corroborate_canonical("Acme", "[Page: x]\nJane Okonjo CEO") == [],
   "corroborate: disabled by ORGANOGRAM_CORROBORATE=0")
_os.environ["ORGANOGRAM_CORROBORATE"] = "1"
ok(L._corroborate_canonical("Acme", "") == [], "corroborate: no canonical text -> no-op")
_prev_key = _os.environ.pop("ANTHROPIC_API_KEY", None)
ok(L._corroborate_canonical("Acme", "[Page: x]\nJane Okonjo CEO") == [],
   "corroborate: no Anthropic key -> no-op")
if _prev_key is not None:
    _os.environ["ANTHROPIC_API_KEY"] = _prev_key
if _prev is None:
    _os.environ.pop("ORGANOGRAM_CORROBORATE", None)
else:
    _os.environ["ORGANOGRAM_CORROBORATE"] = _prev

# ── 7f. hallucination check: proximity, not scattered tokens ────────────────
# A real 3M board page mentions "Neil G. Mitchill", "Jennifer W. Rumsey" and
# "James R. Fitterling"; models invented colleagues by keeping the given name
# and initial and swapping the surname. Every token of the fake existed
# somewhere on the page, so token-anywhere matching passed them.
board_src = ("neil g. mitchill, jr. director | jennifer w. rumsey director | "
             "james r. fitterling director | bluhm capital partners is a "
             "shareholder | johnson controls supplies us | popham ltd").lower()
for real in ["Neil G. Mitchill", "Jennifer W. Rumsey", "James R. Fitterling"]:
    ok(L._name_in_source(real, board_src), f"hallu: real director kept ({real})")
for fake in ["Neil G. Bluhm", "Jennifer W. Johnson", "James R. Popham"]:
    ok(not L._name_in_source(fake, board_src), f"hallu: blended name rejected ({fake})")
ok(L._name_in_source("Mitchill, Neil", board_src), "hallu: surname-first form still matches")
ok(L._name_in_source("Charles Scharf", "... scharf, charles w. is ceo ..."),
   "hallu: reversed with middle initial still matches")

# ── 8. harvester: JS shell WITH embedded data is kept, empty shell dropped ───
class _Resp:
    def __init__(self, text): self.status_code, self.text = 200, text
shell_with_data = ('<div id="root"></div><script id="__NEXT_DATA__" type="application/json">'
                   + json.dumps({"board": [{"name": "Ana Silva", "jobTitle": "Chair"}]})
                   + "</script>")
empty_shell = '<html><body><div id="root"></div></body></html>'
pages = {"https://a.com/led": shell_with_data, "https://a.com/empty": empty_shell}
L.httpx = types.SimpleNamespace()  # not used; patch the import site instead
import httpx as _real_httpx
_orig_get = _real_httpx.get
_real_httpx.get = lambda url, **kw: _Resp(pages.get(url, ""))
try:
    h = L._Harvester()
    got_data = h.fetch("https://a.com/led")
    got_empty = h.fetch("https://a.com/empty")
    ok(got_data, "harvester: JS shell with __NEXT_DATA__ is KEPT (was dropped)")
    ok("Ana Silva" in h.text(), "harvester: extracted name from JS shell")
    ok(not got_empty, "harvester: truly empty shell still dropped")
    ok(not h.fetch("https://a.com/led"), "harvester: duplicate URL skipped")
    h.chars = L._MAX_HARVEST_CHARS
    ok(h.exhausted, "harvester: budget enforced")
finally:
    _real_httpx.get = _orig_get

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
