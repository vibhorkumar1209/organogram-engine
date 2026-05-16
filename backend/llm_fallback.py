"""
LLM company leadership enrichment — Board of Directors and Executive Management only.

Public function:

  llm_fetch_leadership(company_name, domain="")
    Priority 1: Scrape the company's own website (leadership/about/team pages).
    Priority 2: Fall back to Claude's training-data knowledge of the company.

    Returns {"board": [...], "executives": [...]}  each item = {name, title}.

The LLM is NOT used for title classification or seniority inference.
All NLP classification is fully deterministic (overlay → YAML → pattern → fallback).
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HTML UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace. No external deps."""
    # Remove <script> and <style> blocks entirely
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# Ordered list of paths to try when looking for leadership info on a website.
_LEADERSHIP_PATHS = [
    "/leadership",
    "/leadership-team",
    "/about/leadership",
    "/about-us/leadership",
    "/our-leadership",
    "/team",
    "/our-team",
    "/management",
    "/management-team",
    "/about/management",
    "/about-us/management",
    "/board-of-directors",
    "/board",
    "/about/team",
    "/about-us/team",
    "/who-we-are",
    "/about",
    "/about-us",
]

_WEB_TIMEOUT = 8          # seconds per HTTP request
_MAX_PAGE_CHARS = 6_000   # chars to pass to Claude per page
_MAX_PAGES = 2            # stop after collecting this many useful pages


def _fetch_leadership_text(domain: str) -> str:
    """
    Try common leadership/about URLs on *domain* and return concatenated
    plain-text content from successful responses (max _MAX_PAGES pages).
    Returns "" if nothing useful is found or httpx is unavailable.
    """
    try:
        import httpx
    except ImportError:
        logger.debug("httpx not available — skipping website scrape")
        return ""

    if not domain:
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; OrgEngine/2.0; "
            "+https://github.com/vibhorkumar1209/organogram-engine)"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    base_candidates = [
        f"https://{domain}",
        f"https://www.{domain}",
    ]

    collected: list[str] = []
    tried: set[str] = set()

    for base in base_candidates:
        for path in _LEADERSHIP_PATHS:
            url = f"{base}{path}"
            if url in tried:
                continue
            tried.add(url)
            try:
                r = httpx.get(url, headers=headers,
                              timeout=_WEB_TIMEOUT, follow_redirects=True)
                ct = r.headers.get("content-type", "")
                if r.status_code != 200 or "text/html" not in ct:
                    continue
                text = _strip_html(r.text)
                if len(text) < 300:       # page too thin — probably a redirect stub
                    continue
                collected.append(f"[Page: {url}]\n{text[:_MAX_PAGE_CHARS]}")
                logger.debug(f"Scraped leadership page: {url} ({len(text)} chars)")
                if len(collected) >= _MAX_PAGES:
                    break
            except Exception as exc:
                logger.debug(f"Website scrape failed for {url}: {exc}")
                continue
        if len(collected) >= _MAX_PAGES:
            break

    return "\n\n---\n\n".join(collected)


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_FROM_WEB = """\
You are a corporate intelligence assistant.

You are given text scraped from a company's own website. Extract the current \
Board of Directors and Executive Management / C-Suite team from it.

Schema:
{
  "board": [
    {"name": "Full Name", "title": "Exact title from the website"}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact C-Suite title from the website"}
  ]
}

Rules:
- board: Chairman, Non-Executive Directors, Independent Directors, \
  Supervisory Board members, Board of Trustees members.
  Do NOT include executive directors who also hold a C-Suite role.
- executives: ONLY C-Suite — CEO, President, COO, CFO, CTO, CIO, CISO, CMO, \
  CHRO / Chief People Officer, CRO, CLO / General Counsel, Chief Strategy \
  Officer, Chief Digital Officer, Chief Commercial Officer, and equivalent.
  Do NOT include VPs, SVPs, MDs, or Directors.
- Use the exact names and titles as they appear on the website.
- If the scraped text does not contain leadership information, return \
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""

_SYSTEM_FROM_KNOWLEDGE = """\
You are a corporate intelligence assistant with knowledge of listed and \
major private companies worldwide.

