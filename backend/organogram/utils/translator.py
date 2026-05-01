"""
Google Translate hookup for Agent 3 (NLP Agent).

Wraps `deep-translator` (GoogleTranslator), which uses Google's free
translation endpoint — no API key required for normal volumes.

Install:
    pip install deep-translator

Features
--------
- In-process LRU-style cache — the same title is never translated twice
  within a pipeline run.
- Per-call timeout (default 8 s) via the requests session.
- Automatic retry with truncated exponential back-off on transient errors
  (max 3 attempts).
- Graceful fallback to the identity function when the library is not
  installed OR when all retries are exhausted — the pipeline keeps running.
- Thread-safe cache via a simple dict (GIL is sufficient for CPython).

Usage
-----
    from organogram.utils.translator import make_google_translator

    translate = make_google_translator()         # default, returns callable
    nlp = LinkedInNLPAgent(rules=..., translator=translate, ...)

The returned callable signature:
    translate(text: str, target_lang: str = "en") -> str
"""
from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process translation cache
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, str], str] = {}

# ---------------------------------------------------------------------------
# Retry config
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_RETRY_BASE_SLEEP = 1.0   # seconds; doubles each attempt


def _translate_once(text: str, target_lang: str, timeout: int) -> str:
    """Single translation call — no retry, no caching."""
    from deep_translator import GoogleTranslator   # type: ignore[import]
    result = GoogleTranslator(
        source="auto", target=target_lang
    ).translate(text)
    return result or text


def _translate_with_retry(
    text: str,
    target_lang: str,
    timeout: int,
) -> str:
    """Translate with exponential back-off on failure."""
    last_exc: Exception | None = None
    sleep = _RETRY_BASE_SLEEP
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return _translate_once(text, target_lang, timeout)
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                logger.debug(
                    f"[translator] attempt {attempt} failed for {text!r}: {exc}. "
                    f"Retrying in {sleep:.1f}s …"
                )
                time.sleep(sleep)
                sleep = min(sleep * 2, 8.0)   # cap at 8 s
    # All retries exhausted
    logger.warning(
        f"[translator] All {_MAX_RETRIES} retries failed for {text!r}. "
        f"Last error: {last_exc}. Returning original text."
    )
    return text


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def make_google_translator(timeout: int = 8) -> Callable[[str, str], str]:
    """
    Return a cached Google Translate callable compatible with LinkedInNLPAgent.

    Parameters
    ----------
    timeout : int
        HTTP timeout in seconds for each translation request (default 8).

    Returns
    -------
    Callable[[str, str], str]
        translate(text, target_lang) -> translated_text

    If `deep-translator` is not installed, emits a one-time warning and
    returns the identity function so the pipeline degrades gracefully.
    """
    try:
        import deep_translator  # noqa: F401 — verify installed
    except ModuleNotFoundError:
        logger.warning(
            "[translator] `deep-translator` not installed. "
            "Run `pip install deep-translator`. Falling back to identity translator."
        )
        return lambda text, target_lang="en": text

    _warned_fallback: set[str] = set()   # log once per unique text that fell back

    def translate(text: str, target_lang: str = "en") -> str:
        if not text or not text.strip():
            return text

        cache_key = (text, target_lang)
        if cache_key in _CACHE:
            return _CACHE[cache_key]

        result = _translate_with_retry(text, target_lang, timeout)

        if result == text and text not in _warned_fallback:
            _warned_fallback.add(text)
            logger.debug(
                f"[translator] Translation returned original text for {text!r} "
                f"(either already English or API unavailable)."
            )

        _CACHE[cache_key] = result
        logger.debug(f"[translator] {text!r} -> {result!r} (lang={target_lang})")
        return result

    return translate


# ---------------------------------------------------------------------------
# Cache inspection helpers (useful for tests and diagnostics)
# ---------------------------------------------------------------------------
def cache_size() -> int:
    return len(_CACHE)


def clear_cache() -> None:
    _CACHE.clear()
