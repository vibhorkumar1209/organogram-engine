"""
Leadership search beyond companies: government, public sector, schools, NGOs.

Reported case: HEC Paris in Doha. Its leadership team is listed on
qatar.exed.hec.edu/about-us, and the search found nobody. On production the
crawl harvested 0 pages in 210s — every guessed corporate path 404'd, and
"/about-us" was never tried. Searching hec.edu instead crawled course pages
(/purposeful-leadership, faculty departments) and returned a professor's
endowed chair as an "executive".

Run: python3 backend/test_public_sector_search.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.CRITICAL)

import llm_fallback as F
import structural_engine as S

fails: list[str] = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


# ── The homepage's own navigation says where the leadership is ──────────────
HEC_HOME = """
<nav>
 <a href="/about-us">About Us</a>
 <a href="/Programs-and-Degrees/Executive-MBA">Executive MBA</a>
 <a href="/Programs-and-Degrees/Emerging-Leader-Track-EMBA">Emerging Leaders Track</a>
 <a href="/Programs-and-Degrees/Executive-Certificate-Healthcare-Management">Healthcare Management</a>
 <a href="/faculty">Faculty</a>
 <a href="/admissions">Admissions</a>
 <a href="/news">News</a>
 <a href="/contact">Contact</a>
 <a href="https://www.facebook.com/hecparis">Facebook</a>
