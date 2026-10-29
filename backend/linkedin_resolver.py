"""
LinkedIn profiles for Board of Directors and Executive Management members.

Every candidate profile comes from a real search result (Parallel Search), and
the decision to accept it is made HERE, by checking the result's own name,
headline and excerpt against the person — never by trusting a model's claim
that a URL belongs to someone. A search for "Robert Ford Abbott LinkedIn"
returns eight Robert Fords (and a Robert Forde who works at Abbott), none of
them Abbott's CEO; the previous resolver accepted any profile whose slug
contained the surname, so a common name was assigned a stranger's profile.

Rules, in the order they are applied:
  1. The profile's name must be the person's name. Middle names and initials
     may be missing on either side ("Robert B. Ford" = "Robert Ford"), and a
     given name may be a short form ("Rob", "Bill") — but the surname must
     match exactly, so "Forde" is not "Ford".
  2. The company must appear on the profile (current or past), and the
     headline must be a leadership title. A headline that is plainly junior
     ("Sales Representative at Abbott") rejects the profile outright.
  3. When not confident, the answer is "Not Found" — never a best guess.

Confidence:
  High       name + the company on the profile + a leadership title
  Medium     name + company, title not shown; or the headline restates the
             exact expected role but the company isn't visible
  Low        the only senior profile with this name in the results, company
             not visible; or two different profiles fit equally well
  Not Found  anything less — never a best guess

Search is two-pass: company-led first, and only a weak or empty answer pays
for a second, role-led search (directors' profiles often never name the
company), judged together with the first.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

_MAX_PEOPLE = int(os.environ.get("ORGANOGRAM_LINKEDIN_MAX_PEOPLE", "80"))
_DEADLINE_S = int(os.environ.get("ORGANOGRAM_LINKEDIN_DEADLINE_S", "240"))
_WORKERS = int(os.environ.get("ORGANOGRAM_LINKEDIN_WORKERS", "8"))

NOT_FOUND = "Not Found"
_LEVELS = {"High": 3, "Medium": 2, "Low": 1}

# ── Names ─────────────────────────────────────────────────────────────────────

_HONORIFIC_RE = re.compile(
    r"^(?:dr|mr|mrs|ms|miss|prof|professor|sir|dame|lord|lady|rev|hon)\.?\s+", re.I)
_SUFFIX_WORDS = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "cpa", "cfa",
                 "esq", "obe", "cbe", "mbe", "pe", "rn", "facc", "faha"}
_NICKNAME_RE = re.compile(r'["“”\'(]([A-Za-z][\w\'-]*)["“”\')]')

# Short forms that are not simple prefixes of the given name ("Bill" is not a
# prefix of "William"); prefixes such as Rob/Chris/Matt are handled generally.
_NICKNAMES: dict[str, set[str]] = {
    "robert": {"bob", "bobby", "robbie", "bert"}, "william": {"bill", "billy", "will", "liam"},
    "richard": {"rick", "dick", "rich", "ricky"}, "james": {"jim", "jimmy", "jamie"},
    "michael": {"mike", "mick", "mikey"}, "elizabeth": {"liz", "beth", "betsy", "lisa", "libby"},
    "katherine": {"kate", "kathy", "katie", "kat"}, "catherine": {"cathy", "kate", "katie", "cat"},
    "margaret": {"maggie", "meg", "peggy", "marge"}, "edward": {"ed", "ted", "eddie", "ned"},
    "charles": {"chuck", "charlie", "chas"}, "john": {"jack", "johnny"},
    "anthony": {"tony"}, "andrew": {"andy", "drew"}, "stephen": {"steve"},
    "steven": {"steve"}, "susan": {"sue", "suzy"}, "deborah": {"deb", "debbie"},
    "rebecca": {"becky", "becca"}, "debora": {"deb", "debbie", "debby"},
    "deborah": {"deb", "debbie", "debby"}, "dorothy": {"dot", "dottie"}, "lawrence": {"larry"}, "gerald": {"jerry"},
    "harold": {"hal", "harry"}, "henry": {"hank", "harry"}, "theodore": {"ted", "theo"},
    "patricia": {"pat", "trish", "patty"}, "jennifer": {"jen", "jenny"},
    "joseph": {"joe", "joey"}, "thomas": {"tom", "tommy"}, "daniel": {"dan", "danny"},
    "david": {"dave"}, "kenneth": {"ken", "kenny"}, "frederick": {"fred"},
    "alexander": {"alex", "sasha"}, "alexandra": {"alex", "sasha"}, "nicholas": {"nick"},
    "francis": {"frank"}, "francisco": {"paco", "frank"}, "jose": {"pepe"},
}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()


def _name_parts(name: str) -> list[tuple[str, str]]:
    """(given, surname) pairs a name can be written under; middle names dropped."""
    raw = str(name or "").strip()
    # "Jane Doe, PhD, CPA" — credentials after a comma are not part of the name.
    raw = raw.split(",")[0]
    nickname = ""
    m = _NICKNAME_RE.search(raw)
    if m:
        nickname = m.group(1).lower()
        raw = _NICKNAME_RE.sub(" ", raw)
    raw = _HONORIFIC_RE.sub("", _fold(raw).strip())
    tokens = [t for t in re.sub(r"[^a-z ]", " ", raw.lower()).split()
              if t not in _SUFFIX_WORDS]
    # Middle initials go; a leading initial stays ("J. Smith").
    tokens = [t for i, t in enumerate(tokens) if len(t) > 1 or i == 0]
    if len(tokens) < 2:
        return []
    pairs = [(tokens[0], tokens[-1])]
    if nickname:
        pairs.append((re.sub(r"[^a-z]", "", nickname), tokens[-1]))
    return pairs


def _given_compatible(a: str, b: str) -> str:
    """'exact', 'nick' (a known short form), 'initial', or '' when they differ."""
    if a == b:
        return "exact"
    short, long_ = sorted((a, b), key=len)
    if len(short) == 1 and long_.startswith(short):
        return "initial"
    if len(short) >= 3 and long_.startswith(short):
        return "nick"
    if a in _NICKNAMES.get(b, set()) or b in _NICKNAMES.get(a, set()):
        return "nick"
    return ""


_MATCH_ORDER = {"": 0, "initial": 1, "nick": 2, "exact": 3}


def name_match(person: str, candidate: str) -> str:
    """How a profile's name matches the person: 'exact', 'nick', 'initial' or ''."""
    best = ""
    for given, surname in _name_parts(person):
        for cgiven, csurname in _name_parts(candidate):
            if surname != csurname:
                continue
            got = _given_compatible(given, cgiven)
            if _MATCH_ORDER[got] > _MATCH_ORDER[best]:
                best = got
    return best


