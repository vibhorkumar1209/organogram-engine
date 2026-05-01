"""
HTML Fetcher.

Responsibilities:
  - Fetch a URL with retries and rate limiting.
  - Cache responses locally (keyed by MD5(URL) + date) so reruns
    don't re-fetch the same page. Cache expires daily.
  - Clean the raw HTML for the LLM: strip <script>, <style>, <nav>,
    <footer>, <header>, <aside>, hidden elements. Return clean plain text.
  - Never hit the same domain more than once per second.

No authentication; only fetches publicly accessible pages.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup, Comment


# How long to wait between requests to the same domain (seconds)
DOMAIN_RATE_LIMIT = 1.5

# Max characters of cleaned text to pass to the LLM
# (keeps token costs bounded — leadership pages rarely need more than 8 000 chars)
MAX_CLEANED_CHARS = 12_000

# Tags to strip entirely before passing to the LLM
STRIP_TAGS = {
    "script", "style", "noscript", "nav", "footer", "header",
    "aside", "form", "input", "button", "meta", "link",
    "svg", "path", "img", "picture", "figure",
}

USER_AGENT = (
    "Mozilla/5.0 (compatible; OrganogramEngine/1.0; "
    "+https://refractone.com/organogram-engine)"
)


class FetchResult:
    """Outcome of a single fetch operation."""

    def __init__(
        self,
        url: str,
        raw_html: str,
        cleaned_text: str,
        status_code: int,
        cache_hit: bool,
        error: Optional[str] = None,
    ):
        self.url = url
        self.raw_html = raw_html
        self.cleaned_text = cleaned_text
        self.status_code = status_code
        self.cache_hit = cache_hit
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code < 400

    def __repr__(self):
        return (f"FetchResult(url={self.url!r}, status={self.status_code}, "
                f"cache_hit={self.cache_hit}, chars={len(self.cleaned_text)}, "
                f"ok={self.ok})")


class HTMLFetcher:
    """Rate-limited, cached HTML fetcher."""

    def __init__(self, cache_dir: str | Path, timeout: float = 15.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._domain_last_fetch: dict[str, float] = {}

    # ------------------------------------------------------------------
    # PUBLIC
    # ------------------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        """Fetch URL, returning a FetchResult. Uses local cache if fresh."""
        cache_path = self._cache_path(url)
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("date") == str(date.today()):
                raw_html = cached["raw_html"]
                return FetchResult(
                    url=url,
                    raw_html=raw_html,
                    cleaned_text=self._clean(raw_html),
                    status_code=cached.get("status_code", 200),
                    cache_hit=True,
                )

        # Rate limiting
        domain = urlparse(url).netloc
        since = time.monotonic() - self._domain_last_fetch.get(domain, 0)
        if since < DOMAIN_RATE_LIMIT:
            time.sleep(DOMAIN_RATE_LIMIT - since)

        try:
            resp = httpx.get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            self._domain_last_fetch[domain] = time.monotonic()
            raw_html = resp.text
            status_code = resp.status_code
            error = None
        except httpx.TimeoutException:
            return FetchResult(url, "", "", 408, False, "Request timed out.")
        except httpx.RequestError as e:
            return FetchResult(url, "", "", 0, False, str(e))

        # Persist to cache
        cache_path.write_text(
            json.dumps({"url": url, "date": str(date.today()),
                        "status_code": status_code, "raw_html": raw_html},
                       ensure_ascii=False),
            encoding="utf-8",
        )

        return FetchResult(
            url=url,
            raw_html=raw_html,
            cleaned_text=self._clean(raw_html),
            status_code=status_code,
            cache_hit=False,
            error=error,
        )

    def fetch_many(self, urls: list[str]) -> list[FetchResult]:
        return [self.fetch(url) for url in urls]

    # ------------------------------------------------------------------
    # HTML CLEANING
    # ------------------------------------------------------------------
    def _clean(self, raw_html: str) -> str:
        """
        Strip noise tags and return readable plain text suitable for the LLM.
        Preserves structure implied by block-level elements.
        """
        if not raw_html:
            return ""
        soup = BeautifulSoup(raw_html, "html.parser")

        # Remove HTML comments
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()

        # Remove noise tags
        for tag in soup.find_all(STRIP_TAGS):
            tag.decompose()

        # Remove elements with display:none or visibility:hidden
        for tag in soup.find_all(style=re.compile(
                r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)):
            tag.decompose()

        # Remove aria-hidden elements
        for tag in soup.find_all(attrs={"aria-hidden": "true"}):
            tag.decompose()

        # Extract text, inserting newlines at block-level elements
        text = soup.get_text(separator="\n", strip=True)

        # Collapse 3+ consecutive newlines to 2
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Truncate to keep token cost bounded
        if len(text) > MAX_CLEANED_CHARS:
            text = text[:MAX_CLEANED_CHARS] + "\n\n[...truncated for length...]"

        return text.strip()

    # ------------------------------------------------------------------
    # CACHE
    # ------------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        key = hashlib.md5(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def invalidate(self, url: str):
        p = self._cache_path(url)
        if p.exists():
            p.unlink()
