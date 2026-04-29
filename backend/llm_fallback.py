"""
LLM-based seniority fallback.
Called only when the deterministic NLP pipeline (overlay → exact → pattern →
substring → fuzzy → rule-based) produces no confident match.
Uses Claude Haiku (fastest, cheapest) with a terse classification prompt.
Results are cached in-process so the same title is never sent twice.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ── Classification prompt ──────────────────────────────────────────────────────

_SYSTEM = """\
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

# ── In-process cache ──────────────────────────────────────────────────────────

_CACHE: dict[tuple[str, str], int] = {}


def llm_classify_layer(
    designation: str,
    industry_hint: str = "",
    company: str = "",
) -> Optional[int]:
    """
    Call Claude Haiku to classify the seniority layer (0-10) of a job title.

    Returns the integer layer, or None if:
      - ANTHROPIC_API_KEY is not set
      - The API call fails
      - The response cannot be parsed as an integer 0-10

    Safe to call from synchronous code — uses the sync Anthropic client.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.debug("LLM fallback skipped: ANTHROPIC_API_KEY not set")
        return None

    # Normalise cache key
    cache_key = (designation.strip().lower(), industry_hint.strip().lower())
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # Build user message
    ctx_parts: list[str] = []
    if industry_hint:
        ctx_parts.append(f"Industry: {industry_hint}")
    if company:
        ctx_parts.append(f"Company: {company}")
    user_content = f"Title: {designation}"
    if ctx_parts:
        user_content += "\n" + "; ".join(ctx_parts)

    try:
        import anthropic  # lazy import — only needed if API key is present

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8,          # we only need a single digit
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()

        # Extract first integer 0-10 from the response
        m = re.search(r'\b(10|[0-9])\b', raw)
        if m:
            layer = int(m.group(1))
            _CACHE[cache_key] = layer
            logger.debug(f"LLM fallback: '{designation}' → L{layer}  (raw: '{raw}')")
            return layer

        logger.warning(f"LLM fallback: unparseable response '{raw}' for '{designation}'")

    except Exception as exc:
        logger.warning(f"LLM fallback failed for '{designation}': {exc}")

    return None
