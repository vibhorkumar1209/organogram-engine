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

# ── 7g. board titles describe an OUTSIDE career (real 3M directors) ─────────
real_board = {"board": [
    {"name": "Audrey Choi", "title": "Retired Chief Sustainability Officer and Chief Marketing Officer, Morgan Stanley"},
    {"name": "Thomas K. Brown", "title": "Retired Group Vice President, Global Purchasing, Ford"},
    {"name": "James R. Fitterling", "title": "Chairman and Chief Executive Officer, Dow"},
    {"name": "Jennifer W. Rumsey", "title": "Chair, President and Chief Executive Officer, Cummins"},
    {"name": "Pedro J. Pizarro", "title": "President and Chief Executive Officer, Edison International"},
    {"name": "Neil G. Mitchill, Jr.", "title": "Chief Financial Officer, RTX"},
    {"name": "Anne H. Chow", "title": "Retired Chief Executive Officer, AT&T Business"},
], "executives": [], "senior_leadership": []}
before = len(real_board["board"])
after = len(L._resolve_stale(real_board, "audrey choi thomas k. brown james r. fitterling")["board"])
ok(after == before,
   f"board: {before} directors who each ran another company all kept (got {after})")

# executives are still resolved
ex = {"board": [], "senior_leadership": [], "executives": [
    {"name": "Bill Brown", "title": "Chairman and CEO", "_mentions": 3},
    {"name": "Michael Roman", "title": "Executive Chairman of the Board", "_mentions": 1}]}
ok(len(L._resolve_stale(ex, "bill brown chairman and chief executive officer")["executives"]) == 1,
   "exec: former Executive Chairman still dropped")

# ── 7h. truncated names (a live run produced "John P." as an executive) ─────
for bad in ["John P.", "A. B.", "Madonna", "Jane"]:
    ok(not L._is_full_name(bad), f"name: truncated/partial rejected ({bad})")
for good in ["Bill Brown", "Neil G. Mitchill, Jr.", "Li Wei", "J. Smith", "C. Scharf"]:
    ok(L._is_full_name(good), f"name: real name kept ({good})")
ok([p["name"] for p in L._clean_list([{"name": "John P.", "title": "EVP"},
                                      {"name": "Bill Brown", "title": "CEO"}])] == ["Bill Brown"],
   "name: _clean_list drops the truncated entry")

# ── 7i. divisional seats are not the global seat (real Maersk titles) ───────
for scoped in ["CEO, Maersk Ocean", "CEO of Maersk Ocean", "CFO, Terminals",
               "CEO of Logistics & Services",
               "President and Chief Executive Officer, Cummins",
               "Retired Chief Marketing Officer, Morgan Stanley"]:
    ok(L._role_keys(scoped) == set(), f"unit: divisional/outside seat not singular ({scoped})")
for glob, exp in [("Chairman and CEO", {"ceo", "chairman"}),
                  ("Executive Chairman of the 3M Board of Directors", {"chairman"}),
                  ("Chairman of the Board", {"chairman"}),
                  ("Chief Executive Officer of the Company", {"ceo"}),
                  ("Chief Financial Officer", {"cfo"})]:
    ok(L._role_keys(glob) == exp, f"unit: global seat still singular ({glob})")

# The live Maersk conflict set must no longer collide
maersk = {"board": [], "senior_leadership": [], "executives": [
    {"name": "Vincent Clerc", "title": "Chief Executive Officer", "_mentions": 3},
    {"name": "Keith Svendsen", "title": "CEO, Maersk Ocean", "_mentions": 1},
    {"name": "Tina Revsbech", "title": "CEO of Logistics & Services", "_mentions": 1},
    {"name": "Robert Erni", "title": "Chief Financial Officer", "_mentions": 2},
    {"name": "Jakob Sjostrom", "title": "CFO, Terminals", "_mentions": 1}]}
kept = L._resolve_stale(maersk, "vincent clerc chief executive officer robert erni")["executives"]
ok(len(kept) == 5, f"unit: all 5 Maersk executives kept (got {len(kept)})")

# ── 7j. the Abbott failure: 4 executives found where 33 exist ───────────────
# Abbott read 12 pages, ten of them /corpnewsroom/ articles that consumed the
# whole harvest budget, so 34 queued bio pages were never fetched.
for junk in ["https://www.abbott.com/en-us/corpnewsroom/tag/leadership",
             "https://www.abbott.com/en-us/corpnewsroom/strategy-and-strength/x",
             "https://x.com/press-room/2024/ceo", "https://x.com/media-center/story",
             "https://x.com/en/newsroom/article"]:
    ok(L._is_archival(junk), f"abbott: newsroom/tag page excluded ({junk.split('/')[-2]})")