</nav>"""
links = F._nav_leadership_links(HEC_HOME, "https://qatar.exed.hec.edu/")
ok(links == [("https://qatar.exed.hec.edu/about-us", False)],
   f"nav: HEC Doha's About page is found; courses, faculty and news are not (got {links})")

MINISTRY_HOME = """
<a href="/en/the-minister">The Minister</a>
<a href="/en/about/organisational-structure">Organisational Structure</a>
<a href="/en/about">About the Ministry</a>
<a href="/en/e-services">E-Services</a>
<a href="/en/media-centre/news">News</a>
<a href="/en/careers">Careers</a>"""
links = F._nav_leadership_links(MINISTRY_HOME, "https://www.mof.gov.qa/en")
urls = [u for u, _ in links]
ok(links[0][1] and "the-minister" in links[0][0] and "organisational-structure" in urls[1],
   f"nav: a ministry's leadership links come first, marked strong (got {links})")
ok(not any("news" in u or "careers" in u or "e-services" in u for u in urls),
   "nav: news, careers and e-services are not followed")

UNI_HOME = """
<a href="/about/leadership">University Leadership</a>
<a href="/about/board-of-trustees">Board of Trustees</a>
<a href="/office-of-the-president">Office of the President</a>
<a href="/academics/programs">Programs</a>
<a href="/students">Students</a>"""
links = F._nav_leadership_links(UNI_HOME, "https://www.example.edu/")
ok(len(links) == 3 and all(strong for _, strong in links),
   f"nav: university leadership, trustees and president's office are strong links (got {links})")

ok(F._nav_leadership_links('<a href="https://other.org/about">About</a>',
                           "https://example.org/") == [],
   "nav: links to other sites are ignored")

# ── Catalogue pages are not leadership pages ────────────────────────────────
for url in ["https://qatar.exed.hec.edu/Programs-and-Degrees/Executive-MBA",
            "https://www.hec.edu/en/faculty-research/departments/accounting-management-control",
            "https://www.example.edu/admissions/leadership-scholarship"]:
    ok(F._is_offtopic_url(url), f"offtopic: {url.split('/', 3)[-1]}")
for url in ["https://www.abbott.com/about-abbott/leadership.html",
            "https://investors.3m.com/governance/board-of-directors",
            "https://www.pharma.com/research/leadership"]:
    ok(not F._is_offtopic_url(url), f"on-topic kept: {url.split('/', 3)[-1]}")

# ── About pages themselves, from a sitemap ──────────────────────────────────
ok(bool(F._ABOUT_PAGE_RE.search("https://qatar.exed.hec.edu/about-us")), "sitemap: /about-us kept")
ok(not F._ABOUT_PAGE_RE.search("https://x.com/about/our-history"), "sitemap: /about/our-history not kept")
ok("/about-us" in F._LEADERSHIP_FETCH_PATHS and "/board-of-trustees" in F._LEADERSHIP_FETCH_PATHS
   and "/the-minister" in F._LEADERSHIP_FETCH_PATHS, "paths: about, trustees and minister pages guessed")
ok(F._LEADERSHIP_FETCH_PATHS.index("/about-us") > F._LEADERSHIP_FETCH_PATHS.index("/leadership"),
   "paths: corporate leadership paths still tried before the About page")

# ── Guessing stops instead of spending the whole deadline ───────────────────
calls = {"n": 0}
_orig_get = F._get_bounded


def _all_404(url, timeout=6):
    calls["n"] += 1
    return None


F._get_bounded = _all_404
F._sitemap_leadership_urls = lambda domain, limit=40: []
h = F._Harvester()
F._fetch_static_leadership("nothing-here.example", harvester=h)
ok(calls["n"] <= F._MAX_GUESS_MISSES * 2 + 40,
   f"guessing: a site that 404s everything stops after ~{F._MAX_GUESS_MISSES} misses "
   f"(made {calls['n']} requests)")


# A page the user pointed at is fetched first and its links followed.
def _fake_get(url, timeout=6):
    pages = {
        "https://hec.edu/en/qatar": '<p>HEC Paris in Qatar</p><a href="/en/qatar/leadership-team">Leadership Team</a>' + "x" * 400,
        "https://hec.edu/en/qatar/leadership-team": "<h2>Leadership Team</h2><p>Jane Roe, Dean</p>" + " " * 10 + "y" * 400,
    }
    return pages.get(url.rstrip("/"))


F._get_bounded = _fake_get
h = F._Harvester()
F._discover_from_navigation(h, "hec.edu", seed_url="https://hec.edu/en/qatar")
ok(h.fetched[:2] == ["https://hec.edu/en/qatar", "https://hec.edu/en/qatar/leadership-team"],
   f"seed: the user's page is read first, then its leadership link (got {h.fetched})")
F._get_bounded = _orig_get

ok(F._subdomain_hosts(["https://qatar.exed.hec.edu/about-us", "https://www.hec.edu/en",
                       "https://hec.edu/x", "https://other.org/"], "hec.edu")
   == ["qatar.exed.hec.edu"], "subdomain: a search hit on qatar.exed.hec.edu is explored")

# ── Names printed in capitals ───────────────────────────────────────────────
ok(F._tidy_name("DJELLOUL BEKKA") == "Djelloul Bekka", "names: 'DJELLOUL BEKKA' tidied")
ok(F._tidy_name("Joshua KOBB") == "Joshua Kobb", "names: 'Joshua KOBB' tidied")
ok(F._tidy_name("Pablo Martin de Holan") == "Pablo Martin de Holan", "names: mixed case untouched")
ok(F._tidy_name("Darren W. McDew") == "Darren W. McDew", "names: McDew untouched")

# ── Prompts and queries know non-corporate leadership ───────────────────────
for term in ("Minister", "Director-General", "Dean", "Board of Trustees", "Leadership Team",
             "Rector", "Secretary-General"):
    ok(term in F._SYSTEM_FROM_WEB, f"prompt: knows '{term}'")
ok("endowed" not in F._SYSTEM_FROM_WEB.lower() and "professorships" in F._SYSTEM_FROM_WEB,
   "prompt: academic chairs and professorships are excluded")
ok("ADVISORY" in F._SYSTEM_FROM_WEB, "prompt: advisory boards are not the governing board")

# ── The engine treats heads of public bodies and schools as the apex ────────
for title in ["Dean", "Dean, HEC Paris in Qatar", "Minister", "Minister of Finance",
              "Director General", "Secretary General", "Rector", "Vice-Chancellor",
              "President and Vice-Chancellor", "Permanent Secretary",
              "Co-founder and CEO", "Co-founder, President and Chief Executive Officer"]:
    ok(S._is_ceo(title), f"apex: {title}")
for title in ["Dean of Students", "Deputy Minister", "Deputy Director General",
              "Under-Secretary", "Pro-Vice-Chancellor (Research)", "Vice-Rector",
              "President, Medical Devices", "Executive Vice President"]:
    ok(not S._is_ceo(title), f"not apex: {title}")
ok(S._exec_department("Minister of Finance") == "",
   "dept: the Minister heading a ministry runs no 'Finance & Accounting' department")
ok(S._exec_department("Chief Operating and Administrative Officer") == "Operations",
   "dept: 'Chief Operating and Administrative Officer' runs Operations")

# ── The full URL a user types is kept ───────────────────────────────────────
import api_server as A
os.environ["PARALLEL_API_KEY"] = ""
ok(A._clean_domain("hec.edu/en/qatar") == "hec.edu"
   and A._site_url("hec.edu/en/qatar") == "https://hec.edu/en/qatar",
   "input: 'hec.edu/en/qatar' keeps its path as the page to start from")
ok(A._site_url("qatar.exed.hec.edu") == "" and A._site_url("https://www.acme.com/") == "",
   "input: a bare domain has no start page")

print("\n%d failed" % len(fails) if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