Given a company name, return a JSON object with the current (or most recent \
publicly known) Board of Directors and Executive Management team.

Schema:
{
  "board": [
    {"name": "Full Name", "title": "Exact board title (e.g. Non-Executive Director, Chairman)"}
  ],
  "executives": [
    {"name": "Full Name", "title": "Exact C-Suite title (e.g. Chief Financial Officer)"}
  ]
}

Rules:
- board: include Chairman, Non-Executive Directors, Independent Directors, \
  Supervisory Board members, Board of Trustees members. Do NOT include \
  executive directors who also hold a C-Suite role (they go in executives).
- executives: include ONLY C-Suite: CEO, President, COO, CFO, CTO, CIO, CISO, \
  CMO, CHRO / Chief People Officer, CRO, CLO / General Counsel, Chief Strategy \
  Officer, Chief Digital Officer, Chief Commercial Officer, and equivalent.
  Do NOT include VPs, SVPs, MDs, or Directors.
- Use full legal/formal titles exactly as the company uses them.
- If the company is not publicly known or you have no reliable data, return \
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""


# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────

_LEADERSHIP_CACHE: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def llm_fetch_leadership(company_name: str, domain: str = "") -> dict:
    """
    Fetch Board of Directors and Executive Management for a company.

    Priority:
      1. Scrape the company's own website via *domain* (e.g. "rmsindia.com").
         Tries common leadership/about/team page paths.
      2. Fall back to Claude's training-data knowledge of the company.

    Results are cached in-process (keyed on company_name+domain).

    Returns:
        {
            "board":      [{"name": str, "title": str}, ...],
            "executives": [{"name": str, "title": str}, ...],
        }
    """
    if not company_name or len(company_name.strip()) < 3:
        return {"board": [], "executives": []}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("LLM leadership skipped: ANTHROPIC_API_KEY not set")
        return {"board": [], "executives": []}

    cache_key = f"{company_name.strip().lower()}|{(domain or '').strip().lower()}"
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    # ── Priority 1: scrape the company's own website ─────────────────────────
    web_text = _fetch_leadership_text(domain) if domain else ""
    if web_text:
        logger.info(
            f"Using website content for '{company_name}' "
            f"(domain={domain}, {len(web_text)} chars scraped)"
        )
        result = _call_claude(
            system=_SYSTEM_FROM_WEB,
            user_msg=(
                f"Company: {company_name}\n\n"
                f"Website content:\n{web_text}"
            ),
            label=f"{company_name} [web]",
        )
        # If the website gave us real data, use it
        if result["board"] or result["executives"]:
            _LEADERSHIP_CACHE[cache_key] = result
            return result
        logger.info(
            f"Website scrape returned no leaders for '{company_name}' "
            f"— falling back to LLM knowledge"
        )

    # ── Priority 2: LLM training-data knowledge ──────────────────────────────
    result = _call_claude(
        system=_SYSTEM_FROM_KNOWLEDGE,
        user_msg=f"Company: {company_name}",
        label=f"{company_name} [knowledge]",
    )
    _LEADERSHIP_CACHE[cache_key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _call_claude(system: str, user_msg: str, label: str) -> dict:
    """Call Claude and parse the JSON leadership response."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model added them
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)

        data = json.loads(raw)
        result: dict = {
            "board":      _clean_list(data.get("board",      [])),
            "executives": _clean_list(data.get("executives", [])),
        }
        logger.info(
            f"Claude leadership ({label}): "
            f"{len(result['board'])} board, {len(result['executives'])} execs"
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning(f"Claude leadership JSON parse error ({label}): {exc}")
    except Exception as exc:
        logger.warning(f"Claude leadership failed ({label}): {exc}")

    return {"board": [], "executives": []}


def _clean_list(raw: list) -> list[dict]:
    """Validate and normalise a list of {name, title} dicts."""
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name  = str(item.get("name",  "") or "").strip()
        title = str(item.get("title", "") or "").strip()
        if name and title and len(name.split()) >= 2:
            out.append({"name": name, "title": title})
    return out
