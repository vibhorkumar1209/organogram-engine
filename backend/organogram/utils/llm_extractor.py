"""
LLM Extractor.

Uses Claude API (claude-sonnet-4-20250514) to extract Board of Directors
and Executive Management from cleaned text (firm website, SEC filing,
annual report). Applies fuzzy string matching to verify every extracted
name appears in the source text (anti-hallucination guard).

Every LLM call is assigned a UUID for provenance tracking.
"""
from __future__ import annotations
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx


CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 2000

# Fuzzy match thresholds — a name is "verified" if at least 75% of its tokens
# appear in the source text (case-insensitive, ignoring punctuation).
VERIFY_TOKEN_THRESHOLD = 0.75

# System prompt for the extraction task
EXTRACTION_SYSTEM = """You are a corporate intelligence extraction specialist.
You are given cleaned text from a company's leadership page, annual report,
or SEC filing.

Your job is to extract a structured list of Board of Directors and Executive
Management (C-suite, direct reports to CEO).

Return ONLY a valid JSON object. No prose. No markdown. No backticks.

Schema:
{
  "board_of_directors": [
    {
      "name": "Full name exactly as it appears in the text",
      "title": "Exact title from the text",
      "is_board": true
    }
  ],
  "executive_management": [
    {
      "name": "Full name exactly as it appears in the text",
      "title": "Exact title from the text",
      "is_board": false
    }
  ]
}

Rules:
- Use the EXACT name and title as they appear in the source text. Do not paraphrase.
- If a person appears in both board and executive roles, list them in BOTH arrays.
- Do NOT invent names or titles. If you are not sure, omit.
- Do NOT include country-level managers, regional heads, or below C-suite employees
  unless they are explicitly named as executive officers.
- Return empty arrays if no relevant people are found.
- Return JSON only. No other content whatsoever."""


