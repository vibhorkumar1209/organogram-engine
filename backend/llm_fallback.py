"""
LLM-based seniority fallback + company leadership enrichment.

Two public functions:

  llm_classify_layer(designation, industry_hint, company)
    Called when the deterministic NLP pipeline cannot classify a title with
    confidence (conf ≤ 0.35). Returns layer int 0-10 or None.

  llm_fetch_leadership(company_name)
    Called automatically for every uploaded company. Returns the known
    Board of Directors (L0) and Executive Management / C-Suite (L1) so
    the organogram always shows top leadership even when not in the input file.
    Returns {"board": [...], "executives": [...]}  each item = {name, title}.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 1.  TITLE CLASSIFICATION FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """\
You are a corporate hierarchy classifier.
Given a job title and optional context, return ONLY a single integer (0-10) \
representing the seniority layer on this universal scale:

0  Board / Non-Executive Director / NED
1  C-Suite (CEO, CFO, CTO, CHRO, CMO …)
2  MD / EVP / Executive Director / Managing Director
3  VP / SVP / General Manager / Country Head
4  Senior Director / AVP / Head of [Function]
5  Director / Head
6  Senior Manager / Associate Director / DGM
7  Manager / AGM / Team Lead / Project Manager
8  Senior IC / Tech Lead / Staff Engineer / Senior Analyst
9  Analyst / Engineer / Specialist / Associate / IC
10 Graduate / Intern / Trainee / Junior

Reply with ONLY the integer. No words, no punctuation."""

_CLASSIFY_CACHE: dict[tuple[str, str], int] = {}


def llm_classify_layer(
    designation: str,
    industry_hint: str = "",
    company: str = "",
) -> Optional[int]:
    """
    Call Claude Haiku to classify the seniority layer (0-10) of a job title.

    Returns the integer layer, or None if ANTHROPIC_API_KEY is not set,
    the API call fails, or the response cannot be parsed.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("LLM classify skipped: ANTHROPIC_API_KEY not set")
        return None

    cache_key = (designation.strip().lower(), industry_hint.strip().lower())
    if cache_key in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[cache_key]

    ctx_parts: list[str] = []
    if industry_hint:
        ctx_parts.append(f"Industry: {industry_hint}")
    if company:
        ctx_parts.append(f"Company: {company}")
    user_content = f"Title: {designation}"
    if ctx_parts:
        user_content += "\n" + "; ".join(ctx_parts)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        m = re.search(r'\b(10|[0-9])\b', raw)
        if m:
            layer = int(m.group(1))
            _CLASSIFY_CACHE[cache_key] = layer
            logger.debug(f"LLM classify: '{designation}' → L{layer}")
            return layer
        logger.warning(f"LLM classify: unparseable response '{raw}' for '{designation}'")
    except Exception as exc:
        logger.warning(f"LLM classify failed for '{designation}': {exc}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 2.  COMPANY LEADERSHIP ENRICHMENT
# ─────────────────────────────────────────────────────────────────────────────

_LEADERSHIP_SYSTEM = """\
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
- board: include Chairman, Non-Executive Directors, Independent Directors,
  Supervisory Board members, Board of Trustees members. Do NOT include
  executive directors who also hold a C-Suite role (they go in executives).
- executives: include ONLY C-Suite: CEO, President, COO, CFO, CTO, CIO, CISO,
  CMO, CHRO / Chief People Officer, CRO, CLO / General Counsel, Chief Strategy
  Officer, Chief Digital Officer, Chief Commercial Officer, and equivalent.
  Do NOT include VPs, SVPs, MDs, or Directors.
- Use full legal/formal titles exactly as the company uses them.
- If the company is not publicly known or you have no reliable data, return
  {"board": [], "executives": []}.
- Return ONLY valid JSON. No explanation, no markdown, no code blocks."""

_LEADERSHIP_CACHE: dict[str, dict] = {}


def llm_fetch_leadership(company_name: str) -> dict:
    """
    Fetch Board of Directors and Executive Management for a company via Claude.

    Called automatically for every uploaded company regardless of input data.
    Results are cached in-process.

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

    cache_key = company_name.strip().lower()
    if cache_key in _LEADERSHIP_CACHE:
        return _LEADERSHIP_CACHE[cache_key]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=_LEADERSHIP_SYSTEM,
            messages=[{"role": "user", "content": f"Company: {company_name}"}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if the model added them
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$',         '', raw, flags=re.MULTILINE)

        data = json.loads(raw)
        result: dict = {
            "board":      _clean_list(data.get("board",      [])),
            "executives": _clean_list(data.get("executives", [])),
        }
        _LEADERSHIP_CACHE[cache_key] = result
        logger.info(
            f"LLM leadership: '{company_name}' → "
            f"{len(result['board'])} board, {len(result['executives'])} execs"
        )
        return result

    except json.JSONDecodeError as exc:
        logger.warning(f"LLM leadership: JSON parse error for '{company_name}': {exc}")
    except Exception as exc:
        logger.warning(f"LLM leadership failed for '{company_name}': {exc}")

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