for keep in ["https://www.abbott.com/en-us/about-abbott/leadership",
             "https://www.abbott.com/en-us/about-abbott/leadership/executive-team/robert-ford",
             "https://www.abbott.com/investors/governance"]:
    ok(not L._is_archival(keep), "abbott: real leadership URL kept")

# Abbott 404s /about-abbott/leadership but serves /about-abbott/leadership.html,
# and canonicalises under /en-us/. Neither spelling was ever tried.
variants = L._path_variants("/leadership")
ok("/leadership.html" in variants, "abbott: .html spelling tried")
ok("/en-us/leadership" in variants, "abbott: locale prefix tried")
ok("/en-us/leadership.html" in variants, "abbott: locale + .html tried")

# Guessing every spelling for every path is hundreds of 404s, so the site's
# convention is learned from the first hit.
_h = L._Harvester()
ok(len(_h.path_variants("/leadership")) > 4, "abbott: all spellings tried before one works")
_h.learn_convention("/en-us/leadership.html", "/leadership")
ok(_h.path_variants("/governance") == ["/en-us/governance.html"],
   "abbott: convention learned, later paths tried once")

# Names live in component attributes: <abbott-card eyebrow="Chairman and CEO"
# heading="Robert B. Ford">. A single regex pass over the page returned
# non-overlapping matches, so it saw eyebrow= and never heading= on that tag.
card = ('<abbott-card color="medium" eyebrow="Chairman and Chief Executive Officer" '
        'cardstyle="media" heading="Robert B. Ford " ctaType="arrow"></abbott-card>')
attrs = L._extract_attr_names(card)
ok("Robert B. Ford" in attrs, "abbott: name read from a custom element attribute")
ok("Chief Executive Officer" in attrs, "abbott: role attribute read from the same tag")

# Committee tables print directors as initials while the roster prints them in
# full, which listed each director twice.
merged = L._merge_leadership([
    {"board": [{"name": "Nita Ahuja", "title": "Board of Directors"}]},
    {"board": [{"name": "N. Ahuja", "title": "Board Member"}]},
    {"board": [{"name": "Michelle A. Kumbier", "title": "Board of Directors"}]},
    {"board": [{"name": "M.A. Kumbier", "title": "Board Member"}]},
])
ok(len(merged["board"]) == 2,
   f"abbott: initial-form directors merged (4 entries -> {len(merged['board'])})")
ok(L._name_aliases("N. Ahuja") == {"n ahuja"},
   "abbott: a leading initial is kept in the alias key")
two = L._merge_leadership([{"board": [{"name": "John Smith", "title": "Director"}]},
                           {"board": [{"name": "Jane Smith", "title": "Director"}]}])
ok(len(two["board"]) == 2, "abbott: different people sharing a surname stay separate")

# The reserve must not strand budget when the sitemap yields bios directly.
_h2 = L._Harvester()
_h2.chars = _h2.index_budget_chars + 1
ok(not _h2.exhausted, "abbott: no bios queued -> full budget usable")
_h2.bio_queue.append("https://x.com/leadership/a")
ok(_h2.exhausted, "abbott: bios queued -> index reserve enforced")

# ── 8. harvester: JS shell WITH embedded data is kept, empty shell dropped ───
shell_with_data = ('<div id="root"></div><script id="__NEXT_DATA__" type="application/json">'
                   + json.dumps({"board": [{"name": "Ana Silva", "jobTitle": "Chair"}]})
                   + "</script>")
empty_shell = '<html><body><div id="root"></div></body></html>'
pages = {"https://a.com/led": shell_with_data, "https://a.com/empty": empty_shell}
_orig_get_bounded = L._get_bounded
L._get_bounded = lambda url, timeout=6: pages.get(url)
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
    L._get_bounded = _orig_get_bounded

# ── 9. page-size cap keeps one huge page from blowing up memory ─────────────
ok(L._MAX_PAGE_BYTES > 0, "page cap: configured")
_calls = {}
def _fake_stream(method, url, **kw):
    class _R:
        status_code = 200
        headers = {"content-type": "text/html", "content-length": str(kw.pop("_len", 0))}
        encoding = "utf-8"
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_bytes(self):
            # 4 MB body, streamed in 256 KB chunks
            for _ in range(16):
                yield b"x" * (256 * 1024)
    _calls["n"] = _calls.get("n", 0) + 1
    return _R()
import httpx as _hx
_orig_stream = _hx.stream
_hx.stream = _fake_stream
try:
    ok(L._get_bounded("https://big.example/page") is None,
       "page cap: 4 MB body refused before it is decoded")
finally:
    _hx.stream = _orig_stream

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