# ── Company and role ──────────────────────────────────────────────────────────

_LEGAL_WORDS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "llc", "lp", "sa", "ag", "nv", "se", "spa", "bv", "gmbh", "kg", "oyj", "asa",
    "group", "holdings", "holding", "the", "and", "laboratories", "labs", "international",
}


def _padded(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", _fold(text).lower()).strip() + " "


def company_phrases(company: str, domain: str = "") -> list[str]:
    """Padded phrases that identify the company in a profile's text."""
    words = [w for w in _padded(company).split() if w not in _LEGAL_WORDS]
    phrases = []
    if words:
        phrases.append(" " + " ".join(words) + " ")
        # "Abbott Laboratories" → "abbott"; a single distinctive first word is
        # how most people write their employer.
        if len(words) > 1 and len(words[0]) >= 4:
            phrases.append(f" {words[0]} ")
    stem = re.sub(r"^www\.", "", (domain or "").lower()).split(".")[0]
    stem = re.sub(r"[^a-z0-9]", "", stem)
    if len(stem) >= 3 and f" {stem} " not in phrases:
        phrases.append(f" {stem} ")
    return phrases


_EXEC_ROLE_RE = re.compile(
    r"\b(chief|ceo|cfo|coo|cto|cio|cmo|chro|cso|cdo|cco|clo|cpo|cao|cro|president|"
    r"evp|svp|executive vice president|senior vice president|general counsel|"
    r"corporate secretary|treasurer|chair|chairman|chairwoman|chairperson|officer|"
    r"head of|managing director|executive director|board)\b"
    # "Health Care Industry Executive" — but not an executive assistant or an
    # account executive.
    r"|(?<!account )\bexecutive\b(?!\s+(?:assistant|recruiter|search|admin))", re.I)
# Board members' headlines describe their own careers, which are usually
# senior roles elsewhere.
_BOARD_ROLE_RE = re.compile(
    # "Director" alone is a board title; "Director of Sales" or "Sales
    # Director" is a functional one and says nothing about a board seat.
    r"(?<!sales )(?<!marketing )(?<!account )(?<!program )(?<!project )(?<!product )"
    r"(?<!creative )(?<!art )(?<!regional )(?<!area )(?<!store )(?<!clinical )(?<!technical )"
    r"(?<!it )(?<!hr )(?<!finance )(?<!operations )(?<!engineering )(?<!associate )"
    r"\bdirector\b(?!\s+of\b)|"
    r"\b(board|chair|chairman|chairwoman|trustee|ceo|chief|president|founder|"
    r"partner|retired|former|investor|governor|ambassador|professor|dean|executive|"
    r"officer|general|admiral|senator|advisor|adviser)\b", re.I)
_JUNIOR_RE = re.compile(
    r"\b(intern|student|analyst|associate|specialist|representative|rep|coordinator|"
    r"assistant|technician|engineer|nurse|supervisor|sales|account manager|"
    r"recruiter|clerk|operator|cashier)\b", re.I)
_CONNECTIONS_RE = re.compile(r"\b(\d{1,3})\s+connections\b", re.I)


# Supporting a leader is not being one: "Executive Assistant to the CEO"
# names the CEO and is still junior.
_SUPPORT_RE = re.compile(
    r"\b(executive assistant|administrative assistant|personal assistant|assistant to|"
    r"intern|internship)\b", re.I)


def role_signals(text: str, kind: str) -> tuple[bool, bool]:
    """(looks like a leadership title, looks plainly junior)."""
    if _SUPPORT_RE.search(text):
        return False, True
    role_re = _BOARD_ROLE_RE if kind == "board" else _EXEC_ROLE_RE
    ok = bool(role_re.search(text))
    junior = bool(_JUNIOR_RE.search(text)) and not ok
    return ok, junior


# ── Search results ────────────────────────────────────────────────────────────

_PROFILE_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/in/([A-Za-z0-9_%\-]+)/?", re.I)
_TITLE_TAIL_RE = re.compile(r"\s*\|\s*(?:linkedin|professional profile).*$", re.I)


def normalise_profile_url(url: str) -> str:
    m = _PROFILE_RE.match(str(url or "").strip())
    return f"https://www.linkedin.com/in/{m.group(1)}" if m else ""


def parse_result_title(title: str) -> tuple[str, str]:
    """'Jane Doe - CFO - Acme | LinkedIn' → ('Jane Doe', 'CFO - Acme')."""
    body = _TITLE_TAIL_RE.sub("", str(title or "")).strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", body) if p.strip()]
    # "Melissa Lora - LinkedIn": the site name, not a headline.
    parts = [p for p in parts if p.lower() not in ("linkedin", "--", "-")] or parts[:1]
    if not parts:
        return "", ""
    return parts[0], " - ".join(parts[1:])


# Page furniture after which an excerpt describes OTHER people: sidebars,
# other members' posts, "others named X". A company named there is not
# evidence about this profile — "Ron Jadin - Grainger" carries a sidebar that
# says "Colette Kress · NVIDIA".
_NOISE_MARKERS = ("other similar profiles", "people also viewed", "liked by",
                  "others named", "explore more posts", "more activity by",
                  "](http", "* [", "sign in to view")
_STRUCT_RE = re.compile(r"(?:^|[*\-#]\s*)(title|company name|seniority|headline)\s*:\s*([^*#\n]{2,120})",
                        re.I | re.M)
_FOLLOWERS_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*([km])?\s+followers\b|follower count:\s*(\d+)", re.I)
_CONN_COUNT_RE = re.compile(r"connection count:\s*(\d+)", re.I)
# "[Colette Kress](https://www.linkedin.com/in/colette-kress-22328346) ###
# Colette Kress NVIDIA 8K followers" — LinkedIn's own sidebar, naming the
# person, their profile and their current employer.
_BROWSE_RE = re.compile(
    r"\[([^\]\n]{3,60})\]\((https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com/in/[^)\s?]+)[^)]*\)"
    r"(.{0,220})", re.S)


def _own_text(excerpt: str) -> str:
    low = excerpt.lower()
    cut = min([i for i in (low.find(m) for m in _NOISE_MARKERS) if i >= 0] or [len(excerpt)])
    return excerpt[:cut]


def _followers(text: str) -> int:
    m = _FOLLOWERS_RE.search(text)
    if not m:
        return 0
    if m.group(3):
        return int(m.group(3))
    n = float(m.group(1))
    return int(n * {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1))


def _connections(text: str) -> int | None:
    m = _CONN_COUNT_RE.search(text) or _CONNECTIONS_RE.search(text)
    return int(m.group(1)) if m else None


def _profile(result: dict) -> dict | None:
    """The fields of one search result that describe the profile itself."""
    url = normalise_profile_url(result.get("url", ""))
    if not url:
        return None
    name, headline = parse_result_title(result.get("title", ""))
    excerpt = " ".join(result.get("excerpts") or [])
    own = _own_text(excerpt)
    fields = {k.lower(): v.strip() for k, v in _STRUCT_RE.findall(own)}
    # A headline that is only a place ("Denver, Colorado, United States")
    # says nothing about the job.
    if re.fullmatch(r"[A-Za-z .'-]+(?:,\s*[A-Za-z .'-]+){1,3}", headline or "") \
            and not _EXEC_ROLE_RE.search(headline) and not _BOARD_ROLE_RE.search(headline):
        location, headline = headline, ""
    else:
        location = ""
    title = headline or fields.get("headline") or fields.get("title") or ""
    if not title and fields.get("company name"):
        title = fields["company name"]
    exp = re.search(r"Experience:\s*([^·\n]{2,80})", own)
    return {"url": url, "name": name, "headline": title, "own": own,
            "raw_title": str(result.get("title", "")), "location": location,
            "experience": exp.group(1).strip() if exp else "",
            "seniority": fields.get("seniority", ""),
            "connections": _connections(own), "followers": _followers(own)}


def _sidebar_profiles(results: list[dict]) -> list[dict]:
    """Profiles named in other results' sidebars, with the employer shown there."""
    out = []
    for r in results:
        for m in _BROWSE_RE.finditer(" ".join(r.get("excerpts") or [])):
            url = normalise_profile_url(m.group(2))
            if not url:
                continue
            window = re.split(r"\*\s*\[", m.group(3))[0]
            window = re.sub(r"[#*]+", " ", window).replace(m.group(1), " ", 1)
            out.append({"url": url, "name": m.group(1).strip(),
                        "headline": " ".join(window.split())[:100], "own": window,
                        "raw_title": "", "location": "",
                        "experience": "", "seniority": "", "connections": None,
                        "followers": _followers(window), "sidebar": True})
    return out


_TITLE_ABBREV = {
    "evp": "executive vice president", "svp": "senior vice president", "vp": "vice president",
    "ceo": "chief executive officer", "cfo": "chief financial officer",
    "coo": "chief operating officer", "cto": "chief technology officer",
    "cio": "chief information officer", "cmo": "chief marketing officer",
    "chro": "chief human resources officer", "clo": "chief legal officer",
    "gc": "general counsel", "md": "managing director",
}
_TITLE_STOP = {"and", "of", "the", "at", "for", "to", "in", "a", "an", "s"}


def _title_tokens(text: str) -> list[str]:
    out = []
    for tok in _padded(text).split():
        out.extend(_TITLE_ABBREV.get(tok, tok).split())
    return [t for t in out if t not in _TITLE_STOP]


def title_match(expected: str, headline: str) -> bool:
    """The headline restates the expected role, not just a seniority."""
    exp = set(_title_tokens(expected))
    head = _title_tokens(headline)
    if not exp or not head:
        return False
    hits = sum(1 for t in head if t in exp)
    return hits >= 4 and hits / len(head) >= 0.6


def _full_tokens(name: str) -> list[str]:
    raw = _HONORIFIC_RE.sub("", _fold(str(name or "").split(",")[0]).strip())
    return [t for t in re.sub(r"[^a-z ]", " ", raw.lower()).split() if t not in _SUFFIX_WORDS]


def _merge_profiles(profiles: list[dict]) -> list[dict]:
    """One record per profile URL, pooling what every result said about it.

    The same profile turns up in several results, and the damning detail is
    often in only one of them: an impostor "Jensen Huang - Founder and CEO,
    NVIDIA" showed 53 connections and an unrelated employer in one result and
    nothing but the headline in another. Keeping the strongest single reading
    threw that evidence away.
    """
    merged: dict[str, dict] = {}
    for p in profiles:
        m = merged.get(p["url"])
        if m is None:
            merged[p["url"]] = dict(p)
            continue
        own_result = not p.get("sidebar")
        for key in ("name", "headline", "location", "experience", "seniority"):
            if p.get(key) and (not m.get(key) or (own_result and m.get("sidebar"))):
                m[key] = p[key]
        m["own"] = f"{m['own']} {p['own']}"
        m["raw_title"] = f"{m['raw_title']} {p['raw_title']}"
        known = [c for c in (m["connections"], p["connections"]) if c is not None]
        m["connections"] = min(known) if known else None
        m["followers"] = max(m["followers"], p["followers"])
        m["sidebar"] = bool(m.get("sidebar")) and bool(p.get("sidebar"))
    return list(merged.values())


def _features(person: dict, prof: dict, phrases: list[str], hq: str) -> dict | None:
    """What one profile says about one person, before any decision."""
    match = name_match(person["name"], prof["name"])
    if not match:
        return None
    kind = person.get("kind", "executive")
    own_pad = _padded(" ".join([prof["headline"], prof["experience"], prof["own"]]))
    role_ok, junior = role_signals(" ".join([prof["headline"], prof["seniority"]]), kind)
    if not role_ok and not junior:
        # No title in the result's heading — the profile's own text may carry
        # it ("Experience Member Board of Directors NVIDIA").
        role_ok, _ = role_signals(prof["own"][:300], kind)
    full_p, full_c = _full_tokens(person["name"]), _full_tokens(prof["name"])
    exp_pad = _padded(prof["experience"])
    return {
        # An executive whose listed employer is some other company is either a
        # namesake or an impostor claiming the title. Directors are exempt —
        # their employer is usually somewhere else.
        "exp_conflict": (kind != "board" and bool(prof["experience"])
                         and not any(p in exp_pad for p in phrases)),
        "match": match,
        "exact": match == "exact",
        # A known short form (Debbie for Debora) is the same given name.
        "strong": match in ("exact", "nick"),
        # "Timothy S. Teter" written exactly as the company writes it, middle
        # initial included — far rarer than "Timothy Teter".
        "full": match == "exact" and len(full_p) > 2 and full_p == full_c,
        "company": any(p in own_pad for p in phrases),
        "role": role_ok or prof["followers"] >= 5_000,
        "junior": junior,
        "same_role": title_match(person.get("title", ""), prof["headline"]),
        # Thousands of followers is what a public company's officers look
        # like; it stands in for a title the snippet didn't show.
        "notable": prof["followers"] >= 5_000,
        "hq": bool(hq) and hq in _padded(prof["own"] + " " + prof["raw_title"]),
    }


def score_profile(person: dict, prof: dict, f: dict, ctx: dict) -> dict | None:
    """Judge one profile for one person. None = not confidently this person.

    ctx counts the exact-name profiles in the same results: all of them,
    those with a senior title, those that are notable, those at HQ.
    """
    if f["junior"]:
        return None
    exact, strong = f["exact"], f["strong"]
    unique = ctx["namesakes"] == 1 or f["full"]

    if f["company"] and f["role"]:
        level = 3 if strong else 2
    elif f["company"]:
        level = 2 if strong else 1
    elif f["same_role"] and strong:
        # Company not on the snippet, but the headline is this exact role.
        level = 2
    elif exact and f["role"] and (unique or (ctx["senior"] == 1 and ctx["namesakes"] <= 2)
                                  or (f["notable"] and ctx["notable"] == 1)):
        # The only senior profile with this name in the results.
        level = 1
    elif exact and f["hq"] and ctx["hq"] == 1 and not prof["headline"]:
        # No title shown, but the only namesake located at the company's HQ.
        level = 1
    else:
        return None

    # Few connections and no following: impostor and namesake accounts look
    # like this ("Jensen Huang — Founder and CEO, NVIDIA", 53 connections).
    conn = prof["connections"]
    if conn is not None and conn < 300 and prof["followers"] < 1_000:
        level -= 1
    if f["exp_conflict"]:
        level -= 1
    if level < 1:
        return None

    surname = (_name_parts(person["name"]) or [("", "")])[0][1]
    return {
        "linkedin_url": prof["url"],
        "confidence": {3: "High", 2: "Medium", 1: "Low"}[level],
        "matched_name": prof["name"],
        "matched_title": (prof["headline"] or prof["experience"]
                          or (f"No title shown · {prof['location']}" if prof["location"] else "")
                          or _snippet(prof["own"])),
        "source": "linkedin_sidebar" if prof.get("sidebar") else "linkedin_search",
        # A chosen address (/in/jenhsunhuang) over a generated one
        # (/in/jensen-huang-89a60594): officers of public companies nearly
        # always have one, impostor and namesake accounts rarely do. Only
        # breaks ties — it never makes a weak match strong.
        "_rank": (level, exact, not f["exp_conflict"], f["company"] or f["same_role"],
                  f["role"], _is_vanity(prof["url"]), f["notable"],
                  bool(surname) and surname in prof["url"].lower().replace("-", "")),
    }


def _queries(person: dict, company: str, second_pass: bool) -> list[str]:
    full = person["name"]
    pairs = _name_parts(full)
    short = f"{pairs[0][0].title()} {pairs[0][1].title()}" if pairs else full
    title = person.get("title", "")
    if not second_pass:
        return [f"{full} {company} LinkedIn", f"site:linkedin.com/in {short} {company}"]
    # Directors' profiles describe their own careers and often never name
    # this company, so the second pass leads with the role instead.
    if person.get("kind") == "board":
        return [f"{short} board of directors LinkedIn", f"site:linkedin.com/in {full} director"]
    return [f"{short} {title} LinkedIn" if title else f"{short} executive LinkedIn",
            f"site:linkedin.com/in {short} {title or 'executive'}"]


def _search(person: dict, company: str, api_key: str, second_pass: bool = False,
            mode: str = "advanced") -> list[dict]:
    """One Parallel search for this person's profile (see parallel_client for tiers)."""
    import parallel_client
    return parallel_client.search(
        _queries(person, company, second_pass),
        objective=(f"Find the official LinkedIn profile (linkedin.com/in/...) of {person['name']}, "
                   f"{person.get('title') or 'a leader'} at {company}. Only this exact person, "
                   f"who is or was a board member or executive at {company}."),
        mode=mode)


def _is_vanity(url: str) -> bool:
    """True for a chosen profile address, False for LinkedIn's generated one."""
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return not re.search(r"-[0-9a-f]{6,}$|\d{3,}$", slug)


def _hq_phrase(hq: str) -> str:
    city = str(hq or "").split(",")[0].strip()
    return _padded(city) if len(city) >= 3 else ""


def judge(person: dict, results: list[dict], company: str, domain: str = "",
          hq: str = "") -> dict:
    """Pick the verified profile for *person* from raw search results."""
    phrases = company_phrases(company, domain)
    hq_pad = _hq_phrase(hq)
    profiles = _merge_profiles(
        [p for p in (_profile(r) for r in results) if p] + _sidebar_profiles(results))
    feats = [(p, _features(person, p, phrases, hq_pad)) for p in profiles]
    feats = [(p, f) for p, f in feats if f]

    exact_urls = {p["url"] for p, f in feats if f["exact"]}
    ctx = {
        "namesakes": len(exact_urls),
        "senior": len({p["url"] for p, f in feats if f["exact"] and f["role"]}),
        "notable": len({p["url"] for p, f in feats if f["exact"] and f["notable"]}),
        "hq": len({p["url"] for p, f in feats if f["exact"] and f["hq"]}),
    }

    by_url: dict[str, dict] = {}
    for prof, f in feats:
        cand = score_profile(person, prof, f, ctx)
        if not cand:
            continue
        prev = by_url.get(cand["linkedin_url"])
        # The same profile seen twice (its own result + a sidebar): keep the
        # stronger reading.
        if prev is None or cand["_rank"] > prev["_rank"]:
            if prev is not None:
                cand["matched_title"] = cand["matched_title"] or prev["matched_title"]
            by_url[cand["linkedin_url"]] = cand

    # A company website link the leadership search already found is the
    # company vouching for the profile; it stands when the search finds
    # nothing better.
    site_url = normalise_profile_url(person.get("site_linkedin", ""))
    if site_url and site_url not in by_url:
        surname = (_name_parts(person["name"]) or [("", "")])[0][1]
        if surname and surname in site_url.lower().replace("-", ""):
            by_url[site_url] = {
                "linkedin_url": site_url, "confidence": "Medium",
                "matched_name": person["name"], "matched_title": "Linked from company website",
                "source": "company_website", "_rank": (2, True, True, True, False, True, False, True),
            }

    if not by_url:
        return not_found(person)
    ranked = sorted(by_url.values(), key=lambda c: c["_rank"], reverse=True)
    best = dict(ranked[0])
    # Two different profiles that fit equally well: say so rather than pick.
    if len(ranked) > 1 and ranked[1]["_rank"][:7] == best["_rank"][:7]:
        best["confidence"] = "Low"
        best["matched_title"] = (best["matched_title"] + " (another profile fits equally)").strip()
    best["matched_title"] = _clean_title(best["matched_title"])
    return {k: v for k, v in best.items() if not k.startswith("_")}


def _snippet(text: str) -> str:
    """The start of a profile's own text, when it has no headline."""
    t = re.sub(r"[#*]+|\s-\s(?=[a-z ]+:)", " ", str(text or ""))
    t = " ".join(t.split())
    return (t[:90] + "…") if len(t) > 90 else t


def _clean_title(title: str) -> str:
    t = re.sub(r"\s*[-|]\s*LinkedIn\b.*$", "", str(title or ""), flags=re.I)
    t = re.sub(r"\s+\d+(?:\.\d+)?\s*[KkMm]?\s+followers\b.*$", "", t)
    return " ".join(t.split()).strip(" -|")


def resolve_person(person: dict, company: str, domain: str, api_key: str,
                   hq: str = "") -> dict:
    """Best verified profile for one person, or a Not Found record."""
    results: list[dict] = []
    best = not_found(person)
    # Cheapest first, and each step only for what the last couldn't settle:
    #   1. turbo, company-led queries        ($0.001)
    #   2. advanced, the same queries        ($0.005, the lite price)
    #   3. advanced, role-led queries         ($0.005)
    # Every step is judged together with the results before it.
    for second_pass, mode in ((False, "turbo"), (False, "advanced"), (True, "advanced")):
        try:
            results += _search(person, company, api_key, second_pass, mode=mode)
        except Exception as exc:
            logger.warning("LinkedIn search failed for '%s': %s", person.get("name"), exc)
        best = judge(person, results, company, domain, hq)
        if best["confidence"] in ("High", "Medium"):
            break
    return best


def not_found(person: dict) -> dict:
    return {"linkedin_url": "", "confidence": NOT_FOUND, "matched_name": "",
            "matched_title": "", "source": "linkedin_search"}


def resolve_people(people: list[dict], company: str, domain: str = "",
                   hq: str = "") -> dict[str, dict]:
    """
    people: [{"key", "name", "title", "kind": "board"|"executive", "site_linkedin"}]
    Returns {key: result}. People not reached before the deadline are absent,
    so the caller can tell "searched, not found" from "not searched".
    """
    api_key = os.environ.get("PARALLEL_API_KEY", "")
    if not api_key or not people:
        if not api_key:
            logger.info("LinkedIn resolution skipped: PARALLEL_API_KEY not set")
        return {}

    people = people[:_MAX_PEOPLE]
    deadline = time.monotonic() + _DEADLINE_S
    out: dict[str, dict] = {}
    # Worker threads don't inherit context, and the usage tracker lives in a
    # contextvar — run each search inside a copy so its cost is counted.
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(ctx.copy().run, resolve_person, p, company, domain, api_key, hq): p
            for p in people
        }
        for fut in as_completed(futures):
            p = futures[fut]
            if time.monotonic() > deadline:
                logger.warning("LinkedIn resolution deadline hit for '%s'", company)
                for f in futures:
                    f.cancel()
                break
            try:
                out[p["key"]] = fut.result()
            except Exception as exc:
                logger.warning("LinkedIn resolution error for '%s': %s", p.get("name"), exc)

    # One profile cannot belong to two different people. Keep it for the
    # stronger match and mark the other Not Found.
    owner: dict[str, str] = {}
    for key, res in out.items():
        url = res.get("linkedin_url")
        if not url:
            continue
        if url in owner:
            other = owner[url]
            a, b = out[other], res
            if _LEVELS.get(b["confidence"], 0) > _LEVELS.get(a["confidence"], 0):
                out[other] = not_found({})
                owner[url] = key
            else:
                out[key] = not_found({})
        else:
            owner[url] = key

    found = sum(1 for r in out.values() if r.get("linkedin_url"))
    logger.info("LinkedIn resolution for '%s': %d / %d found", company, found, len(people))
    return out