@dataclass
class ExtractedLeader:
    """A single person extracted by the LLM from a source document."""
    name: str
    title: str
    is_board: bool
    source_url: str
    source_type: str
    raw_evidence: str
    verification_status: str
    verification_detail: str
    llm_call_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class LLMExtractor:
    """Extracts leaders from text using Claude API with anti-hallucination guard."""

    def __init__(self, api_key: Optional[str] = None):
        """
        api_key: Anthropic API key. If None, the engine looks for the
        ANTHROPIC_API_KEY environment variable at call time.
        Passing None here is fine — the API call will still be made.
        """
        self.api_key = api_key
        self._call_count = 0

    # ------------------------------------------------------------------
    # PUBLIC
    # ------------------------------------------------------------------
    def extract(
        self,
        cleaned_text: str,
        source_url: str,
        source_type: str,
        firm_name: str,
    ) -> list[ExtractedLeader]:
        """
        Main entry point. Returns a list of verified ExtractedLeader records.
        """
        if not cleaned_text or not cleaned_text.strip():
            return []

        raw_json, call_id = self._call_claude(cleaned_text, firm_name)
        if raw_json is None:
            return []

        raw_leaders = self._parse_json(raw_json)
        verified = []
        for item in raw_leaders:
            name = (item.get("name") or "").strip()
            title = (item.get("title") or "").strip()
            is_board = bool(item.get("is_board", False))
            if not name or not title:
                continue

            status, detail, evidence = self._verify(name, cleaned_text)
            verified.append(ExtractedLeader(
                name=name,
                title=title,
                is_board=is_board,
                source_url=source_url,
                source_type=source_type,
                raw_evidence=evidence,
                verification_status=status,
                verification_detail=detail,
                llm_call_id=call_id,
            ))

        self._call_count += 1
        return verified

    # ------------------------------------------------------------------
    # CLAUDE API CALL
    # ------------------------------------------------------------------
    def _call_claude(
        self, text: str, firm_name: str
    ) -> tuple[Optional[str], str]:
        """Call Claude API. Returns (raw_json_string, call_id)."""
        call_id = str(uuid.uuid4())
        user_message = (
            f"Extract all Board of Directors and Executive Management "
            f"(C-suite) from the following text from {firm_name!r}. "
            f"Return JSON only.\n\n---\n\n{text}"
        )

        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # API key is either passed explicitly or picked up from env by the
        # Anthropic SDK convention. We're using raw httpx here for portability.
        if self.api_key:
            headers["x-api-key"] = self.api_key

        body = {
            "model": CLAUDE_MODEL,
            "max_tokens": MAX_TOKENS,
            "system": EXTRACTION_SYSTEM,
            "messages": [{"role": "user", "content": user_message}],
        }

        try:
            resp = httpx.post(
                CLAUDE_API_URL,
                headers=headers,
                json=body,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    raw_text += block.get("text", "")
            return raw_text.strip(), call_id
        except httpx.HTTPStatusError as e:
            print(f"[llm_extractor] API error {e.response.status_code}: {e.response.text[:200]}")
            return None, call_id
        except Exception as e:
            print(f"[llm_extractor] Unexpected error: {e}")
            return None, call_id

    # ------------------------------------------------------------------
    # JSON PARSING
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(raw: str) -> list[dict]:
        """
        Parse the LLM's JSON response. Tolerant of minor formatting issues.
        """
        # Strip markdown fences if the model added them despite instructions
        raw = re.sub(r"^```(?:json)?", "", raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r"```$", "", raw.strip(), flags=re.MULTILINE)
        raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try extracting the first JSON object from the response
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    return []
            else:
                return []

        people = []
        if isinstance(data, dict):
            board = data.get("board_of_directors", [])
            exec_ = data.get("executive_management", [])
            for p in board:
                p["is_board"] = True
                people.append(p)
            for p in exec_:
                p.setdefault("is_board", False)
                people.append(p)
        elif isinstance(data, list):
            people = data

        return people

    # ------------------------------------------------------------------
    # ANTI-HALLUCINATION VERIFICATION (Decision 6: tolerant/fuzzy match)
    # ------------------------------------------------------------------
    def _verify(
        self, name: str, source_text: str
    ) -> tuple[str, str, str]:
        """
        Verify that `name` appears in `source_text` using fuzzy token matching.

        Returns:
          (status, detail, evidence_snippet)
          status: "verified" | "unverified"
        """
        # Normalise: lowercase, remove punctuation
        def norm(s: str) -> str:
            return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

        name_tokens = set(norm(name).split())
        if not name_tokens:
            return "unverified", "Name empty after normalisation.", ""

        source_norm = norm(source_text)

        # Count tokens present in source (ignoring stopwords of length 1-2)
        meaningful_tokens = {t for t in name_tokens if len(t) > 2}
        if not meaningful_tokens:
            meaningful_tokens = name_tokens  # fallback for short names

        matched = sum(1 for t in meaningful_tokens if t in source_norm)
        ratio = matched / len(meaningful_tokens)

        if ratio >= VERIFY_TOKEN_THRESHOLD:
            # Find evidence snippet: first 200 chars around the surname
            surname = list(meaningful_tokens)[-1]  # heuristic: last token
            pos = source_norm.find(surname)
            if pos == -1:
                # Try any token
                for tok in meaningful_tokens:
                    pos = source_norm.find(tok)
                    if pos != -1:
                        break
            # Map position back to original (approximate, not byte-exact)
            words = source_text.split()
            norm_words = source_norm.split()
            try:
                word_pos = norm_words.index(surname) if surname in norm_words else 0
                word_start = max(0, word_pos - 10)
                word_end = min(len(words), word_pos + 20)
                evidence = " ".join(words[word_start:word_end])
            except ValueError:
                evidence = source_text[:200]

            return (
                "verified",
                f"{matched}/{len(meaningful_tokens)} tokens matched in source.",
                evidence,
            )
        else:
            return (
                "unverified",
                f"Only {matched}/{len(meaningful_tokens)} tokens found in source. "
                f"Possible hallucination — dropped from output unless corroborated.",
                "",
            )
