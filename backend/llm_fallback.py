"""
LLM company leadership enrichment — Board of Directors and Executive Management only.

One public function:

  llm_fetch_leadership(company_name)
    Called automatically for every uploaded company. Returns the known
    Board of Directors (L0) and Executive Management / C-Suite (L1) so
    the organogram always shows top leadership even when not in the input file.
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
# COMPANY LEADERSHIP ENRICHMENT
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
